#!/usr/bin/env bash
# SessionStart hook: record session start in Interspect evidence store.
#
# Inserts a row into the sessions table. Silent — no output, no context injection.
# Runs async alongside the main session-start.sh hook.
#
# Input: Hook JSON on stdin (session_id)
# Output: None
# Exit: 0 always (fail-open)

set -uo pipefail
trap 'exit 0' ERR

# Guard: fail-open if dependencies unavailable
command -v jq &>/dev/null || exit 0
command -v sqlite3 &>/dev/null || exit 0

# Read hook input
INPUT=$(cat)

# Extract session_id
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty') || exit 0
[[ -z "$SESSION_ID" ]] && exit 0

# Source shared library
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib-interspect.sh"

# Ensure DB exists
_interspect_ensure_db || exit 0

# Record session start
PROJECT=$(_interspect_project_name)
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# SQL-escape
E_SID="${SESSION_ID//\'/\'\'}"
E_PROJECT="${PROJECT//\'/\'\'}"

sqlite3 "$_INTERSPECT_DB" \
    "INSERT OR IGNORE INTO sessions (session_id, start_ts, project) VALUES ('${E_SID}', '${TS}', '${E_PROJECT}');" \
    2>/dev/null || true

# Resolve active ic run for this project (fail-open)
RUN_ID=""
if command -v ic &>/dev/null; then
    PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
    RUN_ID=$(ic run current --project="$PROJECT_ROOT" 2>/dev/null) || RUN_ID=""
fi

# Store run_id if available (UPDATE is harmless if empty)
if [[ -n "$RUN_ID" ]]; then
    E_RUN_ID="${RUN_ID//\'/\'\'}"
    sqlite3 "$_INTERSPECT_DB" \
        "UPDATE sessions SET run_id = '${E_RUN_ID}' WHERE session_id = '${E_SID}';" \
        2>/dev/null || true
fi

# Consume kernel events (catch up since last session)
_interspect_consume_kernel_events "$SESSION_ID" 2>/dev/null || true

# Classify session source for calibration weighting
BEAD_ID=$(cat /tmp/interstat-bead-"${SESSION_ID}" 2>/dev/null || echo "")
SESSION_SOURCE="normal"
SESSION_SOURCE=$(_interspect_classify_session_source "$BEAD_ID" 2>/dev/null) || true
_interspect_update_session_source "$SESSION_ID" "$SESSION_SOURCE" 2>/dev/null || true

# Sweep unrecorded verdicts from previous quality-gates runs
# Use absolute path — hook CWD may be plugin install dir, not project root
_SWEEP_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
_interspect_sweep_verdicts "${_SWEEP_ROOT}/.clavain/verdicts" "$SESSION_ID" 2>/dev/null || true

# Check for canary alerts — evaluate completed canaries first
_interspect_check_canaries >/dev/null 2>&1 || true

# Build session-start summary (iv-m6cd): active overrides + canary alerts
SUMMARY_PARTS=()

# Active routing overrides
ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
OVERRIDES_FILE="${ROOT}/${FLUX_ROUTING_OVERRIDES_PATH:-.claude/routing-overrides.json}"
if [[ -f "$OVERRIDES_FILE" ]]; then
    OVERRIDE_COUNT=$(jq '[.overrides[] | select(.action == "exclude")] | length' "$OVERRIDES_FILE" 2>/dev/null || echo "0")
    PROPOSE_COUNT=$(jq '[.overrides[] | select(.action == "propose")] | length' "$OVERRIDES_FILE" 2>/dev/null || echo "0")
    if (( OVERRIDE_COUNT > 0 )); then
        EXCLUDED_AGENTS=$(jq -r '[.overrides[] | select(.action == "exclude") | .agent] | join(", ")' "$OVERRIDES_FILE" 2>/dev/null || echo "")
        SUMMARY_PARTS+=("Interspect: ${OVERRIDE_COUNT} active exclusion(s): ${EXCLUDED_AGENTS}")
    fi
    if (( PROPOSE_COUNT > 0 )); then
        PROPOSED_AGENTS=$(jq -r '[.overrides[] | select(.action == "propose") | .agent] | join(", ")' "$OVERRIDES_FILE" 2>/dev/null || echo "")
        SUMMARY_PARTS+=("Interspect: ${PROPOSE_COUNT} pending proposal(s): ${PROPOSED_AGENTS}. Run /interspect:approve <agent> to apply.")
    fi
fi

# Evidence stats
EVIDENCE_COUNT=$(sqlite3 "$_INTERSPECT_DB" "SELECT COUNT(*) FROM evidence;" 2>/dev/null || echo "0")
SESSION_COUNT=$(sqlite3 "$_INTERSPECT_DB" "SELECT COUNT(*) FROM sessions;" 2>/dev/null || echo "0")
if (( EVIDENCE_COUNT > 0 )); then
    SUMMARY_PARTS+=("Interspect: ${EVIDENCE_COUNT} evidence events across ${SESSION_COUNT} sessions.")
fi

# Canary alerts (highest priority — shown last so it's the final thing read)
ALERT_COUNT=$(sqlite3 "$_INTERSPECT_DB" "SELECT COUNT(*) FROM canary WHERE status = 'alert';" 2>/dev/null || echo "0")
if (( ALERT_COUNT > 0 )); then
    ALERT_AGENTS=$(sqlite3 -separator ', ' "$_INTERSPECT_DB" "SELECT group_id FROM canary WHERE status = 'alert';" 2>/dev/null || echo "")
    SUMMARY_PARTS+=("WARNING: Canary alert for ${ALERT_AGENTS} — review quality may have degraded. Run /interspect:status or /interspect:revert <agent>.")
fi

# Inject active overlays into session context (fail-open)
ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
OVERLAY_DIR="${ROOT}/.clavain/interspect/overlays"
if [[ -d "$OVERLAY_DIR" ]]; then
    OVERLAY_TOKENS=0
    MAX_OVERLAY_TOKENS=2000
    for agent_dir in "$OVERLAY_DIR"/*/; do
        [[ -d "$agent_dir" ]] || continue
        agent=$(basename "$agent_dir")
        agent_content=$(_interspect_read_overlays "$agent" 2>/dev/null) || continue
        [[ -z "$agent_content" ]] && continue
        tokens=$(_interspect_count_overlay_tokens "$agent_content")
        new_total=$((OVERLAY_TOKENS + tokens))
        if (( new_total > MAX_OVERLAY_TOKENS )); then
            break
        fi
        OVERLAY_TOKENS=$new_total
        SUMMARY_PARTS+=("[Interspect tuning for ${agent}]"$'\n'"${agent_content}")
    done
fi

# Emit summary as additionalContext if there's anything to report
if (( ${#SUMMARY_PARTS[@]} > 0 )); then
    # Join parts with newlines (safe via jq)
    SUMMARY=$(printf '%s\n' "${SUMMARY_PARTS[@]}")
    jq -n --arg ctx "$SUMMARY" '{"additionalContext":$ctx}'
fi

exit 0
