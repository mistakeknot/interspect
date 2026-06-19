#!/usr/bin/env python3
"""collect_tokens: marginal token efficiency of a skill vs baseline (7aj8.3).

For each skill invocation's session, compare that session's token cost against
a counterfactual baseline: the median token cost of comparable sessions in the
same project that did NOT run this skill, within ±7 days. Lower-than-baseline
cost scores high.

  value = 1 - sigmoid((session_tokens - baseline) / (baseline_std or 1))

This signal is BEST-EFFORT and degrades gracefully. It is gated on the ``cass``
binary being available (session intelligence backend): if ``cass`` is not on
PATH or returns nothing, ALL rows are skipped (write nothing, log "cass
unavailable"). We use ``cass timeline --json`` to enumerate candidate sessions
+ their transcript paths per workspace, then sum per-session token cost directly
from each transcript's ``message.usage`` fields (input + output + cache) — the
same source cass indexes — because cass exposes no single-session token total.

Which sessions ran this skill is read from the interspect DB itself (skill
evidence ``session_id`` set), so a session is excluded from a skill's own
baseline cohort.

signal_kind: ``tokens``  (1.0 = cheaper than comparable no-skill sessions)

Usage:
  collect_tokens.py [--db <path>] [--dry-run] [--limit N] [--repo-root .]
                    [--window-days 7] [--min-cohort 3]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    default_db_path,
    find_repo_root,
    log,
    pending_evidence,
    write_signal,
)

SIGNAL_KIND = "tokens"


def _parse_ts_ms(value: str | None) -> int | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _sigmoid(x: float) -> float:
    # Numerically stable logistic.
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ─── cass session enumeration ────────────────────────────────────────────────


def cass_available() -> bool:
    return shutil.which("cass") is not None


def cass_timeline(since_days: int) -> list[dict] | None:
    """Run ``cass timeline --since Nd --json`` → flat list of session dicts.

    Returns None if cass errors or yields nothing parseable.
    """
    try:
        proc = subprocess.run(
            ["cass", "timeline", "--since", f"{since_days}d", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    sessions: list[dict] = []
    for items in (obj.get("groups") or {}).values():
        for it in items:
            if isinstance(it, dict):
                sessions.append(it)
    return sessions or None


# ─── Per-session token cost ──────────────────────────────────────────────────


def _usage_tokens(usage: dict) -> int:
    """Marginal API token cost for one assistant turn.

    Deliberately EXCLUDES ``cache_read_input_tokens``: cache reads re-count the
    same cached prefix on every turn, so they scale with conversation length
    (turn count) rather than with marginal work, and are billed at ~0.1x. Summing
    them across a long session inflates the total ~10-12x and swamps the real
    cost difference between skill / no-skill sessions. Marginal cost =
    ``input + output + cache_creation`` (the one-time prompt write + generation).
    """
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def transcript_tokens(path: Path) -> int | None:
    """Sum API token cost across all assistant ``usage`` blocks. None if missing."""
    if not path.is_file():
        return None
    total = 0
    found = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if usage:
                    total += _usage_tokens(usage)
                    found = True
    except OSError:
        return None
    return total if found else None


def session_id_from_path(source_path: str) -> str:
    """Top-level transcript filename stem == session_id. Subagent files differ."""
    return Path(source_path).stem


def workspace_from_path(source_path: str) -> str:
    """Decode the workspace from a ``~/.claude/projects/<encoded>/...`` path.

    Claude encodes the workspace as the parent dir with '/' → '-' and a leading
    '-'. We only need a stable per-project key, so return the encoded dir name.
    """
    p = Path(source_path)
    # .../projects/<encoded>/<session>.jsonl  OR  .../<encoded>/<session>/subagents/..
    parts = p.parts
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return p.parent.name


def encode_workspace(project_path: str) -> str:
    """Encode a repo path the way Claude names its projects dir."""
    return "-" + project_path.strip("/").replace("/", "-")


# ─── Collector ───────────────────────────────────────────────────────────────


def collect(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    limit: int | None,
    window_days: int,
    min_cohort: int,
) -> dict[str, int]:
    stats = {
        "pending": 0,
        "written": 0,
        "skipped_no_session_tokens": 0,
        "skipped_no_cohort": 0,
        "skipped_dup": 0,
        "cass_unavailable": 0,
    }

    if not cass_available():
        # Count all pending as skipped-for-cass so the report is honest.
        pend = pending_evidence(conn, SIGNAL_KIND, limit)
        stats["pending"] = len(pend)
        stats["cass_unavailable"] = len(pend)
        log("collect_tokens: cass unavailable — skipping all rows")
        return stats

    pending = pending_evidence(conn, SIGNAL_KIND, limit)
    stats["pending"] = len(pending)
    if not pending:
        return stats

    # Enumerate sessions over a generous window covering all pending invocations.
    sessions = cass_timeline(max(window_days * 2, 30))
    if not sessions:
        # cass returned nothing — degrade gracefully (treat as unavailable).
        stats["cass_unavailable"] = len(pending)
        log("collect_tokens: cass returned no sessions — skipping all rows")
        return stats

    # Index sessions: session_id → (workspace_key, started_ms, token_cost).
    sess_index: dict[str, tuple[str, int, int]] = {}
    for s in sessions:
        sp = s.get("source_path")
        if not isinstance(sp, str):
            continue
        if "/subagents/" in sp:
            continue  # only top-level sessions
        sid = session_id_from_path(sp)
        started = s.get("started_at")
        if not isinstance(started, int):
            continue
        toks = transcript_tokens(Path(sp))
        if toks is None:
            continue
        sess_index[sid] = (workspace_from_path(sp), started, toks)

    # Which sessions ran this skill (from interspect evidence) — to exclude from
    # the skill's own baseline cohort.
    def sessions_running(skill_name: str) -> set[str]:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM evidence "
            "WHERE source_kind='skill' AND source = ?",
            (skill_name,),
        ).fetchall()
        return {r[0] for r in rows}

    skill_sessions_cache: dict[str, set[str]] = {}
    window_ms = window_days * 86400 * 1000

    for ev in pending:
        sid = ev.session_id
        if sid not in sess_index:
            stats["skipped_no_session_tokens"] += 1
            continue
        ws_key, started_ms, sess_tokens = sess_index[sid]

        # Baseline cohort: same workspace, within ±window, that did NOT run this
        # skill. Prefer matching the evidence project's encoded workspace; fall
        # back to the invocation session's own workspace key.
        target_ws = ws_key
        if ev.project:
            enc = encode_workspace(ev.project)
            # Only override if some session actually uses that workspace key.
            if any(v[0] == enc for v in sess_index.values()):
                target_ws = enc

        if ev.skill_name not in skill_sessions_cache:
            skill_sessions_cache[ev.skill_name] = sessions_running(ev.skill_name)
        ran_skill = skill_sessions_cache[ev.skill_name]

        cohort = [
            tok
            for csid, (cws, cstart, tok) in sess_index.items()
            if cws == target_ws
            and csid not in ran_skill
            and abs(cstart - started_ms) <= window_ms
        ]
        if len(cohort) < min_cohort:
            stats["skipped_no_cohort"] += 1
            continue

        baseline = statistics.median(cohort)
        baseline_std = statistics.pstdev(cohort) if len(cohort) > 1 else 0.0
        denom = baseline_std if baseline_std > 0 else 1.0
        value = 1.0 - _sigmoid((sess_tokens - baseline) / denom)
        value = max(0.0, min(1.0, value))

        meta = json.dumps(
            {
                "session_tokens": sess_tokens,
                "baseline_median": round(baseline, 1),
                "baseline_std": round(baseline_std, 1),
                "cohort_n": len(cohort),
            }
        )
        if dry_run:
            stats["written"] += 1
            continue
        if write_signal(
            conn, ev, SIGNAL_KIND, value, raw_value=float(sess_tokens), metadata=meta
        ):
            stats["written"] += 1
        else:
            stats["skipped_dup"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="Override interspect.db path")
    ap.add_argument("--dry-run", action="store_true", help="Project counts, no writes")
    ap.add_argument("--limit", type=int, default=None, help="Max pending rows to process")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--window-days", type=int, default=7, help="Baseline cohort ± window")
    ap.add_argument("--min-cohort", type=int, default=3, help="Min comparable sessions")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)
    if not db_path.exists():
        log(f"collect_tokens: DB not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        stats = collect(
            conn,
            dry_run=args.dry_run,
            limit=args.limit,
            window_days=args.window_days,
            min_cohort=args.min_cohort,
        )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    log(
        f"collect_tokens: pending={stats['pending']} written={stats['written']} "
        f"skipped_no_session_tokens={stats['skipped_no_session_tokens']} "
        f"skipped_no_cohort={stats['skipped_no_cohort']} "
        f"cass_unavailable={stats['cass_unavailable']}"
        + ("  (dry-run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
