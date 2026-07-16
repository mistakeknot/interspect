#!/usr/bin/env python3
"""Calibrate-audit: detect drift between current and historical interspect calibration.

Reads .clavain/interspect/routing-calibration.json (current) and the
oldest snapshot in .clavain/interspect/calibration-history/ that is at
least --window-days old (default 90). Compares per-agent hit_rate and
rank ordering. Flags drift when:
  - any agent's hit_rate moved by more than --hit-rate-delta (default 0.2)
  - any agent's rank diverged by --rank-delta or more positions (default 5)
  - Spearman rank correlation between the two rankings drops below
    --min-correlation (default 0.7)

Writes a markdown report to
docs/research/interspect-audit/{YYYY-QN}-calibration-audit.md.

If no historical snapshot is old enough, exits 0 with a "bootstrap" report.

Additionally (Sylveste-4b5.1 Phase-1 metrics emission): each run computes a
consensus-trend point from interspect.db — agreement_rate, output_diversity,
defect_escape_rate, evidence_n — and appends it as one JSON line to
.clavain/interspect/consensus-trend.jsonl. This is retrospective-trend
instrumentation for the consensus-trap kill rule (see docs/research/
interspect-audit/2026-07-16-consensus-trap-phase1.md); it does not gate or
fire any breaker on its own.

Usage:
  calibrate-audit.py [--window-days=90] [--hit-rate-delta=0.2]
                     [--rank-delta=5] [--min-correlation=0.7]
                     [--repo-root=.]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".clavain").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return start.resolve()


def load_calibration(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_oldest_snapshot(history_dir: Path, min_age_days: int) -> Path | None:
    if not history_dir.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    candidates = []
    for f in history_dir.glob("*.json"):
        try:
            ts_str = f.stem.rstrip("Z").replace("-", ":")
            # Format: 2026-05-27T04-30-15
            parts = f.stem.rstrip("Z").split("T")
            if len(parts) != 2:
                continue
            date_str, time_str = parts
            time_normalized = time_str.replace("-", ":")
            dt = datetime.fromisoformat(f"{date_str}T{time_normalized}+00:00")
        except (ValueError, IndexError):
            continue
        if dt <= cutoff:
            candidates.append((dt, f))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    return candidates[0][1]


def spearman_correlation(
    rank_a: dict[str, int], rank_b: dict[str, int]
) -> float | None:
    """Spearman rank correlation between two rankings. Uses only shared agents.

    Returns None if fewer than 2 shared agents.
    """
    shared = sorted(set(rank_a) & set(rank_b))
    n = len(shared)
    if n < 2:
        return None
    d_squared_sum = sum((rank_a[a] - rank_b[a]) ** 2 for a in shared)
    rho = 1 - (6 * d_squared_sum) / (n * (n * n - 1))
    return rho


def rank_agents(calibration: dict) -> dict[str, int]:
    """Return {agent_name: rank} where rank 1 = highest hit_rate."""
    agents = calibration.get("agents", {}) or {}
    rows = []
    for name, stats in agents.items():
        hr = stats.get("weighted_hit_rate", stats.get("hit_rate"))
        if hr is None:
            continue
        rows.append((name, hr))
    rows.sort(key=lambda kv: -kv[1])
    return {name: idx + 1 for idx, (name, _) in enumerate(rows)}


def quarter_label(now: datetime) -> str:
    q = (now.month - 1) // 3 + 1
    return f"{now.year}-Q{q}"


def compute_consensus_trend_point(
    db_path: Path, now: datetime, window_days: int
) -> dict | None:
    """Compute one consensus-trend point from interspect.db evidence.

    Definitions (Sylveste-4b5.1 Phase-1; see docs/research/interspect-audit/
    2026-07-16-consensus-trap-phase1.md for full derivation + limitations):

      agreement_rate: among agent-sourced evidence rows in the window,
        1 - (rows with a non-empty override_reason / total agent rows).
        Proxy for verifier-vs-generator agreement. Currently always 1.0
        while override_reason is unpopulated across the fleet — documented
        limitation, not a measured signal yet.

      output_diversity: mean distinct evidence `source` values per session
        in the window, divided by the total distinct `source` values seen
        in the window. 1.0 = every session touches every source (low
        diversity per session relative to the whole); lower = sessions
        specialize on fewer sources. Proxy only; does not inspect actual
        generated content.

      defect_escape_rate: 1 - avg(value) over skill_signals rows with
        signal_kind='error' in the window. skill_signals.value is a
        goodness score in [0,1] (1.0 = clean), so this is the mean deficit.
        Proxy for "problems surfacing after the loop approved something";
        it is a skill-invocation error rate, not a verified downstream
        defect count, since no field currently distinguishes pre- vs
        post-approval discovery.

    Returns None if the db is missing or unreadable. Returns a dict with
    evidence_n=0 (and metrics as None) if the db exists but the window has
    no rows.
    """
    if not db_path.exists():
        return None

    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None

    try:
        cur = conn.cursor()

        # --- agreement_rate ---
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN override_reason IS NOT NULL
                         AND override_reason != '' THEN 1 ELSE 0 END) AS overridden
            FROM evidence
            WHERE source_kind = 'agent' AND ts >= ?
            """,
            (cutoff,),
        )
        row = cur.fetchone()
        agent_total = row["total"] or 0
        agreement_rate = (
            1.0 - (row["overridden"] or 0) / agent_total if agent_total else None
        )

        # --- output_diversity ---
        cur.execute(
            "SELECT COUNT(DISTINCT source) FROM evidence WHERE ts >= ?",
            (cutoff,),
        )
        total_distinct_sources = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT session_id, COUNT(DISTINCT source) AS n_sources
            FROM evidence
            WHERE ts >= ?
            GROUP BY session_id
            """,
            (cutoff,),
        )
        per_session = [r["n_sources"] for r in cur.fetchall()]
        output_diversity = (
            (sum(per_session) / len(per_session)) / total_distinct_sources
            if per_session and total_distinct_sources
            else None
        )

        # --- defect_escape_rate ---
        has_skill_signals = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_signals'"
        ).fetchone()
        defect_escape_rate = None
        if has_skill_signals:
            cur.execute(
                """
                SELECT AVG(value) AS avg_value, COUNT(*) AS n
                FROM skill_signals
                WHERE signal_kind = 'error' AND observed_at >= ?
                """,
                (cutoff,),
            )
            row = cur.fetchone()
            if row["n"]:
                defect_escape_rate = 1.0 - row["avg_value"]

        # --- evidence_n: total evidence rows in window (denominator context) ---
        cur.execute("SELECT COUNT(*) FROM evidence WHERE ts >= ?", (cutoff,))
        evidence_n = cur.fetchone()[0] or 0

    except sqlite3.Error:
        # Malformed/table-less DB or unwritable WAL side-files: honor the
        # docstring's "None if unreadable" contract instead of crashing the
        # pre-existing drift-audit path.
        return None
    finally:
        conn.close()

    return {
        "cycle_ts": now.isoformat(),
        "agreement_rate": agreement_rate,
        "output_diversity": output_diversity,
        "defect_escape_rate": defect_escape_rate,
        "evidence_n": evidence_n,
    }


def append_consensus_trend(calibration_dir: Path, point: dict) -> Path:
    """Append one consensus-trend point as a JSON line. Creates the file if missing."""
    trend_path = calibration_dir / "consensus-trend.jsonl"
    with trend_path.open("a") as f:
        f.write(json.dumps(point, sort_keys=True) + "\n")
    return trend_path


def render_consensus_trend_section(point: dict | None, trend_path: Path | None) -> list[str]:
    lines = ["## Consensus-trend (Sylveste-4b5.1 Phase-1 metrics emission)", ""]
    if point is None:
        lines += [
            "No interspect.db found — consensus-trend point not computed this run.",
            "",
        ]
        return lines

    def fmt(v):
        return f"{v:.3f}" if isinstance(v, (int, float)) else "n/a (no evidence in window)"

    lines += [
        f"- agreement_rate: {fmt(point['agreement_rate'])}",
        f"- output_diversity: {fmt(point['output_diversity'])}",
        f"- defect_escape_rate: {fmt(point['defect_escape_rate'])}",
        f"- evidence_n: {point['evidence_n']}",
        "",
        f"Appended to `{trend_path}`." if trend_path else "",
        "",
        "See docs/research/interspect-audit/2026-07-16-consensus-trap-phase1.md "
        "for metric definitions, limitations, and kill-rule status.",
        "",
    ]
    return [l for l in lines if l is not None]


def render_report(
    *,
    now: datetime,
    current_path: Path,
    snapshot_path: Path | None,
    drift_findings: list[dict],
    correlation: float | None,
    args: argparse.Namespace,
    consensus_trend_lines: list[str] | None = None,
) -> str:
    lines = [
        f"# Interspect calibration audit — {quarter_label(now)}",
        "",
        f"Generated: {now.isoformat()}",
        f"Window: {args.window_days} days",
        f"Current calibration: `{current_path}`",
    ]
    if snapshot_path:
        lines.append(f"Historical snapshot: `{snapshot_path.name}`")
    else:
        lines.append(
            "Historical snapshot: **none** (bootstrap — re-run after"
            f" {args.window_days} days of calibration history accumulates)"
        )
    lines.append("")

    if correlation is None:
        lines += [
            "## Verdict: BOOTSTRAP",
            "",
            "Not enough history to compute drift. The audit will produce useful "
            "output once at least one snapshot is older than the audit window.",
        ]
    elif correlation < args.min_correlation or any(
        f["severity"] == "high" for f in drift_findings
    ):
        lines += [
            "## Verdict: DRIFT DETECTED",
            "",
            f"Spearman rank correlation: **{correlation:.3f}** "
            f"(threshold: {args.min_correlation})",
            "",
        ]
    else:
        lines += [
            "## Verdict: STABLE",
            "",
            f"Spearman rank correlation: {correlation:.3f} (>= {args.min_correlation})",
            f"No agents exceeded drift thresholds (hit_rate Δ < "
            f"{args.hit_rate_delta}, rank Δ < {args.rank_delta}).",
            "",
        ]

    if drift_findings:
        lines += ["## Per-agent drift", ""]
        lines += [
            "| Agent | Δ hit_rate | Δ rank | Severity |",
            "|---|---:|---:|---|",
        ]
        for f in drift_findings:
            lines.append(
                f"| `{f['agent']}` | "
                f"{f['delta_hit_rate']:+.3f} | "
                f"{f['delta_rank']:+d} | "
                f"{f['severity']} |"
            )
        lines.append("")

    if consensus_trend_lines:
        lines += [""] + consensus_trend_lines

    lines += [
        "## Methodology",
        "",
        f"1. Load current calibration from `{current_path}`.",
        f"2. Find the most recent snapshot in `.clavain/interspect/calibration-history/` "
        f"that is at least {args.window_days} days old.",
        "3. For each agent in both:",
        f"   - Flag if |Δ hit_rate| ≥ {args.hit_rate_delta} (severity high)",
        f"   - Flag if |Δ rank| ≥ {args.rank_delta} (severity high)",
        f"   - Otherwise: severity low if any change, none if identical",
        f"4. Compute Spearman rank correlation across shared agents; flag overall "
        f"drift if ρ < {args.min_correlation}.",
        "",
        "## What to do if drift is detected",
        "",
        "1. Inspect the per-agent table. Which agents moved most?",
        "2. Was there a change to the scoring formula in interspect? "
        "Check `_INTERSPECT_SOURCE_WEIGHT_*` and `_INTERSPECT_CALIBRATION_MIN_*` "
        "constants in `hooks/lib-interspect.sh`.",
        "3. Was there a real population shift (different mix of work types) in "
        "the window? Check beads close-out activity.",
        "4. If the formula changed: confirm the new ranking matches reality "
        "(spot-check a few agents that moved). Old formula = the snapshot used "
        "as baseline; new formula = current.",
        "5. If reality shifted: this is signal, not noise — accept and move on.",
        "",
        "Source: Sylveste-xr3 / docs/research/flux-review/improve-toolchain-1d7a8b22/SYNTHESIS.md",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-days", type=int, default=90)
    ap.add_argument("--hit-rate-delta", type=float, default=0.2)
    ap.add_argument("--rank-delta", type=int, default=5)
    ap.add_argument("--min-correlation", type=float, default=0.7)
    ap.add_argument("--repo-root", default=".")
    ap.add_argument(
        "--dry-run-safe",
        action="store_true",
        help=(
            "Test invocation: compute and print the consensus-trend point "
            "without writing consensus-trend.jsonl or the markdown report. "
            "Safe to run against a scratch copy of the state dir."
        ),
    )
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    calibration_dir = repo_root / ".clavain" / "interspect"
    current_path = calibration_dir / "routing-calibration.json"
    history_dir = calibration_dir / "calibration-history"
    db_path = calibration_dir / "interspect.db"
    report_dir = repo_root / "docs" / "research" / "interspect-audit"

    now = datetime.now(timezone.utc)

    # Consensus-trend point (Sylveste-4b5.1 Phase-1): computed independent of
    # whether routing-calibration.json exists yet, since the metrics are
    # derived straight from evidence, not from the calibration artifact.
    trend_point = compute_consensus_trend_point(db_path, now, args.window_days)

    if args.dry_run_safe:
        print(json.dumps(trend_point, indent=2, sort_keys=True))
        return 0

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{quarter_label(now)}-calibration-audit.md"

    trend_path: Path | None = None
    if trend_point is not None:
        trend_path = append_consensus_trend(calibration_dir, trend_point)

    current = load_calibration(current_path)
    if current is None:
        print(
            f"calibrate-audit: no current calibration at {current_path} — "
            "run /interspect:calibrate first",
            file=sys.stderr,
        )
        return 1

    snapshot_path = find_oldest_snapshot(history_dir, args.window_days)
    snapshot = load_calibration(snapshot_path) if snapshot_path else None

    drift_findings: list[dict] = []
    correlation: float | None = None

    if snapshot:
        current_ranks = rank_agents(current)
        snapshot_ranks = rank_agents(snapshot)
        correlation = spearman_correlation(snapshot_ranks, current_ranks)

        for agent in sorted(set(current_ranks) & set(snapshot_ranks)):
            cur_stats = current["agents"][agent]
            snap_stats = snapshot["agents"][agent]
            cur_hr = cur_stats.get("weighted_hit_rate", cur_stats.get("hit_rate"))
            snap_hr = snap_stats.get("weighted_hit_rate", snap_stats.get("hit_rate"))
            if cur_hr is None or snap_hr is None:
                continue
            d_hr = cur_hr - snap_hr
            d_rank = current_ranks[agent] - snapshot_ranks[agent]
            severity = "none"
            if abs(d_hr) >= args.hit_rate_delta or abs(d_rank) >= args.rank_delta:
                severity = "high"
            elif abs(d_hr) > 0 or abs(d_rank) > 0:
                severity = "low"
            if severity != "none":
                drift_findings.append(
                    {
                        "agent": agent,
                        "delta_hit_rate": d_hr,
                        "delta_rank": d_rank,
                        "severity": severity,
                    }
                )

    report = render_report(
        now=now,
        current_path=current_path,
        snapshot_path=snapshot_path,
        drift_findings=drift_findings,
        correlation=correlation,
        args=args,
        consensus_trend_lines=render_consensus_trend_section(trend_point, trend_path),
    )
    report_path.write_text(report)
    print(report_path)

    # Exit non-zero if drift detected (for cron / CI to act on)
    if drift_findings and any(f["severity"] == "high" for f in drift_findings):
        return 2
    if correlation is not None and correlation < args.min_correlation:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
