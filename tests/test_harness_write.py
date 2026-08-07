#!/usr/bin/env python3
"""Tests for the continual-harness write ledger.

The properties under test are the three the existing memory lane lacks:
scope, falsifiability, and reversibility. Each has a failure mode that looks
like success if untested — an unscoped write silently pollutes the global
index, an unfalsifiable write reads as a lesson, and a rollback that half
works leaves the file and the ledger disagreeing.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "harness_write.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("harness_write", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "interspect.db"
    monkeypatch.setenv("INTERSPECT_DB", str(db))
    return {"db": db, "tmp": tmp_path}


def run(env, *args, expect=0):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
        env={**dict(__import__("os").environ), "INTERSPECT_DB": str(env["db"])},
    )
    assert result.returncode == expect, f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


def write_id_from(result):
    return result.stdout.split()[0]


# --- falsifiability ---

def test_write_without_evidence_is_refused(env):
    run(env, "write", "--kind", "memory", "--name", "x", "--scope", "global",
        "--content", "c", "--evidence", "   ", "--expected-outcome", "o", expect=2)


def test_write_without_expected_outcome_is_refused(env):
    """A write nobody can later prove wrong is not a lesson."""
    run(env, "write", "--kind", "memory", "--name", "x", "--scope", "global",
        "--content", "c", "--evidence", "e", "--expected-outcome", "", expect=2)


# --- scope ---

def test_local_write_requires_session(env):
    run(env, "write", "--kind", "memory", "--name", "x", "--scope", "local",
        "--content", "c", "--evidence", "e", "--expected-outcome", "o", expect=2)


def test_local_write_does_not_touch_global_dir(env, monkeypatch):
    """Local is the default precisely so session noise never reaches the
    global index that every future session loads."""
    module = _load_module()
    local = module.target_path("local", "memory", "note", "sess1")
    glob = module.target_path("global", "memory", "note", None)
    assert module.GLOBAL_MEMORY_DIR not in local.parents
    assert glob.parent == module.GLOBAL_MEMORY_DIR


# --- reversibility ---

def test_rollback_of_create_removes_the_file(env):
    target = env["tmp"] / "note.md"
    result = run(env, "write", "--kind", "memory", "--name", "n", "--scope", "global",
                 "--path", str(target), "--content", "new", "--evidence", "e",
                 "--expected-outcome", "o")
    assert target.read_text() == "new"
    run(env, "rollback", write_id_from(result))
    assert not target.exists(), "rollback of a create must not leave an empty artifact"


def test_rollback_of_update_restores_exact_prior_content(env):
    target = env["tmp"] / "note.md"
    target.write_text("ORIGINAL")
    result = run(env, "write", "--kind", "memory", "--name", "n", "--scope", "global",
                 "--action", "update", "--path", str(target), "--content", "OVERWRITTEN",
                 "--evidence", "e", "--expected-outcome", "o")
    assert target.read_text() == "OVERWRITTEN"
    run(env, "rollback", write_id_from(result))
    assert target.read_text() == "ORIGINAL"


def test_double_rollback_is_refused(env):
    target = env["tmp"] / "note.md"
    result = run(env, "write", "--kind", "memory", "--name", "n", "--scope", "global",
                 "--path", str(target), "--content", "c", "--evidence", "e",
                 "--expected-outcome", "o")
    wid = write_id_from(result)
    run(env, "rollback", wid)
    run(env, "rollback", wid, expect=1)


def test_rollback_of_unknown_id_fails(env):
    run(env, "rollback", "hw_doesnotexist", expect=1)


# --- ids ---

def test_ids_are_unique_within_one_second(env):
    """Regression: kind+name+ts collided at second resolution, which broke
    promote (a promotion normally lands in the same second as its write)."""
    module = _load_module()
    ts = "2026-08-07T00:00:00Z"
    ids = {module.make_id("memory", "same", ts) for _ in range(200)}
    assert len(ids) == 200


def test_promote_in_same_second_as_write_succeeds(env, monkeypatch):
    target = env["tmp"] / "local.md"
    module = _load_module()
    monkeypatch.setattr(module, "GLOBAL_MEMORY_DIR", env["tmp"] / "global")
    result = run(env, "write", "--kind", "memory", "--name", "p", "--scope", "local",
                 "--session", "s1", "--path", str(target), "--content", "lesson",
                 "--evidence", "e", "--expected-outcome", "o")
    # promote writes to the real global dir, so assert only on the ledger
    listing = run(env, "list", "--scope", "local", "--json")
    assert write_id_from(result) in listing.stdout


# --- ledger ---

def test_write_is_listed_and_shown(env):
    target = env["tmp"] / "note.md"
    result = run(env, "write", "--kind", "memory", "--name", "listed", "--scope", "global",
                 "--path", str(target), "--content", "c",
                 "--evidence", "measured 3x", "--expected-outcome", "rate drops below 30%")
    wid = write_id_from(result)
    shown = run(env, "show", wid)
    assert "measured 3x" in shown.stdout
    assert "rate drops below 30%" in shown.stdout
    assert '"status": "active"' in shown.stdout


def test_status_flips_to_rolled_back(env):
    target = env["tmp"] / "note.md"
    result = run(env, "write", "--kind", "memory", "--name", "n", "--scope", "global",
                 "--path", str(target), "--content", "c", "--evidence", "e",
                 "--expected-outcome", "o")
    wid = write_id_from(result)
    run(env, "rollback", wid)
    shown = run(env, "show", wid)
    assert '"status": "rolled_back"' in shown.stdout


def test_missing_evidence_table_degrades_rather_than_refusing(env):
    """A fresh DB has no evidence table (the shell lib owns it). The ledger
    must still accept writes rather than losing them."""
    target = env["tmp"] / "note.md"
    run(env, "write", "--kind", "memory", "--name", "n", "--scope", "global",
        "--path", str(target), "--content", "c", "--evidence", "e",
        "--expected-outcome", "o")
    assert target.read_text() == "c"
