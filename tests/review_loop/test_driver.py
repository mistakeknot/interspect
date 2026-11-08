"""Offline tests for the weekly driver: fake evidence, fake judge, fake quota."""

import json
import pathlib
import re
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from review_loop import cli, core, driver  # noqa: E402

WEEK = "2026-W40"
NOW = core.parse_time("2026-10-25T00:00:00Z")


def rows(n_prs=8, blocks_per=2, reviewer="gpt-6.1-sol"):
    out = []
    for i in range(1, n_prs + 1):
        for k in range(blocks_per):
            out.append({"repo": "o/r", "pr": i, "created_at": f"2026-09-{29 + (k > 0)}T1{k}:00:00Z",
                        "verdict": "BLOCK", "reviewer_identity": reviewer,
                        "producer_identity": "claude-sonnet-5-5", "head_sha": f"{i:02d}{k}".ljust(40, "a"),
                        "base_sha": "b" * 40, "dispatch_id": f"{i}-{k}"})
    return out


def judgment(rounds, outcome="defect", partial=False):
    return "JUDGMENT_JSON: " + json.dumps({"receipts": [
        {"round": r, "partial": partial, "unresolved_reason": None, "findings": [
            {"category": "logic-bug", "outcome": outcome, "class": "unknown", "severity": "high",
             "mechanism": "m", "prevent_with": "test"}]} for r in rounds]})


class Fake:
    def __init__(self, identity="claude-opus-5-5", no_text=(), quota=lambda n: 0.2, stage_b_outcome=None,
                 reply=None, reply_fn=None, reviewers=None):
        self.calls, self.identity, self.no_text, self.quota_fn = [], identity, set(no_text), quota
        self.stage_b_outcome, self.reply, self.reply_fn = stage_b_outcome, reply, reply_fn
        self.reviewers, self.producers = reviewers or {}, []

    def evidence(self, pr, block):
        return {"text": None if pr in self.no_text else f"finding for {pr} round {block['round']}",
                "diff": "```diff\n+x\n```", "spec": "spec", "outcome": "merged"}

    def dispatch(self, prompt, reviewer, tag):
        self.calls.append(tag)
        self.producers.append(reviewer)
        if self.reply_fn is not None:
            text = self.reply_fn(prompt, tag)
        elif self.reply is not None:
            text = self.reply
        else:
            rounds = [int(x) for x in re.findall(r"--- receipt round (\d+) head", prompt)]
            kind = "B" if "Stage B" in prompt else "A"
            outcome = self.stage_b_outcome if (kind == "B" and self.stage_b_outcome) else "defect"
            text = judgment(rounds, outcome)
        return {"text": text, "identity": self.identity, "effort": "medium", "dispatch_id": f"d{len(self.calls)}",
                "exit_code": 0}

    def quota(self):
        return [{"enabled": True, "limitWindows": [{"utilization": self.quota_fn(len(self.calls))}]}]

    def env(self):
        return driver.Env(self.evidence, self.dispatch, self.quota, lambda: NOW)


def test_quota_gate_cases():
    ok = {"enabled": True, "limitWindows": [{"utilization": 0.3}, {"utilization": 0.5}]}
    hot = {"enabled": True, "limitWindows": [{"utilization": 0.9}]}
    blind = {"enabled": True, "limitWindows": [{"utilization": None}]}
    off = {"enabled": False, "limitWindows": [{"utilization": 0.0}]}
    assert driver.quota_gate([hot, ok])[0]
    assert not driver.quota_gate([hot])[0]
    assert not driver.quota_gate([blind])[0]
    assert not driver.quota_gate([off])[0]
    assert not driver.quota_gate([])[0]


def test_latest_ready_week_is_the_first_whose_horizon_elapsed():
    assert driver.latest_ready_week(NOW) == WEEK
    just_before = core.parse_time("2026-10-18T23:59:59Z")  # W40 closes 10-05, horizon 10-19
    assert driver.latest_ready_week(just_before) == "2026-W39"


def test_sanitize_neutralises_markers_and_truncates():
    out = driver.sanitize("a ```x``` ~~~ <!-- c --> b", 100)
    assert "```" not in out and "~~~" not in out and "<!--" not in out
    assert driver.sanitize("é" * 100, 21).endswith("[truncated]")


