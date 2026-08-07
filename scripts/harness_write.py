#!/usr/bin/env python3
"""Continual-harness write ledger — scoped, evidence-backed, reversible.

Adapted from the continual harness in PrimeIntellect/prime-agent, kept in
interspect because interspect already owns the half prime-agent lacks:
counting-rule thresholds, canary periods, and a regression gate. Generating
harness entries without that machinery produces writes nobody ever validates.

Three properties the existing memory lane does not have:

1. Scope. Writes are LOCAL to a session by default; GLOBAL is opt-in and
   reserved for durable cross-session lessons. auto-memory is global-only
   today, so every session-specific observation either pollutes the shared
   index or is dropped. Local writes give them somewhere to go.

2. Falsifiability. Every write carries `evidence` (what in the trajectory
   justifies it) and `expected_outcome` (what should improve, and how to
   check). Both are required and non-empty — a write nobody can later prove
   wrong is not a lesson, it is a rumor. These are also what interspect's
   canary needs as input to score the write later.

3. Reversibility. Every write has a stable id and stores prior content, so
   `rollback <id>` restores the exact previous state. Git gives you history;
   this gives you "undo the write that came from that trajectory".

Usage:
    harness_write.py write --kind memory --name my-fact --scope local \
        --session abc123 --content-file note.md \
        --evidence "Edit failed 3x on the same stale read" \
        --expected-outcome "Re-read before editing; check edit_stats not_read_first drops"
    harness_write.py list [--scope local] [--session abc123] [--status active]
    harness_write.py show <id>
    harness_write.py rollback <id>
    harness_write.py promote <id>        # local -> global
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

KINDS = ("memory", "prompt", "skill", "subagent")
SCOPES = ("local", "global")
ACTIONS = ("create", "update", "delete")

# Global memory writes land in the auto-memory directory the session loads at
# startup. Local writes are session-scoped and never reach that index.
GLOBAL_MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-sma-projects" / "memory"

SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_writes (
    id                TEXT PRIMARY KEY,
    ts                TEXT NOT NULL,
    scope             TEXT NOT NULL CHECK (scope IN ('local','global')),
    session_id        TEXT,
    project           TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL,
    name              TEXT NOT NULL,
    path              TEXT,
    action            TEXT NOT NULL,
    content           TEXT,
    prior_content     TEXT,
    prior_existed     INTEGER NOT NULL DEFAULT 0,
    evidence          TEXT NOT NULL,
    expected_outcome  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    rolled_back_at    TEXT,
    promoted_from     TEXT,
    version           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_hw_scope ON harness_writes(scope, status);
CREATE INDEX IF NOT EXISTS idx_hw_session ON harness_writes(session_id);
CREATE INDEX IF NOT EXISTS idx_hw_name ON harness_writes(kind, name);
"""


def db_path() -> Path:
    override = os.environ.get("INTERSPECT_DB")
    if override:
        return Path(override)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = Path(root)
    while path != path.parent:
        if (path / ".clavain").is_dir() or (path / ".git").exists():
            return path / ".clavain" / "interspect" / "interspect.db"
        path = path.parent
    return Path.cwd() / ".clavain" / "interspect" / "interspect.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_id(kind: str, name: str, ts: str) -> str:
    """Stable, unique write id.

    The timestamp is only second-resolution, so kind+name+ts collides when a
    write and its promotion land in the same second — which is the common
    case, not a rare one. A uuid4 component makes the id unique; it does not
    need to be reproducible, only stable once assigned.
    """
    digest = hashlib.sha256(f"{kind}:{name}:{ts}:{uuid4().hex}".encode()).hexdigest()[:10]
    return f"hw_{digest}"


def target_path(scope: str, kind: str, name: str, session_id: str | None) -> Path:
    if scope == "global":
        return GLOBAL_MEMORY_DIR / f"{name}.md"
    base = db_path().parent / "harness" / "local" / (session_id or "unknown")
    return base / f"{kind}__{name}.md"


def record_evidence_row(conn: sqlite3.Connection, row: dict) -> None:
    """Mirror the write into interspect's evidence table.

    This is what makes expected_outcome load-bearing rather than decorative:
    the same counting rules and canary that score agents and skills can see
    harness writes as a third source_kind.
    """
    try:
        conn.execute(
            "INSERT INTO evidence (ts, session_id, seq, source, event, context, project, source_kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                row["ts"], row["session_id"] or "", 0, f"{row['kind']}:{row['name']}",
                "harness_write",
                json.dumps({
                    "write_id": row["id"], "scope": row["scope"], "action": row["action"],
                    "expected_outcome": row["expected_outcome"],
                }),
                row["project"], "memory",
            ),
        )
    except sqlite3.OperationalError:
        # The evidence table belongs to the shell library's migration. If this
        # DB predates it, the ledger still works standalone — degrade rather
        # than refuse the write.
        pass


# --- commands ---

