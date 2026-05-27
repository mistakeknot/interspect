---
name: calibrate-audit
description: Self-audit interspect calibration — compare current ranking vs a snapshot from N days ago, flag drift
argument-hint: "[--window-days=90] [--hit-rate-delta=0.2] [--rank-delta=5] [--min-correlation=0.7]"
---

# Interspect Calibrate-Audit

Detect drift in interspect's own calibration over time. Track-C-distant
review finding from the 2026-05-26 multi-agent toolchain audit:
*interspect calibrates agents using its own evidence model; if all
three agents (interspect, agents, evidence collection) fail silently,
the loop recalibrates to the broken state and stabilizes there.*

This command compares the current `.clavain/interspect/routing-calibration.json`
against a historical snapshot from at least `--window-days` (default 90)
days ago and flags significant drift in agent rankings.

## Run

```bash
# Locate the script
SCRIPT="${CLAUDE_PLUGIN_ROOT}/scripts/calibrate-audit.py"
if [[ ! -f "$SCRIPT" ]]; then
    echo "Error: $SCRIPT not found" >&2
    exit 1
fi

# Find repo root (where .clavain/ lives)
REPO_ROOT="$(pwd)"
while [[ "$REPO_ROOT" != "/" && ! -d "$REPO_ROOT/.clavain" && ! -d "$REPO_ROOT/.git" ]]; do
    REPO_ROOT="$(dirname "$REPO_ROOT")"
done

python3 "$SCRIPT" --repo-root="$REPO_ROOT" "$@"
```

## Drift criteria

A snapshot pair triggers `DRIFT DETECTED` verdict when **any** apply:

1. **Spearman rank correlation** between the two agent rankings drops
   below `--min-correlation` (default 0.7). This catches global
   reshuffling — agents collectively moved relative to each other.
2. **Per-agent hit_rate delta** ≥ `--hit-rate-delta` (default 0.2).
   This catches individual agents whose accuracy materially changed.
3. **Per-agent rank delta** ≥ `--rank-delta` (default 5). This catches
   agents that moved many positions even when absolute hit_rate is
   stable (e.g., everyone else got better).

Severity is reported per-agent:
- `none` — no change
- `low` — change present but below thresholds
- `high` — exceeds at least one threshold

## Output

Report lands at `docs/research/interspect-audit/{YYYY-QN}-calibration-audit.md`.
Quarter-tagged so reruns within a quarter overwrite (one report per
quarter is the intended cadence).

Exit codes:
- `0` — STABLE (or BOOTSTRAP if no historical snapshot is old enough)
- `1` — error (no current calibration; run `/interspect:calibrate` first)
- `2` — DRIFT DETECTED (suitable for cron/CI to alert on)

## Bootstrap behavior

If `.clavain/interspect/calibration-history/` contains no snapshot
older than `--window-days`, the report is generated with verdict
`BOOTSTRAP` and exit 0. The audit becomes useful once enough
calibration history has accumulated.

Snapshots are created automatically by `_interspect_write_routing_calibration`
in `hooks/lib-interspect.sh` — every time `/interspect:calibrate` runs,
the resulting JSON is copied into the history directory with a
timestamped filename. Snapshots older than 1 year are pruned.

## Scheduling

For quarterly automation, install a one-line scheduled task:

```bash
# Via mk's scheduled-tasks MCP (preferred — see /schedule skill)
/schedule "0 9 1 */3 *" "python3 ~/projects/Sylveste/interverse/interspect/scripts/calibrate-audit.py --repo-root=~/projects/Sylveste"

# Or via cron directly
echo '0 9 1 */3 * cd ~/projects/Sylveste && python3 interverse/interspect/scripts/calibrate-audit.py --repo-root=$(pwd) > /tmp/interspect-audit.log 2>&1' | crontab -
```

The audit is read-only (snapshots are made by `/interspect:calibrate`,
not by this command), so it's safe to run at any frequency.

## Related

- Source finding: [Track-C distant review](../../../../docs/research/flux-review/improve-toolchain-1d7a8b22/track-c-distant.md) — "Causal Loop Blindness"
- Synthesis: [SYNTHESIS.md Action 8](../../../../docs/research/flux-review/improve-toolchain-1d7a8b22/SYNTHESIS.md)
- Bead: `Sylveste-xr3`
