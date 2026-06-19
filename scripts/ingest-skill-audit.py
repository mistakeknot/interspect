#!/usr/bin/env python3
"""Ingest skill invocations into the Interspect evidence store (sylveste-7aj8.2).

Source-agnostic adapter: drains `tool: "Skill"` rows from a tool-invocation
log into two tables:

  - `evidence`     — one row per skill invocation, source_kind='skill',
                     event='skill_invocation'. Used by pattern/classification
                     queries the same way agent/tool rows are.
  - `skill_signals`— one 'error' signal row per invocation
                     (value 1.0 = success, 0.0 = failure). The
                     UNIQUE(invocation_id, signal_kind) constraint makes the
                     insert idempotent.

Source selection (adapter):
  1. `~/.claude/audit.log` (+ rotated `~/.claude/audit.log.1.gz`) if present
     and non-empty → parse as audit-log schema
     (docs/contracts/audit-log-schema.md).
  2. Otherwise `~/.claude/tool-time/events.jsonl` → parse as tool-time schema.
  Override with --source <path> and --format auto|audit|tooltime.

Incremental watermark: a single global cursor in the `sentinels` table
(key 'skill_ingest_watermark') stores the max processed `ts`. Repeated runs
skip records with ts <= watermark. We deliberately use one global sentinel
rather than per-session `sessions.last_skill_audit_ts`: it is simpler and
correct, and the idempotent inserts (UNIQUE on skill_signals, source_event_id
guard on evidence) cover any overlap from --since backfills or clock skew.

Usage:
  ingest-skill-audit.py [--source <path>] [--format auto|audit|tooltime]
                        [--db <path>] [--since <ISO8601|30d>] [--dry-run]
                        [--repo-root .]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, NamedTuple


# ─── Repo-root / DB discovery (mirrors calibrate-audit.py) ───────────────────


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".clavain").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return start.resolve()


def default_db_path(repo_root: Path) -> Path:
    return repo_root / ".clavain" / "interspect" / "interspect.db"


# ─── Normalized record ───────────────────────────────────────────────────────


class SkillRecord(NamedTuple):
    invocation_id: str
    session_id: str
    skill_name: str
    ts: str
    success: bool
    project: str | None


# ─── Source selection ────────────────────────────────────────────────────────

AUDIT_LOG = "~/.claude/audit.log"
AUDIT_LOG_GZ = "~/.claude/audit.log.1.gz"
TOOLTIME = "~/.claude/tool-time/events.jsonl"


class Source(NamedTuple):
    paths: list[Path]
    fmt: str  # 'audit' | 'tooltime'


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def select_source(source_arg: str | None, fmt_arg: str) -> Source:
    """Resolve which file(s) + format to parse.

    --source overrides the path; --format pins the parser. With defaults,
    prefer audit.log (current + rotated) when present and non-empty, else
    fall back to tool-time events.jsonl.
    """
    if source_arg:
        path = Path(os.path.expanduser(source_arg))
        fmt = fmt_arg
        if fmt == "auto":
            # Infer from filename when not pinned.
            fmt = "tooltime" if "events.jsonl" in path.name else "audit"
        return Source(paths=[path], fmt=fmt)

    if fmt_arg in ("audit", "tooltime"):
        # Format pinned but no explicit source — use that format's default path.
        if fmt_arg == "audit":
            current = Path(os.path.expanduser(AUDIT_LOG))
            gz = Path(os.path.expanduser(AUDIT_LOG_GZ))
            paths = [p for p in (current, gz) if p.is_file()]
            return Source(paths=paths, fmt="audit")
        return Source(paths=[Path(os.path.expanduser(TOOLTIME))], fmt="tooltime")

    # auto: prefer audit.log if present + non-empty.
    current = Path(os.path.expanduser(AUDIT_LOG))
    gz = Path(os.path.expanduser(AUDIT_LOG_GZ))
    if _nonempty(current) or _nonempty(gz):
        paths = [p for p in (current, gz) if p.is_file()]
        return Source(paths=paths, fmt="audit")

    return Source(paths=[Path(os.path.expanduser(TOOLTIME))], fmt="tooltime")


# ─── Line readers ────────────────────────────────────────────────────────────


def _open_lines(path: Path) -> Iterator[str]:
    """Yield text lines from a plain or gzipped file. Missing file → nothing."""
    if not path.is_file():
        return
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            yield from fh
    else:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from fh


# ─── Normalization ───────────────────────────────────────────────────────────

# session_id = the tool-time `id` with the trailing "-<counter>" stripped.
_COUNTER_SUFFIX = re.compile(r"-\d+$")


def _strip_counter(native_id: str) -> str:
    return _COUNTER_SUFFIX.sub("", native_id)


def _audit_invocation_id(ts: str, session_id: str, name: str) -> str:
    raw = f"{ts}|{session_id}|{name}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_audit(lines: Iterator[str]) -> Iterator[SkillRecord]:
    """Parse audit-log schema rows into SkillRecords (Skill rows only)."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("tool") != "Skill":
            continue
        name = obj.get("name") or ""
        ts = obj.get("ts") or ""
        session_id = obj.get("session_id") or ""
        if not name or not ts:
            continue
        # audit.log carries no project field.
        yield SkillRecord(
            invocation_id=_audit_invocation_id(ts, session_id, name),
            session_id=session_id,
            skill_name=name,
            ts=ts,
            success=(obj.get("exit_code", 0) == 0),
            project=None,
        )