def test_end_to_end_week(tmp_path):
    fake = Fake(no_text={"o/r#3"}, stage_b_outcome="over_strict")
    summary = driver.run_week(fake.env(), rows(), tmp_path, week=None)
    run = pathlib.Path(summary["run_dir"])
    assert run.name == WEEK
    report = (run / "report.md").read_text()
    assert "Stage A (blind, headline)" in report and "outcome-induced label change" in report
    assert "Calibration set:" in report and "Second judge:" in report
    assert 0.0 <= summary["L"] <= summary["U"] <= 1.0
    # Stage A all defect except the unrecovered PR, which is unresolved and widens U
    assert summary["L"] < summary["U"]
    assert summary["L_stage_b"] < summary["L"]
    a = json.loads((run / "judgments-a.json").read_text())
    assert a["o/r#3"]["receipts"][0]["unresolved_reason"] == "text_unrecovered"
    assert not any(t.endswith("o/r#3") for t in fake.calls if t.startswith(("A:", "B:")))
    assert (run / "digest.txt").read_text().strip()


def test_prompts_do_not_carry_fence_markers(tmp_path):
    seen = []
    fake = Fake()
    orig = fake.dispatch
    fake.dispatch = lambda p, r, t: (seen.append(p), orig(p, r, t))[1]
    driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert seen and all("```" not in p and "~~~" not in p for p in seen)


def test_judge_lab_is_declared_and_same_lab_is_flagged(tmp_path):
    fake = Fake(identity="gpt-6.1-sol")  # same lab as the reviewer
    summary = driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert "Same-lab exception" in (pathlib.Path(summary["run_dir"]) / "report.md").read_text()
    fake2 = Fake(identity=None)
    s2 = driver.run_week(fake2.env(), rows(3), tmp_path / "x", with_calibration=False)
    assert "identity not recorded" in (pathlib.Path(s2["run_dir"]) / "report.md").read_text()


def test_unparseable_judge_reply_leaves_receipts_unresolved(tmp_path):
    fake = Fake(reply="I could not decide.")
    summary = driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert summary["L"] == 0.0 and summary["U"] == 1.0


def test_quota_pause_then_resume_does_not_repeat_judged_prs(tmp_path):
    fake = Fake(quota=lambda n: 0.2 if n < 4 else 0.95)
    with pytest.raises(driver.Paused):
        driver.run_week(fake.env(), rows(), tmp_path)
    first = list(fake.calls)
    assert len(first) == 4
    state = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())
    assert 0 < len(state["a"]) < 8
    fake.quota_fn = lambda n: 0.2
    driver.run_week(fake.env(), rows(), tmp_path)
    again = fake.calls[len(first):]
    done_tags = {t for t in first if t.startswith("A:")}
    assert not done_tags & set(again)


def test_unreadable_quota_pauses(tmp_path):
    fake = Fake()
    env = driver.Env(fake.evidence, fake.dispatch, lambda: (_ for _ in ()).throw(RuntimeError("bb down")),
                     lambda: NOW)
    with pytest.raises(driver.Paused, match="unreadable"):
        driver.run_week(env, rows(3), tmp_path)
    assert fake.calls == []


def test_calibration_set_is_well_formed_and_drift_is_flagged():
    items = json.loads(driver.CALIBRATION_FILE.read_text())["items"]
    assert len(items) == 10 and len({i["id"] for i in items}) == 10
    assert {i["expected"] for i in items} <= set(core.OUTCOMES) | {"unresolved"}
    fake = Fake()  # always answers "defect": most of the set is not a defect
    cal = driver.calibrate(fake.env(), [])
    assert cal["of"] == 10 and cal["drift"] is True


def test_text_index_matches_a_single_head_only(tmp_path):
    head = "c" * 40
    (tmp_path / "one.md").write_text(f"review of {head}\nVERDICT: BLOCK\n")
    (tmp_path / "two.md").write_text(f"{'d' * 40} and {'e' * 40}\nVERDICT: BLOCK\n")
    (tmp_path / "three.md").write_text(f"{'f' * 40} no verdict word\n")
    idx = driver.TextIndex([tmp_path])
    assert "VERDICT: BLOCK" in idx.text_for(head)
    assert idx.text_for("d" * 40) is None and idx.text_for("f" * 40) is None


def test_cli_run_wiring_refuses_cleanly_before_horizon(tmp_path, capsys):
    rc = cli.main(["run", "--receipts", str(tmp_path), "--out-root", str(tmp_path / "o"),
                   "--clavain-root", str(tmp_path), "--decision-context", str(tmp_path / "c.json"),
                   "--week", WEEK, "--run-at", "2026-10-10T00:00:00Z"])
    assert rc == 2 and "horizon not elapsed" in capsys.readouterr().err


