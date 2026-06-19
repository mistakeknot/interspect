#!/usr/bin/env python3
"""collect_no_redirect: did the user redirect right after a skill ran? (7aj8.3)

Locate the session transcript (``~/.claude/projects/*/<session_id>.jsonl``),
find the skill invocation, and inspect the next up-to-5 USER turns for
redirect markers (course-corrections like "wait", "stop", "actually", "/clear").
More redirects → lower score.

  value = 1 - min(1.0, redirect_markers / turns_examined)

If the session transcript can't be found, SKIP (pending — a later run may find
it once the session is flushed/indexed). If the transcript is found but the
specific skill invocation can't be located, we still score against the user
turns that follow the invocation timestamp (ts-based fallback); if neither the
skill block nor any subsequent user turn is found, SKIP.

signal_kind: ``no_redirect``  (1.0 = no redirect, clean follow-through)

Usage:
  collect_no_redirect.py [--db <path>] [--dry-run] [--limit N]
                         [--repo-root .] [--projects-dir ~/.claude/projects]
                         [--max-turns 5]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    default_db_path,
    find_repo_root,
    log,
    pending_evidence,
    write_signal,
)

SIGNAL_KIND = "no_redirect"
DEFAULT_PROJECTS_DIR = "~/.claude/projects"

# Case-insensitive, word-boundary redirect markers. Each pattern matched once
# per user turn (presence, not frequency) so a turn contributes at most 1.
REDIRECT_PATTERNS = [
    re.compile(r"/clear\b", re.IGNORECASE),
    re.compile(r"\bwait\b", re.IGNORECASE),
    re.compile(r"\bstop\b", re.IGNORECASE),
    re.compile(r"\bactually\b", re.IGNORECASE),
    re.compile(r"\binstead\b", re.IGNORECASE),
    re.compile(r"\bredo\b", re.IGNORECASE),
    re.compile(r"that's wrong", re.IGNORECASE),
    re.compile(r"\bdon't\b", re.IGNORECASE),
    re.compile(r"\bundo\b", re.IGNORECASE),
    re.compile(r"\bno,", re.IGNORECASE),
]


# ─── Transcript location ─────────────────────────────────────────────────────


def find_transcript(session_id: str, projects_dir: Path) -> Path | None:
    """Find ``<projects_dir>/*/<session_id>.jsonl``. First match wins."""
    if not session_id or not projects_dir.is_dir():
        return None
    for match in projects_dir.glob(f"*/{session_id}.jsonl"):
        if match.is_file():
            return match
    return None


# ─── Transcript text extraction ──────────────────────────────────────────────


def _user_text(message: object) -> str | None:
    """Extract plain text from a user message's ``content`` (str or block list).

    Returns None for non-text user turns (e.g. tool_result-only turns), which
    are not real user redirections and should be ignored.
    """
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    # tool_result turns are not user redirections.
                    return None
        return "\n".join(parts) if parts else None
    return None


def count_redirects(text: str) -> int:
    return sum(1 for pat in REDIRECT_PATTERNS if pat.search(text))


def score_followup(
    transcript: Path, skill_name: str, invocation_ts: str, max_turns: int
) -> float | None:
    """Score the user turns following the skill invocation. None → SKIP.

    Strategy: stream the transcript, locate the assistant ``tool_use`` block for
    this skill (``name == 'Skill'`` and ``input.skill == skill_name``). After it,
    collect up to ``max_turns`` genuine user text turns and count redirect
    markers. Fall back to the invocation ts if the block isn't found.
    """
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # 1) Find the invocation index via the Skill tool_use block.
    start_idx = None
    for i, obj in enumerate(records):
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        for block in msg.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Skill"
            ):
                inp = block.get("input") or {}
                if inp.get("skill") == skill_name:
                    start_idx = i
                    break
        if start_idx is not None:
            break

    # 2) Fallback: first record whose timestamp is >= the invocation ts.
    if start_idx is None and invocation_ts:
        for i, obj in enumerate(records):
            ts = obj.get("timestamp")
            if isinstance(ts, str) and ts >= invocation_ts:
                start_idx = i
                break

    if start_idx is None:
        return None

    # 3) Examine the next user text turns after the invocation.
    turns_examined = 0
    redirect_markers = 0
    for obj in records[start_idx + 1 :]:
        if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
            continue
        text = _user_text(obj.get("message"))
        if text is None:
            continue
        turns_examined += 1
        redirect_markers += count_redirects(text)
        if turns_examined >= max_turns:
            break

    if turns_examined == 0:
        # No user turn followed (e.g. session ended on the skill). Treat as a
        # clean follow-through rather than skipping forever.
        return 1.0

    return 1.0 - min(1.0, redirect_markers / turns_examined)


# ─── Collector ───────────────────────────────────────────────────────────────


def collect(
    conn: sqlite3.Connection,
    projects_dir: Path,
    *,
    dry_run: bool,
    limit: int | None,
    max_turns: int,
) -> dict[str, int]:
    stats = {
        "pending": 0,
        "written": 0,
        "skipped_no_transcript": 0,
        "skipped_no_anchor": 0,
        "skipped_dup": 0,
    }
    pending = pending_evidence(conn, SIGNAL_KIND, limit)
    stats["pending"] = len(pending)

    for ev in pending:
        transcript = find_transcript(ev.session_id, projects_dir)
        if transcript is None:
            stats["skipped_no_transcript"] += 1
            continue
        value = score_followup(transcript, ev.skill_name, ev.ts, max_turns)
        if value is None:
            stats["skipped_no_anchor"] += 1
            continue
        raw = round(1.0 - value, 6)  # raw_value = redirect fraction
        if dry_run:
            stats["written"] += 1
            continue
        if write_signal(conn, ev, SIGNAL_KIND, value, raw_value=raw):
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
    ap.add_argument(
        "--projects-dir",
        default=DEFAULT_PROJECTS_DIR,
        help="Claude projects dir holding session transcripts",
    )
    ap.add_argument("--max-turns", type=int, default=5, help="User turns to examine")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)
    if not db_path.exists():
        log(f"collect_no_redirect: DB not found at {db_path}")
        return 1
    projects_dir = Path(os.path.expanduser(args.projects_dir))

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        stats = collect(
            conn,
            projects_dir,
            dry_run=args.dry_run,
            limit=args.limit,
            max_turns=args.max_turns,
        )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    log(
        f"collect_no_redirect: pending={stats['pending']} written={stats['written']} "
        f"skipped_no_transcript={stats['skipped_no_transcript']} "
        f"skipped_no_anchor={stats['skipped_no_anchor']}"
        + ("  (dry-run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