def parse_tooltime(lines: Iterator[str]) -> Iterator[SkillRecord]:
    """Parse tool-time events.jsonl schema rows into SkillRecords."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("tool") != "Skill":
            continue
        skill_name = obj.get("skill") or ""
        ts = obj.get("ts") or ""
        native_id = obj.get("id") or ""
        if not skill_name or not ts or not native_id:
            continue
        yield SkillRecord(
            invocation_id=native_id,
            session_id=_strip_counter(native_id),
            skill_name=skill_name,
            ts=ts,
            success=(obj.get("error") is None),
            project=obj.get("project"),
        )


def read_records(source: Source) -> Iterator[SkillRecord]:
    parser = parse_audit if source.fmt == "audit" else parse_tooltime

    def all_lines() -> Iterator[str]:
        for p in source.paths:
            yield from _open_lines(p)

    yield from parser(all_lines())


# ─── Watermark ───────────────────────────────────────────────────────────────

WATERMARK_KEY = "skill_ingest_watermark"


def get_watermark(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM sentinels WHERE key = ?", (WATERMARK_KEY,)
    ).fetchone()
    return row[0] if row else None


def set_watermark(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "INSERT INTO sentinels (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (WATERMARK_KEY, value),
    )


def resolve_since(since: str | None) -> str | None:
    """Convert --since into an ISO8601 lower bound, or None.

    Accepts ISO8601 (e.g. 2026-06-01T00:00:00Z) or a duration like '30d',
    '12h', '90m' interpreted as "now minus that".
    """
    if not since:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", since.strip())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        dt = datetime.now(timezone.utc) - delta
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Assume it's already an ISO8601 timestamp; pass through verbatim so the
    # string comparison stays consistent with stored `ts` values.
    return since.strip()


# ─── DB helpers ──────────────────────────────────────────────────────────────


def next_seq(conn: sqlite3.Connection, seq_cache: dict[str, int], session_id: str) -> int:
    """Next per-session evidence seq. Cached so a batch run is consistent."""
    if session_id not in seq_cache:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM evidence WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq_cache[session_id] = row[0] if row else 0
    seq_cache[session_id] += 1
    return seq_cache[session_id]


def evidence_exists(conn: sqlite3.Connection, invocation_id: str) -> bool:
    """Dedup gate for evidence: keyed on source_event_id = invocation_id."""
    row = conn.execute(
        "SELECT 1 FROM evidence WHERE source_event_id = ? "
        "AND source_kind = 'skill' LIMIT 1",
        (invocation_id,),
    ).fetchone()
    return row is not None


# ─── Main ingest ─────────────────────────────────────────────────────────────


def ingest(
    conn: sqlite3.Connection,
    records: Iterator[SkillRecord],
    *,
    lower_bound: str | None,
    dry_run: bool,
) -> dict[str, int | str]:
    stats = {
        "scanned": 0,
        "skill_rows": 0,
        "below_watermark": 0,
        "evidence_inserted": 0,
        "signals_inserted": 0,
        "duplicates_skipped": 0,
        "max_ts": lower_bound or "",
    }
    seq_cache: dict[str, int] = {}
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for rec in records:
        stats["scanned"] += 1  # type: ignore[operator]
        stats["skill_rows"] += 1  # type: ignore[operator]

        # Watermark / --since gate: skip already-processed rows.
        if lower_bound is not None and rec.ts <= lower_bound:
            stats["below_watermark"] += 1  # type: ignore[operator]
            continue

        # Track the high-water mark across processed rows.
        if rec.ts > str(stats["max_ts"]):
            stats["max_ts"] = rec.ts

        if dry_run:
            # Project counts without writing. We can't cheaply know which would
            # dedup without a DB read, so report them as would-insert.
            if evidence_exists(conn, rec.invocation_id):
                stats["duplicates_skipped"] += 1  # type: ignore[operator]
            else:
                stats["evidence_inserted"] += 1  # type: ignore[operator]
                stats["signals_inserted"] += 1  # type: ignore[operator]
            continue

        # --- Evidence row (idempotent via source_event_id guard) ---
        new_evidence = not evidence_exists(conn, rec.invocation_id)
        if new_evidence:
            seq = next_seq(conn, seq_cache, rec.session_id)
            conn.execute(
                "INSERT INTO evidence "
                "(ts, session_id, seq, source, source_version, event, "
                " override_reason, context, project, project_lang, "
                " project_type, source_event_id, source_table, "
                " raw_override_reason, quarantine_until, source_kind) "
                "VALUES (?, ?, ?, ?, NULL, 'skill_invocation', NULL, '{}', "
                "        ?, NULL, NULL, ?, 'skill_signals', NULL, 0, 'skill')",
                (
                    rec.ts,
                    rec.session_id,
                    seq,
                    rec.skill_name,
                    rec.project or "",
                    rec.invocation_id,
                ),
            )
            stats["evidence_inserted"] += 1  # type: ignore[operator]
        else:
            stats["duplicates_skipped"] += 1  # type: ignore[operator]

        # --- Signal row (idempotent via UNIQUE(invocation_id, signal_kind)) ---
        cur = conn.execute(
            "INSERT OR IGNORE INTO skill_signals "
            "(skill_name, session_id, invocation_id, signal_kind, value, "
            " raw_value, observed_at, metadata) "
            "VALUES (?, ?, ?, 'error', ?, ?, ?, NULL)",
            (
                rec.skill_name,
                rec.session_id,
                rec.invocation_id,
                1.0 if rec.success else 0.0,
                0 if rec.success else 1,
                rec.ts,
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            stats["signals_inserted"] += 1  # type: ignore[operator]

    # Touch now_iso reference for potential future use (keeps lints quiet).
    _ = now_iso
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=None, help="Override source path")
    ap.add_argument(
        "--format",
        choices=["auto", "audit", "tooltime"],
        default="auto",
        help="Source format (default: auto)",
    )
    ap.add_argument("--db", default=None, help="Override interspect.db path")
    ap.add_argument(
        "--since",
        default=None,
        help="Backfill lower bound: ISO8601 or duration (e.g. 30d, 12h)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Project counts, no writes")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)

    if not db_path.exists():
        print(
            f"ingest-skill-audit: DB not found at {db_path} — "
            "run a hook or _interspect_ensure_db first",
            file=sys.stderr,
        )
        return 1

    source = select_source(args.source, args.format)
    if not source.paths or not any(p.is_file() for p in source.paths):
        print(
            f"ingest-skill-audit: no readable source "
            f"(format={source.fmt}, paths={[str(p) for p in source.paths]})",
            file=sys.stderr,
        )
        return 1

    print(
        f"ingest-skill-audit: source format={source.fmt} "
        f"paths={[str(p) for p in source.paths]}",
        file=sys.stderr,
    )

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        # --since overrides the stored watermark for backfill.
        if args.since:
            lower_bound = resolve_since(args.since)
            print(
                f"ingest-skill-audit: --since lower bound = {lower_bound}",
                file=sys.stderr,
            )
        else:
            lower_bound = get_watermark(conn)
            if lower_bound:
                print(
                    f"ingest-skill-audit: watermark lower bound = {lower_bound}",
                    file=sys.stderr,
                )

        records = read_records(source)
        stats = ingest(conn, records, lower_bound=lower_bound, dry_run=args.dry_run)

        if not args.dry_run:
            new_max = str(stats["max_ts"])
            # Advance the watermark only forward.
            if new_max and (lower_bound is None or new_max > lower_bound):
                set_watermark(conn, new_max)
                stats["watermark_advanced_to"] = new_max
            else:
                stats["watermark_advanced_to"] = lower_bound or "(unchanged)"
            conn.commit()
        else:
            stats["watermark_advanced_to"] = "(dry-run, unchanged)"
    finally:
        conn.close()

    print(
        "ingest-skill-audit: "
        f"source={source.fmt} "
        f"scanned={stats['scanned']} "
        f"skill_rows={stats['skill_rows']} "
        f"below_watermark={stats['below_watermark']} "
        f"evidence_inserted={stats['evidence_inserted']} "
        f"signals_inserted={stats['signals_inserted']} "
        f"duplicates_skipped={stats['duplicates_skipped']} "
        f"watermark_advanced_to={stats['watermark_advanced_to']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
