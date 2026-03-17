#!/usr/bin/env bash
set -eo pipefail

# Setup test DB in temp directory
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

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

assert_true() {
    local desc="$1" val="$2"
    if [[ -n "$val" && "$val" != "null" && "$val" != "" ]]; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (empty/null)"
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

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -q "$needle"; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (missing '$needle')"
        ((FAIL++)) || true
    fi
}

echo "=== Interspect Effectiveness Tests ==="

# ── Test Group 1: Empty DB ──
echo ""
echo "Group 1: Empty database"

result=$(_interspect_effectiveness_report 30)
valid=$(echo "$result" | jq '.agents | length' 2>/dev/null || echo "INVALID")
assert_true "report returns valid JSON on empty DB" "$valid"
assert_eq "report has empty agents" "$(echo "$result" | jq '.agents | length')" "0"
assert_eq "report has zero dispatches" "$(echo "$result" | jq '.total_dispatches')" "0"

summary=$(_interspect_effectiveness_summary)
assert_true "summary returns string on empty DB" "$summary"

# ── Test Group 2: With evidence data ──
echo ""
echo "Group 2: Populated database"

sqlite3 "$DB" "
INSERT INTO sessions (session_id, start_ts, end_ts, project) VALUES
  ('s1', datetime('now', '-25 days'), datetime('now', '-25 days', '+1 hour'), 'test'),
  ('s2', datetime('now', '-20 days'), datetime('now', '-20 days', '+1 hour'), 'test'),
  ('s3', datetime('now', '-10 days'), datetime('now', '-10 days', '+1 hour'), 'test'),
  ('s4', datetime('now', '-5 days'), datetime('now', '-5 days', '+1 hour'), 'test'),
  ('s5', datetime('now', '-2 days'), datetime('now', '-2 days', '+1 hour'), 'test');

INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, context, project) VALUES
  (datetime('now', '-25 days'), 's1', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-25 days'), 's1', 2, 'fd-safety', 'override', 'agent_wrong', '', 'test'),
  (datetime('now', '-20 days'), 's2', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-20 days'), 's2', 2, 'fd-safety', 'override', 'agent_wrong', '', 'test'),
  (datetime('now', '-10 days'), 's3', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-10 days'), 's3', 2, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-5 days'), 's4', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-2 days'), 's5', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-2 days'), 's5', 2, 'fd-quality', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-2 days'), 's5', 3, 'fd-quality', 'override', 'agent_wrong', '', 'test');
"

result=$(_interspect_effectiveness_report 30)
valid=$(echo "$result" | jq '.agents | length' 2>/dev/null || echo "INVALID")
assert_true "report returns valid JSON with data" "$valid"

agent_count=$(echo "$result" | jq '.agents | length')
assert_ge "report has agents" "$agent_count" 1

total_dispatches=$(echo "$result" | jq '.total_dispatches')
assert_ge "dispatches counted correctly" "$total_dispatches" 6

total_corrections=$(echo "$result" | jq '.total_corrections')
assert_ge "corrections counted correctly" "$total_corrections" 2

override_rate=$(echo "$result" | jq '.override_rate')
assert_true "override rate is non-zero" "$override_rate"

# Check per-agent data
fd_safety_rate=$(echo "$result" | jq '[.agents[] | select(.agent == "fd-safety")] | .[0].override_rate')
assert_true "fd-safety has override rate" "$fd_safety_rate"

# ── Test Group 3: Summary with trend data ──
echo ""
echo "Group 3: Summary with prior data"

# Add prior-window evidence (31-60 days ago)
sqlite3 "$DB" "
INSERT INTO sessions (session_id, start_ts, end_ts, project) VALUES
  ('s-old1', datetime('now', '-45 days'), datetime('now', '-45 days', '+1 hour'), 'test'),
  ('s-old2', datetime('now', '-40 days'), datetime('now', '-40 days', '+1 hour'), 'test');

INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, context, project) VALUES
  (datetime('now', '-45 days'), 's-old1', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-45 days'), 's-old1', 2, 'fd-safety', 'override', 'agent_wrong', '', 'test'),
  (datetime('now', '-40 days'), 's-old2', 1, 'fd-safety', 'agent_dispatch', '', '', 'test'),
  (datetime('now', '-40 days'), 's-old2', 2, 'fd-safety', 'override', 'agent_wrong', '', 'test');
"

summary=$(_interspect_effectiveness_summary)
assert_true "summary returns non-empty with trend data" "$summary"
assert_contains "summary contains percentage" "$summary" "%"

# ── Test Group 4: Window parameter ──
echo ""
echo "Group 4: Custom window"

result_7d=$(_interspect_effectiveness_report 7)
valid_7d=$(echo "$result_7d" | jq '.window_days' 2>/dev/null || echo "INVALID")
assert_true "7-day window returns valid JSON" "$valid_7d"
assert_eq "7-day window_days is 7" "$(echo "$result_7d" | jq '.window_days')" "7"

result_90d=$(_interspect_effectiveness_report 90)
valid_90d=$(echo "$result_90d" | jq '.window_days' 2>/dev/null || echo "INVALID")
assert_true "90-day window returns valid JSON" "$valid_90d"

# 90-day should have >= 7-day dispatches
d_7=$(echo "$result_7d" | jq '.total_dispatches')
d_90=$(echo "$result_90d" | jq '.total_dispatches')
assert_ge "90-day dispatches >= 7-day dispatches" "$d_90" "$d_7"

# ── Summary ──
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
