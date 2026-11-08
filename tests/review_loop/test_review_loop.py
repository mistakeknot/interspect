"""Fixture tests for the review-loop core, judge validation and CLI replay."""

import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from review_loop import core, judge  # noqa: E402

WEEK = "2026-W40"  # Mon 2026-09-28 .. Sun 2026-10-04
RUN_AT = core.parse_time("2026-10-25T00:00:00Z")  # past close + 14 days


def rec(pr, day, verdict, reviewer="gpt-6", producer="claude-sonnet-5-5", head="h", did=None):
    return {"repo": "o/r", "pr": pr, "created_at": f"2026-{day}T10:00:00Z", "verdict": verdict,
            "reviewer_identity": reviewer, "producer_identity": producer, "head_sha": head,
            "dispatch_id": did or f"{pr}-{day}-{verdict}"}


def frame_of(n, reviewer="gpt-6", producer="claude-sonnet-5-5", start=1):
    return [{"pr": f"o/r#{start + i}", "reviewer": reviewer, "producer": producer,
             "blocks": [{"round": 1}]} for i in range(n)]


def test_week_bounds_and_label():
    start, end = core.week_bounds(WEEK)
    assert start.isoformat().startswith("2026-09-28") and end.isoformat().startswith("2026-10-05")
    assert core.iso_week(core.parse_time("2026-10-04T23:59:59Z")) == WEEK


def test_timestamps_with_fractions_and_bad_rows():
    assert core.parse_time("2026-10-24T16:07:03.404891120Z").microsecond == 404891
    rows = core.load_receipts([rec(1, "09-29", "BLOCK"),
                               {**rec(2, "09-29", "BLOCK"), "created_at": "yesterday"}])
    assert [r["pr"] for r in rows] == ["o/r#1"]


def test_freeze_refuses_before_horizon():
    rows = core.load_receipts([rec(1, "09-29", "BLOCK")])
    with pytest.raises(ValueError, match="horizon not elapsed"):
        core.freeze(rows, WEEK, core.parse_time("2026-10-10T00:00:00Z"), code_version="t")


def test_cohort_is_first_review_of_any_verdict_and_frame_needs_a_block():
    rows = core.load_receipts([
        rec(1, "09-29", "PASS"), rec(1, "10-02", "BLOCK"),       # first review in week, later block -> frame
        rec(2, "09-20", "BLOCK"), rec(2, "09-30", "BLOCK"),       # first review before week -> not cohort
        rec(3, "10-01", "PASS"),                                   # cohort, no block -> not frame
        rec(4, "10-03", "BLOCK"), rec(4, "11-30", "BLOCK"),       # second block after horizon -> late
    ])
    snap = core.freeze(rows, WEEK, RUN_AT, code_version="t")
    pop = snap["population"]
    assert pop["cohort_prs"] == ["o/r#1", "o/r#3", "o/r#4"]
    assert [f["pr"] for f in pop["frame"]] == ["o/r#1", "o/r#4"]
    assert pop["n_block_receipts"] == 2
    assert pop["late_rounds"] == 1


def test_freeze_is_deterministic_and_seed_tracks_population():
    rows = core.load_receipts([rec(1, "09-29", "BLOCK"), rec(2, "09-30", "BLOCK")])
    a = core.freeze(rows, WEEK, RUN_AT, code_version="t")
    b = core.freeze(list(reversed(rows)), WEEK, RUN_AT, code_version="t")
    assert core.canonical_json(a) == core.canonical_json(b)
    c = core.freeze(core.load_receipts([rec(1, "09-29", "BLOCK")]), WEEK, RUN_AT, code_version="t")
    assert c["manifest"]["bootstrap_seed"] != a["manifest"]["bootstrap_seed"]


def test_take_all_when_frame_at_most_budget():
    plan = core.allocate(frame_of(30))
    assert plan["take_all"] and plan["budget_used"] == 30
    drawn = core.draw(frame_of(30), 1)
    assert all(s["weight"] == 1.0 for s in drawn["sample"])


def test_weights_sum_to_stratum_population_and_budget_holds():
    frame = frame_of(40, "gpt-6", "claude-sonnet-5-5") + frame_of(20, "claude-opus-5-5", "gpt-6", start=100)
    drawn = core.draw(frame, 7)
    assert len(drawn["sample"]) <= core.BUDGET
    by = {}
    for s in drawn["sample"]:
        by.setdefault(s["stratum"], []).append(s)
    for label, items in by.items():
        pop = len(drawn["allocation"]["strata"][label]["members"])
        assert sum(i["weight"] for i in items) == pytest.approx(pop)


def test_opus_certainty_stratum_taken_whole_when_small():
    frame = frame_of(40) + frame_of(5, "claude-opus-5-5", "gpt-6", start=100)
    plan = core.allocate(frame)
    s = plan["strata"]["claude-opus-5-5|gpt-6"]
    assert s["certainty"] and s["n"] == 5


