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
                 reply=None):
        self.calls, self.identity, self.no_text, self.quota_fn = [], identity, set(no_text), quota
        self.stage_b_outcome, self.reply = stage_b_outcome, reply

    def evidence(self, pr, block):
        return {"text": None if pr in self.no_text else f"finding for {pr} round {block['round']}",
                "diff": "```diff\n+x\n```", "spec": "spec", "outcome": "merged"}

    def dispatch(self, prompt, reviewer, tag):
        self.calls.append(tag)
        if self.reply is not None:
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
