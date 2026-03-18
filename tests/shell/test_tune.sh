#!/usr/bin/env bash
set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

# Create a fake go.mod for project type detection
echo "module test" > "$TEST_DIR/go.mod"

# Init a git repo so git rev-parse works
cd "$TEST_DIR" && git init -q && git config user.name test && git config user.email test@test.com

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)

PASS=0
FAIL=0

assert_true() {
    local desc="$1" val="$2"
    if [[ -n "$val" && "$val" != "null" ]]; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (empty/null)"
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

echo "=== Overlay Generation Tests ==="

# ── Test Group 1: No evidence ──
echo ""
echo "Group 1: No evidence"

result=$(_interspect_generate_overlay "fd-safety" 2>&1) || true
assert_contains "no evidence returns error message" "$result" "No correction evidence"

# ── Test Group 2: With corrections ──
echo ""
echo "Group 2: With correction evidence"

sqlite3 "$DB" "
INSERT INTO sessions (session_id, start_ts, end_ts, project) VALUES
  ('s1', datetime('now', '-5 days'), datetime('now', '-5 days', '+1 hour'), 'test');

INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, context, project) VALUES
  (datetime('now', '-5 days'), 's1', 1, 'fd-safety', 'override', 'agent_wrong', '', 'test'),
  (datetime('now', '-5 days'), 's1', 2, 'fd-safety', 'override', 'agent_wrong', '', 'test'),
  (datetime('now', '-5 days'), 's1', 3, 'fd-safety', 'override', 'severity_miscalibrated', '', 'test'),
  (datetime('now', '-5 days'), 's1', 4, 'fd-safety', 'agent_dispatch', '', '', 'test');
"

result=$(_interspect_generate_overlay "fd-safety")
assert_true "generates overlay content" "$result"
assert_contains "contains agent name" "$result" "fd-safety"
assert_contains "contains project type" "$result" "Go"
assert_contains "contains correction pattern" "$result" "agent_wrong"
assert_contains "contains severity pattern" "$result" "severity_miscalibrated"
assert_contains "contains guidance section" "$result" "Guidance"

# ── Test Group 3: Token estimate ──
echo ""
echo "Group 3: Token counting"

tokens=$(_interspect_count_overlay_tokens "$result")
assert_true "token count is non-zero" "$tokens"
# Content should be well under 500 tokens
if [[ "$tokens" -lt 500 ]]; then
    echo "  PASS: within token budget ($tokens < 500)"
    ((PASS++)) || true
else
    echo "  FAIL: exceeds token budget ($tokens >= 500)"
    ((FAIL++)) || true
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
