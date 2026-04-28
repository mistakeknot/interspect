#!/usr/bin/env bash
set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect" "$TEST_DIR/.clavain/verdicts"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (got '$got', expected '$expected')"
        ((FAIL++)) || true
    fi
}

assert_ge() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" -ge "$expected" ]] 2>/dev/null; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (got '$got', expected >= '$expected')"
        ((FAIL++)) || true
    fi
}

echo "=== Interspect Calibration v2 Tests ==="

echo ""
echo "Group 1: quality-gates verdict sweep records updated verdict files"
cat > "$TEST_DIR/.clavain/verdicts/fd-quality.json" <<'JSON'
{"type":"verdict","status":"CLEAN","model":"sonnet","findings_count":1,"summary":"first","timestamp":"2026-04-28T20:00:00Z","session_id":"s-old","phase":"plan"}
JSON
_interspect_sweep_verdicts "$TEST_DIR/.clavain/verdicts" "sweep-1" >/dev/null
count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE event='verdict_outcome' AND source='fd-quality';")
assert_eq "first sweep records verdict" "$count" "1"

cat > "$TEST_DIR/.clavain/verdicts/fd-quality.json" <<'JSON'
{"type":"verdict","status":"NEEDS_ATTENTION","model":"opus","findings_count":2,"summary":"second","timestamp":"2026-04-28T21:00:00Z","session_id":"s-new","phase":"ship"}
JSON
_interspect_sweep_verdicts "$TEST_DIR/.clavain/verdicts" "sweep-2" >/dev/null
count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE event='verdict_outcome' AND source='fd-quality';")
assert_eq "second sweep records changed verdict despite marker" "$count" "2"
latest_status=$(sqlite3 "$DB" "SELECT json_extract(context, '$.status') FROM evidence WHERE event='verdict_outcome' AND source='fd-quality' ORDER BY id DESC LIMIT 1;")
assert_eq "updated verdict status captured" "$latest_status" "NEEDS_ATTENTION"

# Reset DB for calibration score assertions.
sqlite3 "$DB" "DELETE FROM evidence; DELETE FROM sessions;"

# Lower global non-bootstrap threshold for this unit-sized fixture.
_INTERSPECT_CALIBRATION_MIN_NON_BOOTSTRAP=3

echo ""
echo "Group 2: agent scores include phase-specific recommendations"
sqlite3 "$DB" "
INSERT INTO sessions (session_id, start_ts, end_ts, project, source) VALUES
  ('plan-1', datetime('now','-6 days'), datetime('now','-6 days','+1 hour'), 'test', 'normal'),
  ('plan-2', datetime('now','-5 days'), datetime('now','-5 days','+1 hour'), 'test', 'normal'),
  ('plan-3', datetime('now','-4 days'), datetime('now','-4 days','+1 hour'), 'test', 'normal'),
  ('ship-1', datetime('now','-3 days'), datetime('now','-3 days','+1 hour'), 'test', 'normal'),
  ('ship-2', datetime('now','-2 days'), datetime('now','-2 days','+1 hour'), 'test', 'normal'),
  ('ship-3', datetime('now','-1 days'), datetime('now','-1 days','+1 hour'), 'test', 'normal');
"
_interspect_record_verdict "plan-1" "fd-game-design" "CLEAN" 1 "sonnet" "plan" >/dev/null
_interspect_record_verdict "plan-2" "fd-game-design" "CLEAN" 1 "sonnet" "plan" >/dev/null
_interspect_record_verdict "plan-3" "fd-game-design" "CLEAN" 1 "sonnet" "plan" >/dev/null
_interspect_record_verdict "ship-1" "fd-game-design" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null
_interspect_record_verdict "ship-2" "fd-game-design" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null
_interspect_record_verdict "ship-3" "fd-game-design" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null

scores=$(_interspect_compute_agent_scores)
agent_count=$(echo "$scores" | jq 'length')
assert_ge "score output has agent" "$agent_count" 1
plan_model=$(echo "$scores" | jq -r '.[0].phases.plan.recommended_model // empty')
ship_model=$(echo "$scores" | jq -r '.[0].phases.ship.recommended_model // empty')
assert_eq "plan phase recommends demotion from low hit rate" "$plan_model" "haiku"
assert_eq "ship phase keeps sonnet from high hit rate" "$ship_model" "sonnet"

_interspect_write_routing_calibration
schema_version=$(jq -r '.schema_version' "$TEST_DIR/.clavain/interspect/routing-calibration.json")
phase_model=$(jq -r '.agents["fd-game-design"].phases.plan.recommended_model // empty' "$TEST_DIR/.clavain/interspect/routing-calibration.json")
assert_eq "routing calibration writes schema v2" "$schema_version" "2"
assert_eq "routing calibration persists phase scores" "$phase_model" "haiku"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