def test_budget_raised_when_mandatory_exceeds_it():
    frame = []
    for i in range(40):  # 40 distinct strata of 2 PRs each, no pooling can fit 30
        frame += frame_of(2, f"rev{i}", f"prod{i}", start=i * 10 + 1)
    plan = core.allocate(frame)
    assert plan["budget_used"] >= 1


def test_draw_is_reproducible_for_a_seed():
    frame = frame_of(50)
    assert core.draw(frame, 3) == core.draw(frame, 3)
    assert core.draw(frame, 3) != core.draw(frame, 4)


def finding(outcome="defect", cat="logic-bug", mech="m", sev="medium"):
    return {"category": cat, "outcome": outcome, "class": "unknown", "severity": sev,
            "mechanism": mech, "prevent_with": "test"}


def sample_rows(n, w=1.0, blocks=1, stratum="a|b"):
    return [{"pr": f"o/r#{i}", "stratum": stratum, "weight": w, "n_blocks": blocks,
             "stratum_size": n, "stratum_n": n, "pooled": False} for i in range(n)]


def test_receipt_outcome_precedence():
    assert core.receipt_outcome([finding("defect_minor"), finding("defect")], True) == "defect"
    assert core.receipt_outcome([finding("process_slip")], True) == "unresolved"
    assert core.receipt_outcome([finding("defect_minor"), finding("policy_gate")], False) == "defect_minor"
    assert core.receipt_outcome([finding("policy_gate")], False) == "policy_gate"
    assert core.receipt_outcome([], False) == "unclear"


def test_bounds_ordering_and_same_denominator():
    sample = sample_rows(10, w=2.0, blocks=2)
    judgments = {}
    for i, s in enumerate(sample):
        if i < 3:
            judgments[s["pr"]] = {"receipts": [{"round": 1, "findings": [finding()]},
                                               {"round": 2, "findings": [finding("over_strict")]}]}
        elif i < 5:
            judgments[s["pr"]] = {"receipts": [{"round": 1, "partial": True, "findings": []},
                                               {"round": 2, "findings": [finding("over_strict")]}]}
        # the rest have no judgments: both receipts count as unresolved
    units = core.build_units(sample, judgments)
    lo, up = core._bounds(units)
    den = 10 * 2 * 2.0
    assert lo == pytest.approx(3 * 2.0 / den)
    assert up == pytest.approx((3 * 2.0 + (2 * 1 + 5 * 2) * 2.0) / den)
    assert 0 <= lo <= up <= 1


def test_bootstrap_endpoints_ordered_deterministic_and_small_sample_suppressed():
    sample = sample_rows(12, w=3.0)
    judgments = {s["pr"]: {"receipts": [{"round": 1, "findings": [finding("defect" if i % 3 == 0 else "over_strict")]}]}
                 for i, s in enumerate(sample)}
    snap = {"manifest": {"week": WEEK, "bootstrap_seed": 11}, "population": {"n_block_receipts": 36}}
    a = core.aggregate(sample, judgments, snap["manifest"], snap["population"])
    b = core.aggregate(sample, judgments, snap["manifest"], snap["population"])
    assert a == b and a["interval"] is not None
    assert a["interval"]["L"][0] <= a["interval"]["L"][1]
    assert a["interval"]["U"][0] <= a["interval"]["U"][1]
    assert a["L"] <= a["U"]
    small = core.aggregate(sample[:5], judgments, snap["manifest"], snap["population"])
    assert small["interval"] is None


def test_certainty_strata_have_zero_variance():
    sample = sample_rows(10, w=1.0)  # stratum_n == stratum_size: whole stratum, f == 1
    judgments = {s["pr"]: {"receipts": [{"round": 1, "findings": [finding("defect" if i < 4 else "over_strict")]}]}
                 for i, s in enumerate(sample)}
    manifest = {"week": WEEK, "bootstrap_seed": 5}
    out = core.aggregate(sample, judgments, manifest, {"n_block_receipts": 10})
    assert out["interval"]["L"][0] == out["interval"]["L"][1] == pytest.approx(0.4)


def test_backlog_threshold_and_no_relaxation_rule():
    sample = sample_rows(4)
    judgments = {s["pr"]: {"receipts": [{"round": 1, "findings": [finding(mech="stale cache")]}]}
                 for s in sample[:3]}
    judgments[sample[3]["pr"]] = {"receipts": [{"round": 1, "findings": [
        finding(cat="security-access", mech="open admin route", sev="high")]}]}
    mechs = core.mechanism_counts(sample, judgments)
    props = core.backlog_proposals(mechs, {"logic-bug: stale cache": "Return notes when the cache is stale"})
    names = {p["mechanism"]: p for p in props}
    assert "logic-bug: stale cache" in names and "security-access: open admin route" in names
    assert names["logic-bug: stale cache"]["rejected_by_no_relaxation_rule"]
    assert not names["security-access: open admin route"]["rejected_by_no_relaxation_rule"]


