"""Opt-in external evidence storage must preserve the subject repository."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def resolve(project, override=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    env.pop("INTERSPECT_PRIVATE_DB", None)
    if override is not None:
        env["INTERSPECT_PRIVATE_DB"] = str(override)
    return subprocess.run(
        ["bash", "-c", 'source "$1"; _interspect_db_path', "test", str(ROOT / "hooks/lib-interspect.sh")],
        env=env, cwd=project, capture_output=True, text=True,
    )


def tree(root):
    return {str(p.relative_to(root)): ("link", os.readlink(p)) if p.is_symlink() else ("file", p.read_bytes()) if p.is_file() else ("dir", None)
            for p in root.rglob("*")}


def test_default_remains_project_local(tmp_path):
    assert resolve(tmp_path).stdout.strip() == str(tmp_path / ".clavain/interspect/interspect.db")


def test_private_override_records_real_session_without_project_writes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    db = state / "interspect.db"
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), INTERSPECT_PRIVATE_DB=str(db))
    p = subprocess.run(["bash", str(ROOT / "hooks/interspect-session.sh")],
                       input=json.dumps({"session_id": "private-state-test"}), text=True,
                       capture_output=True, cwd=project, env=env, timeout=30)
    assert p.returncode == 0
    assert list(project.iterdir()) == []
    assert db.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("private-state-test",)]
        assert connection.execute("SELECT project FROM sessions").fetchone()[0] == project.name
    # The scorer runs asynchronously; its atomically written output is the
    # completion boundary. Observe it before asserting repository preservation.
    for _ in range(100):
        if (state / "routing-calibration.json").exists():
            break
        time.sleep(0.05)
    assert (state / "routing-calibration.json").exists()
    p = subprocess.run(["bash", str(ROOT / "hooks/interspect-session-end.sh")],
                       input=json.dumps({"session_id": "private-state-test"}), text=True,
                       capture_output=True, cwd=project, env=env, timeout=30)
    assert p.returncode == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT end_ts FROM sessions").fetchone()[0]
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("kind", ["empty", "relative", "trailing-slash", "trailing-dot", "newline", "missing-parent", "public-parent", "link", "directory", "hardlink", "parent-link", "wal-link", "shm-link", "journal-link"])
def test_invalid_override_fails_without_project_fallback(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "db"
    if kind == "empty":
        path = ""
    elif kind == "relative":
        path = Path("db")
    elif kind == "trailing-slash":
        path = str(path) + "/"
    elif kind == "trailing-dot":
        path = str(path) + "/."
    elif kind == "newline":
        path = str(path) + "\n"
    elif kind == "missing-parent":
        path = state / "absent/db"
    elif kind == "public-parent":
        state.chmod(0o755)
    elif kind == "link":
        target = state / "target"
        target.touch()
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "hardlink":
        target = state / "target"
        target.touch()
        os.link(target, path)
    elif kind == "parent-link":
        link = tmp_path / "link"
        link.symlink_to(state, target_is_directory=True)
        path = link / "db"
    elif kind in ("wal-link", "shm-link", "journal-link"):
        target = state / "target"
        target.touch()
        Path(str(path) + "-" + kind.split("-")[0]).symlink_to(target)
    before = tree(tmp_path)
    result = resolve(tmp_path, path)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not (tmp_path / ".clavain").exists()
    for name in ("interspect-session", "interspect-session-end", "interspect-evidence"):
        hook = subprocess.run(["bash", str(ROOT / "hooks" / (name + ".sh"))],
                              input='{"session_id":"invalid-path","tool_name":"Task"}', text=True,
                              capture_output=True, cwd=tmp_path,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path), INTERSPECT_PRIVATE_DB=str(path)), timeout=10)
        assert hook.returncode == 0
        assert tree(tmp_path) == before


def test_default_resolver_needs_no_python(tmp_path):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path), PATH=str(tmp_path))
    env.pop("INTERSPECT_PRIVATE_DB", None)
    result = subprocess.run(["/bin/bash", "-c", 'source "$1"; _interspect_db_path', "test", str(ROOT / "hooks/lib-interspect.sh")],
                            env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path / ".clavain/interspect/interspect.db")


def test_private_evidence_does_not_invoke_project_calibrators(tmp_path):
    (tmp_path / "db").touch()
    script = '''source "$1"
clavain-cli() { touch "$MARKER"; }
_interspect_review_calibration_ready() { touch "$MARKER"; }
_interspect_decomposition_calibration_ready() { touch "$MARKER"; }
sqlite3() { touch "$MARKER"; return 1; }
_interspect_auto_calibrate
_interspect_calibrate_reviews
'''
    p = subprocess.run(["bash", "-c", script, "test", str(ROOT / "hooks/lib-interspect.sh")],
                       env=dict(os.environ, INTERSPECT_PRIVATE_DB=str(tmp_path / "db"), MARKER=str(tmp_path / "forbidden")), capture_output=True)
    assert p.returncode == 1
    assert not (tmp_path / "forbidden").exists()