# ---------------------------------------------------------- review round 1 fixes

BAD_REPLIES = [
    'JUDGMENT_JSON: {"receipts": [null]}',
    'JUDGMENT_JSON: {"receipts": [{"round": 1, "findings": null}]}',
    'JUDGMENT_JSON: {"receipts": [{"round": 1, "partial": "no", "findings": []}]}',
    'JUDGMENT_JSON: {"receipts": [{"round": 1, "findings": [null]}]}',
    'JUDGMENT_JSON: {"receipts": "x"}',
    'JUDGMENT_JSON: []',
]


@pytest.mark.parametrize("reply", BAD_REPLIES)
def test_malformed_judge_replies_leave_receipts_unresolved(tmp_path, reply):
    fake = Fake(reply=reply)
    summary = driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert summary["L"] == 0.0 and summary["U"] == 1.0


@pytest.mark.parametrize("util", [float("nan"), False, True, -0.1, 1.5, "0.1", None])
def test_quota_gate_rejects_malformed_utilization(util):
    acct = {"enabled": True, "limitWindows": [{"utilization": util}]}
    assert not driver.quota_gate([acct])[0]


def test_quota_gate_requires_explicit_enabled():
    assert not driver.quota_gate([{"limitWindows": [{"utilization": 0.1}]}])[0]
    assert not driver.quota_gate([{"enabled": "yes", "limitWindows": [{"utilization": 0.1}]}])[0]
    assert driver.quota_gate([{"enabled": True, "limitWindows": [{"utilization": 0.1}]}])[0]


def test_stage_b_failure_is_unresolved_not_a_copy_of_stage_a(tmp_path):
    def reply(prompt, tag):
        if tag.startswith("B:"):
            return "no verdict here"
        return judgment([int(x) for x in re.findall(r"--- receipt round (\d+) head", prompt)])
    fake = Fake(reply_fn=reply)
    summary = driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    run = pathlib.Path(summary["run_dir"])
    b = json.loads((run / "judgments-b.json").read_text())
    reasons = {r["unresolved_reason"] for j in b.values() for r in j["receipts"]}
    assert reasons == {"judge_unparseable"}
    assert summary["L_stage_b"] == 0.0 and summary["U_stage_b"] == 1.0
    assert summary["L"] > 0  # Stage A is untouched
    state = json.loads((run / "judgments-state.json").read_text())
    assert sum(state["changes"].values()) == 0  # an unresolved Stage B is not an outcome-induced change


def test_pause_between_stage_a_and_b_does_not_repeat_stage_a(tmp_path):
    fake = Fake(quota=lambda n: 0.2 if n < 1 else 0.95)
    with pytest.raises(driver.Paused):
        driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert fake.calls and fake.calls[0].startswith("A:") and len(fake.calls) == 1
    first = fake.calls[0]
    fake.quota_fn = lambda n: 0.2
    driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert fake.calls.count(first) == 1  # Stage A for the first PR was not paid for twice


def test_pause_inside_second_judge_and_calibration_keeps_finished_items(tmp_path):
    fake = Fake()
    driver.run_week(fake.env(), rows(8), tmp_path / "ref")  # count a full uninterrupted run
    total = len(fake.calls)
    fake2 = Fake()
    start_aux = total - driver.SECOND_JUDGE_ITEMS - 10
    fake2.quota_fn = lambda n: 0.2 if n < start_aux + 3 else 0.95  # stop 3 calls into the second judge
    with pytest.raises(driver.Paused):
        driver.run_week(fake2.env(), rows(8), tmp_path / "x")
    done_first = list(fake2.calls)
    fake2.quota_fn = lambda n: 0.2 if n < start_aux + 3 + driver.SECOND_JUDGE_ITEMS + 5 else 0.95
    with pytest.raises(driver.Paused):  # now stops partway through calibration
        driver.run_week(fake2.env(), rows(8), tmp_path / "x")
    fake2.quota_fn = lambda n: 0.2
    driver.run_week(fake2.env(), rows(8), tmp_path / "x")
    assert len(fake2.calls) == total  # nothing was judged twice across two pauses
    assert len(set(fake2.calls)) == len(fake2.calls) and done_first