def test_backlog_needs_two_prs_for_a_non_high_mechanism():
    sample = sample_rows(1)
    judgments = {sample[0]["pr"]: {"receipts": [{"round": 1, "findings": [
        finding(mech="x"), finding(mech="x"), finding(mech="x")]}]}}
    assert core.backlog_proposals(core.mechanism_counts(sample, judgments)) == []


def good_reply(**over):
    f = {"category": "logic-bug", "outcome": "defect", "class": "semantic_drift", "severity": "high",
         "mechanism": "off by one", "prevent_with": "test"}
    f.update(over)
    return "notes\nJUDGMENT_JSON: " + json.dumps({"receipts": [{"round": 1, "findings": [f]}]})


def test_judge_parse_and_validation():
    assert judge.parse_judgment(good_reply())["receipts"][0]["round"] == 1
    for bad in (good_reply(category="vibes"), good_reply(outcome="bad"), good_reply(severity="huge"),
                good_reply(mechanism=""), "no json here"):
        with pytest.raises(ValueError):
            judge.parse_judgment(bad)


def test_judge_agreement():
    a = judge.parse_judgment(good_reply())
    b = judge.parse_judgment(good_reply(outcome="over_strict"))
    assert judge.agreement(a, a) == 1.0 and judge.agreement(a, b) == 0.0


def test_report_and_digest_are_bounded_and_say_what_they_are(tmp_path):
    sample = sample_rows(10, w=2.0)
    judgments = {s["pr"]: {"receipts": [{"round": 1, "findings": [finding()]}]} for s in sample}
    manifest = {"week": WEEK, "population_sha256": "ab" * 32, "rubric_version": "v1",
                "judge_prompt_version": "j1", "code_version": "t", "run_at": "2026-10-25T00:00:00Z",
                "bootstrap_seed": 1}
    pop = {"n_block_receipts": 20, "late_rounds": 0, "frame": [{}] * 20, "cohort_prs": []}
    agg = core.aggregate(sample, judgments, manifest, pop)
    plan = core.allocate(frame_of(20))
    text = core.render_report(manifest, pop, plan, agg, {}, [], {"recovered": 10, "total": 10})
    assert "lower bound" in text and "Nothing here ranks" in text
    digest = core.digest_block(manifest, agg, [], {"recovered": 10, "total": 10})
    assert len(digest.splitlines()) <= 10


def _write_receipts(directory, rows):
    directory.mkdir()
    for i, r in enumerate(rows):
        (directory / f"r{i}.json").write_text(json.dumps(r))


def test_cli_replay_is_byte_equal(tmp_path):
    rows = [rec(i, "09-29", "BLOCK", did=f"d{i}") for i in range(1, 15)]
    _write_receipts(tmp_path / "rc", rows)
    judgments = {f"o/r#{i}": {"receipts": [{"round": 1, "findings": [finding("defect" if i % 2 else "over_strict")]}]}
                 for i in range(1, 15)}
    (tmp_path / "j.json").write_text(json.dumps(judgments))

    def run(tag):
        d = tmp_path / tag
        d.mkdir()
        env = {"PYTHONPATH": str(SCRIPTS), "PATH": "/usr/bin:/bin"}
        base = [sys.executable, "-m", "review_loop"]
        def go(*args):
            return subprocess.run(base + list(args), env=env, capture_output=True, text=True, check=True,
                                  cwd=str(d))
        go("freeze", "--receipts", str(tmp_path / "rc"), "--week", WEEK, "--run-at", "2026-10-25T00:00:00Z",
           "--out", "snap.json")
        go("sample", "--snapshot", "snap.json", "--out", "sample.json")
        go("aggregate", "--snapshot", "snap.json", "--sample", "sample.json",
           "--judgments", str(tmp_path / "j.json"), "--report", "report.md", "--digest", "digest.txt")
        return {n: (d / n).read_bytes() for n in ("snap.json", "sample.json", "report.md", "digest.txt")}

    assert run("a") == run("b")


def test_cli_refuses_before_horizon(tmp_path):
    _write_receipts(tmp_path / "rc", [rec(1, "09-29", "BLOCK")])
    r = subprocess.run([sys.executable, "-m", "review_loop", "freeze", "--receipts", str(tmp_path / "rc"),
                        "--week", WEEK, "--run-at", "2026-10-06T00:00:00Z", "--out", str(tmp_path / "s.json")],
                       env={"PYTHONPATH": str(SCRIPTS), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 2 and "horizon not elapsed" in r.stderr


def test_public_hygiene_no_private_markers():
    banned = ("/home/", "/Users/", "thread-storage", "ruling")
    for path in (SCRIPTS / "review_loop").glob("*.py"):
        text = path.read_text().lower()
        for b in banned:
            assert b.lower() not in text, f"{path.name} contains {b}"