def cmd_write(args: argparse.Namespace) -> int:
    evidence = (args.evidence or "").strip()
    expected = (args.expected_outcome or "").strip()
    # Refusing the write is the point. A harness entry with no stated evidence
    # and no way to check it is exactly the kind of unfalsifiable claim this
    # tier exists to keep out.
    if not evidence:
        print("refused: --evidence is required and must be non-empty", file=sys.stderr)
        return 2
    if not expected:
        print("refused: --expected-outcome is required and must be non-empty", file=sys.stderr)
        return 2
    if args.scope == "local" and not args.session:
        print("refused: --session is required for local scope", file=sys.stderr)
        return 2

    content = ""
    if args.content_file:
        content = Path(args.content_file).read_text()
    elif args.content is not None:
        content = args.content
    elif args.action != "delete":
        print("refused: provide --content or --content-file", file=sys.stderr)
        return 2

    path = Path(args.path) if args.path else target_path(args.scope, args.kind, args.name, args.session)
    prior_existed = path.exists()
    prior_content = path.read_text() if prior_existed else None

    ts = now_iso()
    write_id = make_id(args.kind, args.name, ts)
    row = {
        "id": write_id, "ts": ts, "scope": args.scope, "session_id": args.session,
        "project": args.project or os.getcwd(), "kind": args.kind, "name": args.name,
        "path": str(path), "action": args.action, "content": content,
        "prior_content": prior_content, "prior_existed": int(prior_existed),
        "evidence": evidence, "expected_outcome": expected,
    }

    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO harness_writes (id,ts,scope,session_id,project,kind,name,path,action,"
            "content,prior_content,prior_existed,evidence,expected_outcome) "
            "VALUES (:id,:ts,:scope,:session_id,:project,:kind,:name,:path,:action,"
            ":content,:prior_content,:prior_existed,:evidence,:expected_outcome)",
            row,
        )
        record_evidence_row(conn, row)

    if args.action == "delete":
        if prior_existed:
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    print(f"{write_id}  {args.scope}/{args.kind}/{args.name} -> {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = connect()
    query = "SELECT * FROM harness_writes WHERE 1=1"
    params: list = []
    for field, value in (("scope", args.scope), ("session_id", args.session), ("status", args.status), ("kind", args.kind)):
        if value:
            query += f" AND {field} = ?"
            params.append(value)
    rows = conn.execute(query + " ORDER BY ts DESC LIMIT ?", (*params, args.limit)).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    if not rows:
        print("no harness writes match")
        return 0
    for r in rows:
        flag = "  " if r["status"] == "active" else "RB"
        print(f"{flag} {r['id']}  {r['ts']}  {r['scope']:<6} {r['kind']}/{r['name']}")
        print(f"     expect: {r['expected_outcome'][:100]}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM harness_writes WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"no such write: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(dict(row), indent=2))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM harness_writes WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"no such write: {args.id}", file=sys.stderr)
        return 1
    if row["status"] != "active":
        print(f"already rolled back: {args.id}", file=sys.stderr)
        return 1

    path = Path(row["path"])
    if row["prior_existed"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(row["prior_content"] or "")
        restored = "restored prior content"
    else:
        # The write created this file, so rollback removes it rather than
        # leaving an empty artifact behind.
        if path.exists():
            path.unlink()
        restored = "removed created file"

    with conn:
        conn.execute(
            "UPDATE harness_writes SET status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
            (now_iso(), args.id),
        )
    print(f"rolled back {args.id}: {restored} at {path}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Promote a local write to global — the only path from session to shared."""
    conn = connect()
    row = conn.execute("SELECT * FROM harness_writes WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"no such write: {args.id}", file=sys.stderr)
        return 1
    if row["scope"] != "local":
        print(f"not a local write: {args.id}", file=sys.stderr)
        return 1
    if row["status"] != "active":
        print(f"cannot promote a rolled-back write: {args.id}", file=sys.stderr)
        return 1

    ts = now_iso()
    new_id = make_id(row["kind"], row["name"], ts)
    path = target_path("global", row["kind"], row["name"], None)
    prior_existed = path.exists()
    prior_content = path.read_text() if prior_existed else None

    new_row = {
        "id": new_id, "ts": ts, "scope": "global", "session_id": row["session_id"],
        "project": row["project"], "kind": row["kind"], "name": row["name"],
        "path": str(path), "action": "update" if prior_existed else "create",
        "content": row["content"], "prior_content": prior_content,
        "prior_existed": int(prior_existed), "evidence": row["evidence"],
        "expected_outcome": row["expected_outcome"],
    }
    with conn:
        conn.execute(
            "INSERT INTO harness_writes (id,ts,scope,session_id,project,kind,name,path,action,"
            "content,prior_content,prior_existed,evidence,expected_outcome,promoted_from) "
            "VALUES (:id,:ts,:scope,:session_id,:project,:kind,:name,:path,:action,"
            ":content,:prior_content,:prior_existed,:evidence,:expected_outcome,:promoted_from)",
            {**new_row, "promoted_from": row["id"]},
        )
        record_evidence_row(conn, new_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row["content"] or "")
    print(f"{new_id}  promoted {args.id} local -> global at {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("write", help="Record and apply a harness write.")
    w.add_argument("--kind", choices=KINDS, required=True)
    w.add_argument("--name", required=True)
    w.add_argument("--scope", choices=SCOPES, default="local")
    w.add_argument("--session")
    w.add_argument("--project", default="")
    w.add_argument("--action", choices=ACTIONS, default="create")
    w.add_argument("--content")
    w.add_argument("--content-file")
    w.add_argument("--path", help="Override the computed target path.")
    w.add_argument("--evidence", required=True, help="What in the trajectory justifies this.")
    w.add_argument("--expected-outcome", required=True, help="What should improve, and how to check it.")
    w.set_defaults(func=cmd_write)

    l = sub.add_parser("list")
    l.add_argument("--scope", choices=SCOPES)
    l.add_argument("--session")
    l.add_argument("--kind", choices=KINDS)
    l.add_argument("--status", choices=("active", "rolled_back"))
    l.add_argument("--limit", type=int, default=25)
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    r = sub.add_parser("rollback")
    r.add_argument("id")
    r.set_defaults(func=cmd_rollback)

    p = sub.add_parser("promote")
    p.add_argument("id")
    p.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