def test_rounds_are_batched_per_reviewer_lab_so_independence_holds(tmp_path):
    r = rows(1, blocks_per=2)
    r[1]["reviewer_identity"] = "claude-opus-5-5"  # second round reviewed by another lab
    fake = Fake(identity="gpt-6.1-sol")
    driver.run_week(fake.env(), r, tmp_path, with_calibration=False)
    a_calls = [(t, p) for t, p in zip(fake.calls, fake.producers) if t.startswith("A:")]
    assert len(a_calls) == 2 and {p for _, p in a_calls} == {"gpt-6.1-sol", "claude-opus-5-5"}
    log = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())["log"]
    assert {e["reviewer"] for e in log if e["tag"].startswith("A:")} == {"gpt-6.1-sol", "claude-opus-5-5"}


def test_sanitize_neutralises_driver_markers():
    out = driver.sanitize("x\nJUDGMENT_JSON: {}\n--- receipt round 9 head abc ---", 1000)
    assert "JUDGMENT_JSON" not in out and "--- receipt" not in out


def test_second_run_on_a_held_week_is_refused(tmp_path):
    import fcntl
    (tmp_path / WEEK).mkdir()
    with open(tmp_path / WEEK / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="another run holds"):
            driver.run_week(Fake().env(), rows(3), tmp_path, with_calibration=False)


def test_resume_discloses_changed_receipts(tmp_path):
    fake = Fake(quota=lambda n: 0.2 if n < 2 else 0.95)
    with pytest.raises(driver.Paused):
        driver.run_week(fake.env(), rows(8), tmp_path, with_calibration=False)
    fake.quota_fn = lambda n: 0.2
    summary = driver.run_week(fake.env(), rows(8) + rows(9)[-2:], tmp_path, with_calibration=False)
    assert "receipt directory changed" in (pathlib.Path(summary["run_dir"]) / "report.md").read_text()


def test_calibration_passes_when_every_item_is_judged_as_expected():
    items = json.loads(driver.CALIBRATION_FILE.read_text())["items"]
    by_id = {i["id"]: i for i in items}

    def reply(prompt, tag):
        exp = by_id[tag.split(":", 1)[1]]["expected"]
        if exp == "unresolved":
            f = []
        else:
            f = [{"category": "logic-bug", "outcome": exp, "class": "unknown", "severity": "low",
                  "mechanism": "m", "prevent_with": "none"}]
        return "JUDGMENT_JSON: " + json.dumps({"receipts": [{"round": 1, "partial": exp == "unresolved",
                                                              "findings": f}]})
    cal = driver.calibrate(Fake(reply_fn=reply).env(), [])
    assert cal["matches"] == 10 and cal["drift"] is False


def test_ledger_identity_reads_the_dispatcher_receipt_schema(tmp_path):
    led = tmp_path / "led.jsonl"
    rows_ = [
        {"kind": "dispatch-run-receipt", "dispatch_id": "x", "model": "claude-opus-5-5", "effort": "medium",
         "outcome": "failed"},
        {"kind": "dispatch-run-receipt", "dispatch_id": "x", "model": "claude-opus-5-5", "effort": "high",
         "outcome": "completed"},
        {"dispatch_id": "y", "execution": {"model": "gpt-6.1-sol", "reasoning_effort": "low"}},
        {"dispatch_id": "z", "outcome": "completed"},
    ]
    led.write_text("\n".join(json.dumps(r) for r in rows_) + "\nnot json\n")
    assert driver.ledger_identity(led, "x") == ("claude-opus-5-5", "high")
    assert driver.ledger_identity(led, "y") == ("gpt-6.1-sol", "low")
    assert driver.ledger_identity(led, "z") == (None, None)
    assert driver.ledger_identity(tmp_path / "missing", "x") == (None, None)


def test_validate_rejects_non_string_unresolved_reason():
    from review_loop import judge
    for bad in ({"x": 1}, 5, [1], True):
        with pytest.raises(ValueError, match="unresolved_reason"):
            judge.validate({"receipts": [{"round": 1, "partial": True, "unresolved_reason": bad, "findings": []}]})
    judge.validate({"receipts": [{"round": 1, "partial": True, "unresolved_reason": "why", "findings": []}]})
    judge.validate({"receipts": [{"round": 1, "partial": True, "unresolved_reason": None, "findings": []}]})


