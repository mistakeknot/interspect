#!/usr/bin/env python3
"""collect_error: catch-up runner for the skill ``error`` signal (7aj8.3).

The ``error`` signal is normally written *inline* by ``ingest-skill-audit.py``
at ingest time (value 1.0 = success, 0.0 = failure), so this collector is mostly
a no-op safety net. It exists to backfill the ``error`` signal for any skill
evidence row that somehow lacks one — e.g. an evidence row written by a code
path that did not also write the signal, or a partial/interrupted ingest.

Success derivation: the evidence row does not carry exit/error context (the
ingest stores it only in the inline signal and the evidence ``context`` is
``'{}'``). When that inline signal is missing we cannot recover the failure
state, so we conservatively default to **success = 1.0** and note it in
``metadata`` (``{"recovered":"default-success"}``). This matches the ingest's
own behavior for sources that lack an explicit error field.

signal_kind: ``error``  (value 1.0 = success, 0.0 = failure)

Usage:
  collect_error.py [--db <path>] [--dry-run] [--limit N] [--repo-root .]
"""

from __future__ import annotations

import argparse
import os
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

SIGNAL_KIND = "error"


def collect(conn: sqlite3.Connection, *, dry_run: bool, limit: int | None) -> dict[str, int]:
    stats = {"pending": 0, "written": 0, "skipped": 0}
    pending = pending_evidence(conn, SIGNAL_KIND, limit)
    stats["pending"] = len(pending)

    for ev in pending:
        # No recoverable exit/error context on the evidence row → default success.
        value = 1.0
        if dry_run:
            stats["written"] += 1
            continue
        if write_signal(
            conn,
            ev,
            SIGNAL_KIND,
            value,
            raw_value=0.0,
            metadata='{"recovered":"default-success"}',
        ):
            stats["written"] += 1
        else:
            stats["skipped"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="Override interspect.db path")
    ap.add_argument("--dry-run", action="store_true", help="Project counts, no writes")
    ap.add_argument("--limit", type=int, default=None, help="Max pending rows to process")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)
    if not db_path.exists():
        log(f"collect_error: DB not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        stats = collect(conn, dry_run=args.dry_run, limit=args.limit)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    log(
        f"collect_error: pending={stats['pending']} written={stats['written']} "
        f"skipped={stats['skipped']}"
        + ("  (dry-run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
