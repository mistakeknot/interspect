---
name: calibrate
description: Compute agent scores from evidence and write routing calibration
argument-hint: ""
---

# Interspect Calibrate

Compute per-agent routing scores from evidence (agent_dispatch, verdict_outcome, override events) and write `.clavain/interspect/routing-calibration.json`.

Requires >= 3 evidence sessions per agent before scoring. Agents with zero findings are excluded (insufficient signal). Safety floor agents are never recommended below sonnet.

## Locate Library

```bash
# Find lib-interspect.sh from the interspect plugin
SCRIPT_DIR="${CLAUDE_PLUGIN_ROOT}/hooks"
if [[ ! -f "${SCRIPT_DIR}/lib-interspect.sh" ]]; then
    echo "Error: Could not locate hooks/lib-interspect.sh" >&2
    exit 1
fi
source "${SCRIPT_DIR}/lib-interspect.sh"
```

## Run Calibration

```bash
# Ensure DB
_interspect_ensure_db || { echo "No interspect database found."; exit 0; }

# Compute scores
scores=$(_interspect_compute_agent_scores)
if [[ "$scores" == "[]" || -z "$scores" ]]; then
    echo "No agents with sufficient evidence for calibration (need >= 3 sessions with verdicts)."
    exit 0
fi

# Write calibration file
_interspect_write_routing_calibration
write_status=$?

# Display summary table
echo ""
echo "Agent Routing Calibration"
echo "========================="
echo ""
printf "%-25s %8s %8s %10s %12s\n" "Agent" "Sessions" "Hit Rate" "Current" "Recommended"
printf "%-25s %8s %8s %10s %12s\n" "─────" "────────" "────────" "───────" "───────────"

echo "$scores" | jq -r '.[] | [.agent, (.evidence_sessions|tostring), (if .hit_rate then (.hit_rate|tostring) else "n/a" end), .current_model, .recommended_model] | @tsv' | while IFS=$'\t' read -r agent sessions hr current rec; do
    printf "%-25s %8s %8s %10s %12s\n" "$agent" "$sessions" "$hr" "$current" "$rec"
done

echo ""
if [[ $write_status -eq 0 ]]; then
    echo "Calibration written to .clavain/interspect/routing-calibration.json"
    echo "Mode: shadow (log what would change). Set calibration.mode: enforce in routing.yaml to apply."
else
    echo "Warning: Failed to write calibration file."
fi
```