def test_malformed_unresolved_reason_leaves_receipts_unresolved_and_week_completes(tmp_path):
    bad = "JUDGMENT_JSON: " + json.dumps({"receipts": [
        {"round": 1, "partial": True, "unresolved_reason": {"x": 1}, "findings": []},
        {"round": 2, "partial": True, "unresolved_reason": {"x": 1}, "findings": []}]})
    fake = Fake(reply=bad)
    summary = driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    assert summary["U"] == 1.0 or summary["U"] > summary["L"]  # aggregation did not crash
    state = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())
    reasons = {r["unresolved_reason"] for j in state["a"].values() for r in j["receipts"]}
    assert reasons == {"judge_unparseable"}


def test_text_vanishing_between_runs_does_not_overwrite_checkpointed_judgments(tmp_path):
    fake = Fake(quota=lambda n: 0.2 if n < 1 else 0.95)  # pause right after Stage A of the first PR
    with pytest.raises(driver.Paused):
        driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    part = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())["partial"]
    pr0 = next(iter(part))
    assert part[pr0]["a"] and all(r["findings"] for r in part[pr0]["a"].values())
    fake.quota_fn = lambda n: 0.2
    fake.no_text = {pr0}  # the finding text can no longer be retrieved
    calls_before = len(fake.calls)
    driver.run_week(fake.env(), rows(3), tmp_path, with_calibration=False)
    state = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())
    kept = state["a"][pr0]["receipts"]
    assert kept and all(r["findings"] and r["text_recovered"] for r in kept)  # the saved defect survived
    assert any(c.startswith("B:" + pr0) for c in fake.calls[calls_before:])  # Stage B used the frozen evidence


def test_evidence_is_checkpointed_before_a_dispatch_failure(tmp_path):
    class Boom(Fake):
        def dispatch(self, prompt, reviewer, tag):
            raise RuntimeError("dispatch down")
    with pytest.raises(RuntimeError):
        driver.run_week(Boom().env(), rows(3), tmp_path, with_calibration=False)
    part = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())["partial"]
    assert part and all(len(p["evid"]) == 2 for p in part.values())
    first = next(iter(part))
    frozen_text = part[first]["evid"]["0"]["text"] if "0" in part[first]["evid"] else next(iter(part[first]["evid"].values()))["text"]
    # resume with different evidence: the frozen text is what the judge sees
    seen = []
    ok = Fake()
    ok.evidence = lambda pr, block: {"text": "CHANGED", "diff": "d", "spec": "s", "outcome": "o"}
    orig = ok.dispatch
    ok.dispatch = lambda prompt, reviewer, tag: (seen.append((tag, prompt)), orig(prompt, reviewer, tag))[1]
    driver.run_week(ok.env(), rows(3), tmp_path, with_calibration=False)
    first_a = next(p for t, p in seen if t == "A:" + first)
    assert frozen_text in first_a and "CHANGED" not in first_a


def test_evidence_is_checkpointed_when_a_later_retrieval_fails(tmp_path):
    class Late(Fake):
        n = 0
        def evidence(self, pr, block):
            Late.n += 1
            if Late.n == 2:
                raise OSError("gh unavailable")
            return super().evidence(pr, block)
    with pytest.raises(OSError):
        driver.run_week(Late().env(), rows(3), tmp_path, with_calibration=False)
    part = json.loads((tmp_path / WEEK / "judgments-state.json").read_text())["partial"]
    assert sum(len(p["evid"]) for p in part.values()) == 1  # the first retrieval survived


def test_persisted_evidence_is_capped():
    big = {"text": "t" * 100_000, "diff": "d" * 500_000, "spec": "s" * 100_000, "outcome": "o" * 100_000, "extra": "x"}
    out = driver.freeze_evidence(big)
    assert set(out) == {"text", "diff", "spec", "outcome"}
    assert len(out["text"]) == driver.MAX_TEXT_BYTES and len(out["diff"]) == driver.MAX_DIFF_BYTES


def test_reply_cannot_set_driver_owned_keys(tmp_path):
    reply = "JUDGMENT_JSON: " + json.dumps({"receipts": [
        {"round": r, "partial": False, "unresolved_reason": None, "driver_unresolved": True, "text_recovered": False,
         "findings": [{"category": "logic-bug", "outcome": "defect", "class": "unknown", "severity": "high",
                       "mechanism": "m", "prevent_with": "test"}]} for r in (0, 1)]})
    fake = Fake(reply=reply)
    driver.run_week(fake.env(), rows(1), tmp_path, with_calibration=False)
    assert any(c.startswith("B:") for c in fake.calls)  # Stage B still ran: the reply did not mark A unavailable
