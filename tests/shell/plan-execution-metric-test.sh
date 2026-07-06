#!/usr/bin/env bash
# fc5.4 acceptance: plan_execution_outcome aggregation with source weighting.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
export CLAUDE_PROJECT_DIR="$tmp"
export INTERSPECT_QUARANTINE_HOURS=0
export INTERSPECT_PLAN_EXEC_MIN_N=3
source hooks/lib-interspect.sh
_interspect_ensure_db >/dev/null 2>&1 || true
_INTERSPECT_DB=$(_interspect_db_path)

ins() { # pass|fail  source  escalations
  local pass="$1" src="$2" esc="$3"
  local ctx
  ctx=$(jq -nc --arg a fable --arg e sonnet --arg v opus --argjson p "$pass" --argjson esc "$esc" --arg s "$src" \
    '{author_model:$a,executor_model:$e,validator_model:$v,criteria_total:3,criteria_failed:(if $p then 0 else 1 end),pass:$p,escalation_count:$esc,session_source:$s,bead:"t"}')
  _interspect_insert_evidence "sess-$RANDOM" "quality-gates" "plan_execution_outcome" "" "$ctx"
}
ins true normal 0; ins true normal 0; ins false normal 1; ins true bootstrap 0

stats=$(_interspect_compute_plan_execution_stats)
fail() { echo "FAIL: $1 — got: $stats" >&2; exit 1; }
[[ "$(echo "$stats" | jq -r '.total')" == "4" ]] || fail "total != 4"
[[ "$(echo "$stats" | jq -r '.sufficient_data')" == "true" ]] || fail "sufficient_data (min_n=3) not true"
[[ "$(echo "$stats" | jq -r '.cells | keys | length')" == "1" ]] || fail "expected 1 tier-triple cell"
# weighted: (1+1+0+0.5*1)/(1+1+1+0.5) = 2.5/3.5 ≈ 0.714 — bootstrap discounted (f-043)
wpr=$(echo "$stats" | jq -r '.cells[] | .weighted_pass_rate')
python3 -c "import sys; v=float('$wpr'); sys.exit(0 if abs(v-0.714)<0.02 else 1)" || fail "weighted_pass_rate $wpr != ~0.714 (source weighting broken)"
[[ "$(echo "$stats" | jq -r '.cells[] | .escalated')" == "1" ]] || fail "escalated count != 1 (f-027 attribution)"
echo "PASS: plan-execution metric suite"
