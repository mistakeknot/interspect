#!/usr/bin/env python3
"""Shared helpers for skill-signal collectors (sylveste-7aj8.3).

Each collector under ``scripts/signals/`` reads *pending* skill evidence rows —
``evidence`` rows with ``source_kind='skill'`` whose matching
``(invocation_id, signal_kind)`` pair is not yet in ``skill_signals`` — and
writes one normalized ``[0,1]`` signal row (1.0 = good) per invocation. All
writes go through ``INSERT OR IGNORE`` on the
``UNIQUE(invocation_id, signal_kind)`` constraint so reruns are idempotent.

The invocation_id lives in ``evidence.source_event_id`` (set by
``ingest-skill-audit.py``); session_id / skill_name / ts / project come from the
evidence row directly. This module centralizes repo-root and DB discovery
(mirroring ``ingest-skill-audit.py`` and ``calibrate-audit.py``), the
pending-row query, and the idempotent signal writer so every collector behaves
identically.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple


# ─── Repo-root / DB discovery (mirrors ingest-skill-audit.py) ────────────────


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".clavain").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return start.resolve()


def default_db_path(repo_root: Path) -> Path:
    return repo_root / ".clavain" / "interspect" / "interspect.db"


# ─── Pending skill evidence ──────────────────────────────────────────────────


class SkillEvidence(NamedTuple):
    """A skill invocation awaiting one specific signal_kind."""

    invocation_id: str
    session_id: str
    skill_name: str
    ts: str
    project: str  # path string ('' if unknown)


def pending_evidence(
    conn: sqlite3.Connection, signal_kind: str, limit: int | None = None
) -> list[SkillEvidence]:
    """Return skill evidence rows lacking a ``signal_kind`` signal.

    A row is pending when it is a skill evidence row (``source_kind='skill'``,
    ``source_event_id`` populated) and no ``skill_signals`` row exists for that
    ``(invocation_id, signal_kind)`` pair. ``evidence.source`` holds the skill
    name; ``evidence.source_event_id`` holds the invocation_id.
    """
    sql = (
        "SELECT e.source_event_id, e.session_id, e.source, e.ts, e.project "
        "FROM evidence e "
        "WHERE e.source_kind = 'skill' "
        "  AND e.source_event_id IS NOT NULL "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM skill_signals s "
        "    WHERE s.invocation_id = e.source_event_id "
        "      AND s.signal_kind = ?"
        "  ) "
        "ORDER BY e.ts ASC"
    )
    params: tuple = (signal_kind,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (signal_kind, limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        SkillEvidence(
            invocation_id=r[0],
            session_id=r[1],
            skill_name=r[2],
            ts=r[3],
            project=r[4] or "",
        )
        for r in rows
    ]


# ─── Idempotent signal writer ────────────────────────────────────────────────


def write_signal(
    conn: sqlite3.Connection,
    ev: SkillEvidence,
    signal_kind: str,
    value: float,
    *,
    raw_value: float | None = None,
    metadata: str | None = None,
) -> bool:
    """INSERT OR IGNORE one signal row. Returns True if a row was written.

    Idempotent via ``UNIQUE(invocation_id, signal_kind)``. ``observed_at`` is the
    invocation ts (so the signal sorts with the evidence, not wall-clock now).
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO skill_signals "
        "(skill_name, session_id, invocation_id, signal_kind, value, "
        " raw_value, observed_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev.skill_name,
            ev.session_id,
            ev.invocation_id,
            signal_kind,
            value,
            raw_value,
            ev.ts,
            metadata,
        ),
    )
    return bool(cur.rowcount and cur.rowcount > 0)


# ─── Shared logging ──────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(msg, file=sys.stderr)
