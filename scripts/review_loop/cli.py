"""Command line for the review-loop job.

    review_loop freeze    --receipts DIR --week 2026-W44 --run-at ISO --out FILE
    review_loop sample    --snapshot FILE --out FILE
    review_loop aggregate --snapshot FILE --sample FILE --judgments FILE --report FILE [--digest FILE]

Each step reads and writes JSON files, so a week can be replayed byte-for-byte
from its snapshot. Nothing here edits policy or files an issue.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

from . import core

CODE_VERSION = "review-loop-1"


def _read_receipts(directory: pathlib.Path) -> list[dict]:
    rows = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _write(path: str, obj) -> None:
    pathlib.Path(path).write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n")


def _load(path: str):
    return json.loads(pathlib.Path(path).read_text())


def cmd_freeze(a) -> int:
    rows = _read_receipts(pathlib.Path(a.receipts))
    receipts = core.load_receipts(rows)
    run_at = core.parse_time(a.run_at)
    try:
        snap = core.freeze(receipts, a.week, run_at, code_version=CODE_VERSION)
    except ValueError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    snap["dropped_unattributable"] = len(rows) - len(receipts)
    _write(a.out, snap)
    print(f"froze {a.week}: {len(snap['population']['frame'])} frame PRs, "
          f"{snap['population']['n_block_receipts']} BLOCK receipts")
    return 0


def cmd_sample(a) -> int:
    snap = _load(a.snapshot)
    drawn = core.draw(snap["population"]["frame"], snap["manifest"]["bootstrap_seed"])
    _write(a.out, drawn)
    print(f"sampled {len(drawn['sample'])} PRs")
    return 0


def cmd_aggregate(a) -> int:
    snap = _load(a.snapshot)
    drawn = _load(a.sample)
    judgments = _load(a.judgments)
    try:
        agg = core.aggregate(drawn["sample"], judgments, snap["manifest"], snap["population"])
    except ValueError as exc:
        print(f"refusing to aggregate: {exc}", file=sys.stderr)
        return 2
    mechs = core.mechanism_counts(drawn["sample"], judgments)
    proposals = core.backlog_proposals(mechs, judgments.get("_changes") if isinstance(judgments, dict) else None)
    recoverable = _recovered(drawn["sample"], judgments)
    report = core.render_report(snap["manifest"], snap["population"], drawn["allocation"], agg,
                                mechs, proposals, recoverable)
    pathlib.Path(a.report).write_text(report)
    if a.digest:
        pathlib.Path(a.digest).write_text(core.digest_block(snap["manifest"], agg, proposals, recoverable))
    print(f"L={agg['L']:.4f} U={agg['U']:.4f}")
    return 0


def _recovered(sample: list[dict], judgments: dict) -> dict:
    total = rec = 0
    for s in sample:
        j = judgments.get(s["pr"])
        for r in (j["receipts"] if j else []):
            total += 1
            if r.get("text_recovered", True):
                rec += 1
    return {"recovered": rec, "total": total}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="review_loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--receipts", required=True)
    f.add_argument("--week", required=True)
    f.add_argument("--run-at", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_freeze)
    s = sub.add_parser("sample")
    s.add_argument("--snapshot", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_sample)
    g = sub.add_parser("aggregate")
    g.add_argument("--snapshot", required=True)
    g.add_argument("--sample", required=True)
    g.add_argument("--judgments", required=True)
    g.add_argument("--report", required=True)
    g.add_argument("--digest")
    g.set_defaults(fn=cmd_aggregate)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
