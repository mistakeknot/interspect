#!/usr/bin/env python3
"""collect_bead_close: did the bead a skill was working on get closed? (7aj8.3)

For each pending skill invocation, attribute it to the bead that was
``in_progress`` at invocation time, then score by that bead's eventual fate.

signal_kind: ``bead_close``
  - bead reached ``closed`` / ``resolved`` within 7d of the invocation → 1.0
  - bead ``deferred`` / rejected within 7d                            → 0.0
  - bead still open / in_progress (no terminal state yet)             → SKIP
    (write nothing; a later run re-evaluates once the bead resolves)

We read ``.beads/issues.jsonl`` DIRECTLY (one JSON object per line) — never the
``bd`` CLI. Beads carry no ``project`` field, so the project is the
``.beads/issues.jsonl`` *file* we read; we resolve it from the evidence row's
``project`` path (a repo path written by the tool-time adapter). The session DB
itself usually lives in the same project, so we also fall back to the repo root.

═══ Matching heuristic (necessarily approximate — stays conservative) ═════════

Beads expose timestamps but NOT a full status-transition history, so we cannot
know with certainty which bead was in_progress at an arbitrary past instant. We
approximate the "active bead" as:

  the bead whose ``started_at`` is the LATEST started_at <= invocation_ts,
  among beads in this project, that ALSO had not terminated before the
  invocation (closed_at, if present, is after the invocation).

Rationale: ``started_at`` marks entry into in_progress; the most-recently-
started not-yet-closed bead is the best single guess for "what was being worked
on". We deliberately SKIP rather than guess when:

  - no ``.beads/issues.jsonl`` is found for the project (pending),
  - no bead has ``started_at <= invocation_ts`` (nothing was active; pending),
  - two or more candidate beads share the same latest ``started_at`` within a
    tie window (ambiguous → skip), or
  - the attributed bead is still non-terminal at scan time (pending).

Skipping is safe: pending rows are simply re-examined on the next run, when more
history is available. This trades coverage for precision on purpose — a wrong
attribution would poison the skill's score, whereas a skip costs nothing.

Usage:
  collect_bead_close.py [--db <path>] [--dry-run] [--limit N]
                        [--repo-root .] [--window-days 7] [--tie-window-s 60]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    default_db_path,
    find_repo_root,
    log,
    pending_evidence,
    write_signal,
)

SIGNAL_KIND = "bead_close"

CLOSED_STATES = {"closed", "resolved", "done", "completed"}
REJECTED_STATES = {"deferred", "rejected", "wontfix", "cancelled", "canceled"}


# ─── Bead model ──────────────────────────────────────────────────────────────


class Bead(NamedTuple):
    id: str
    status: str
    started_at: datetime | None
    closed_at: datetime | None
    updated_at: datetime | None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_beads(beads_path: Path) -> list[Bead]:
    """Parse .beads/issues.jsonl into Bead rows. Missing file → empty list."""
    beads: list[Bead] = []
    if not beads_path.is_file():
        return beads
    with beads_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = obj.get("id")
            status = obj.get("status")
            if not bid or not status:
                continue
            beads.append(
                Bead(
                    id=bid,
                    status=status,
                    started_at=_parse_ts(obj.get("started_at")),
                    closed_at=_parse_ts(obj.get("closed_at")),
                    updated_at=_parse_ts(obj.get("updated_at")),
                )
            )
    return beads


# ─── .beads path resolution ──────────────────────────────────────────────────


def resolve_beads_path(project: str, repo_root: Path) -> Path | None:
    """Find a project's .beads/issues.jsonl.

    The evidence ``project`` is a repo path string. Try it first, then the
    interspect repo_root (sessions usually run in-project). Returns the first
    existing path, else None.
    """
    candidates: list[Path] = []
    if project:
        candidates.append(Path(os.path.expanduser(project)) / ".beads" / "issues.jsonl")
    candidates.append(repo_root / ".beads" / "issues.jsonl")
    for c in candidates:
        if c.is_file():
            return c
    return None


# ─── Attribution heuristic ───────────────────────────────────────────────────


def attribute_bead(
    beads: list[Bead], invocation_ts: datetime, tie_window_s: int
) -> Bead | None:
    """Pick the bead in_progress at invocation_ts, or None if ambiguous/none.

    Candidate = started_at <= invocation_ts AND not already closed before then.
    Winner = the candidate with the LATEST started_at. If two candidates'
    started_at are within ``tie_window_s`` of each other, the attribution is
    ambiguous → return None (skip).
    """
    candidates = [
        b
        for b in beads
        if b.started_at is not None
        and b.started_at <= invocation_ts
        and (b.closed_at is None or b.closed_at > invocation_ts)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda b: b.started_at, reverse=True)  # type: ignore[arg-type,return-value]
    winner = candidates[0]
    if len(candidates) > 1:
        runner_up = candidates[1]
        gap = (winner.started_at - runner_up.started_at).total_seconds()  # type: ignore[operator]
        if abs(gap) <= tie_window_s:
            return None  # ambiguous
    return winner


def score_bead(
    bead: Bead, invocation_ts: datetime, window_days: int
) -> tuple[float, float] | None:
    """Map a bead's fate to (value, raw_value), or None to SKIP (still pending).

    Terminal-within-window:
      closed/resolved → (1.0, 1.0); deferred/rejected → (0.0, 0.0).
    Non-terminal, or terminal but outside the 7d window → SKIP.
    """
    horizon = invocation_ts + timedelta(days=window_days)
    status = bead.status.lower()

    if status in CLOSED_STATES:
        # Require the close to land inside the window after the invocation.
        if bead.closed_at is not None and bead.closed_at > horizon:
            return None
        return (1.0, 1.0)
    if status in REJECTED_STATES:
        ref = bead.closed_at or bead.updated_at
        if ref is not None and ref > horizon:
            return None
        return (0.0, 0.0)
    # open / in_progress / blocked → no terminal state yet.
    return None


# ─── Collector ───────────────────────────────────────────────────────────────


def collect(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    dry_run: bool,
    limit: int | None,
    window_days: int,
    tie_window_s: int,
) -> dict[str, int]:
    stats = {
        "pending": 0,
        "written": 0,
        "skipped_no_beads": 0,
        "skipped_no_match": 0,
        "skipped_unresolved": 0,
        "skipped_dup": 0,
    }
    pending = pending_evidence(conn, SIGNAL_KIND, limit)
    stats["pending"] = len(pending)

    # Cache parsed beads per resolved file (many invocations share a project).
    beads_cache: dict[str, list[Bead]] = {}

    for ev in pending:
        beads_path = resolve_beads_path(ev.project, repo_root)
        if beads_path is None:
            stats["skipped_no_beads"] += 1
            continue
        key = str(beads_path)
        if key not in beads_cache:
            beads_cache[key] = load_beads(beads_path)
        beads = beads_cache[key]

        inv_ts = _parse_ts(ev.ts)
        if inv_ts is None:
            stats["skipped_no_match"] += 1
            continue

        bead = attribute_bead(beads, inv_ts, tie_window_s)
        if bead is None:
            stats["skipped_no_match"] += 1
            continue

        scored = score_bead(bead, inv_ts, window_days)
        if scored is None:
            stats["skipped_unresolved"] += 1
            continue

        value, raw = scored
        meta = json.dumps({"bead": bead.id, "bead_status": bead.status})
        if dry_run:
            stats["written"] += 1
            continue
        if write_signal(conn, ev, SIGNAL_KIND, value, raw_value=raw, metadata=meta):
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
    ap.add_argument("--window-days", type=int, default=7, help="Close window after invocation")
    ap.add_argument(
        "--tie-window-s",
        type=int,
        default=60,
        help="started_at gap (s) below which attribution is ambiguous → skip",
    )
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)
    if not db_path.exists():
        log(f"collect_bead_close: DB not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        stats = collect(
            conn,
            repo_root,
            dry_run=args.dry_run,
            limit=args.limit,
            window_days=args.window_days,
            tie_window_s=args.tie_window_s,
        )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    log(
        f"collect_bead_close: pending={stats['pending']} written={stats['written']} "
        f"skipped_no_beads={stats['skipped_no_beads']} "
        f"skipped_no_match={stats['skipped_no_match']} "
        f"skipped_unresolved={stats['skipped_unresolved']}"
        + ("  (dry-run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
