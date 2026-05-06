#!/usr/bin/env bash
# Tests for sylveste-sfhq.3: /interspect:tune dual-mode generation.
#
# Verifies:
#   1. _interspect_generate_overlay backwards-compatible (no kind arg → agent mode)
#   2. kind=agent explicit produces same content as default
#   3. kind=tool dispatches to tool remediation path
#   4. kind=invalid returns error
#   5. _interspect_generate_tool_remediation produces remediation per pattern type
#   6. Source-name validation rejects path-traversal / shell-injection patterns
#   7. No-evidence cases return error gracefully
#   8. Tool remediation does NOT pollute .clavain/interspect/overlays/

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"
export INTERSPECT_QUARANTINE_HOURS=0

# Init a git repo so git rev-parse works
cd "$TEST_DIR" && git init -q && git config user.name test && git config user.email test@test.com

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (got '$got', expected '$expected')"; ((FAIL++)) || true; fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (no match for '$needle')"; ((FAIL++)) || true; fi
}

assert_not_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (unexpected '$needle')"; ((FAIL++)) || true; fi
}

assert_fail() {
    local desc="$1"; shift
    if ! "$@" >/dev/null 2>&1; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (expected non-zero exit)"; ((FAIL++)) || true; fi
}

# Seed agent override evidence
seed_agent_evidence() {
    for i in 1 2 3 4 5; do
        _interspect_insert_evidence "agent-sess-$i" "fd-quality" "override" "agent_wrong" '{}' "interspect-correction"
    done
    for i in 1 2 3; do
        _interspect_insert_evidence "agent-sess-$i" "fd-quality" "override" "severity_miscalibrated" '{}' "interspect-correction"
    done
}

# Seed tool pattern evidence
seed_tool_evidence() {
    _interspect_insert_evidence "tool-sess-1" "Bash" "tool_error_rate_high" "" \
        '{"tool":"Bash","calls":50,"errors":8}' "tool-time-pattern" "tool-sess-1:tool_error_rate_high:Bash" "tool-time-stats" "" "tool"
    _interspect_insert_evidence "tool-sess-1" "Bash" "tool_bash_dominance" "" \
        '{"bash_share":0.8}' "tool-time-pattern" "tool-sess-1:tool_bash_dominance:Bash" "tool-time-stats" "" "tool"
    _interspect_insert_evidence "tool-sess-2" "Bash" "tool_error_rate_high" "" \
        '{"tool":"Bash","calls":30,"errors":5}' "tool-time-pattern" "tool-sess-2:tool_error_rate_high:Bash" "tool-time-stats" "" "tool"
    _interspect_insert_evidence "tool-sess-1" "Edit" "tool_edit_without_read" "" \
        '{"count":7}' "tool-time-pattern" "tool-sess-1:tool_edit_without_read:Edit" "tool-time-stats" "" "tool"
}

echo "=== Group 1: backwards compatibility ==="
seed_agent_evidence
DEFAULT_OUT=$(_interspect_generate_overlay "fd-quality" 2>&1)
EXPLICIT_OUT=$(_interspect_generate_overlay "fd-quality" "agent" 2>&1)
assert_eq "no-kind arg matches kind=agent output" "$DEFAULT_OUT" "$EXPLICIT_OUT"
assert_contains "agent overlay contains agent name" "$DEFAULT_OUT" "fd-quality"
assert_contains "agent overlay mentions corrections" "$DEFAULT_OUT" "corrections"

echo ""
echo "=== Group 2: kind dispatch ==="
seed_tool_evidence
TOOL_OUT=$(_interspect_generate_overlay "Bash" "tool" 2>&1)
assert_contains "tool kind dispatches to tool remediation header" "$TOOL_OUT" "Tool Discipline"
assert_contains "tool remediation includes source name" "$TOOL_OUT" "Bash"
# Tool output must NOT contain agent-overlay sections
assert_not_contains "tool output does not include 'Correction Patterns' (agent header)" "$TOOL_OUT" "Correction Patterns"
assert_not_contains "tool output does not include 'Project-Specific Tuning' (agent header)" "$TOOL_OUT" "Project-Specific Tuning"

echo ""
echo "=== Group 3: invalid kind ==="
assert_fail "kind=invalid returns error" _interspect_generate_overlay "fd-quality" "invalid"
assert_fail "kind=AGENT (uppercase) rejected" _interspect_generate_overlay "fd-quality" "AGENT"

echo ""
echo "=== Group 4: tool remediation pattern coverage ==="
# Each pattern type should produce a recognizable remediation rule
assert_contains "Bash error_rate_high produces error-rate rule" "$TOOL_OUT" "error rate"
assert_contains "Bash dominance produces dominance rule" "$TOOL_OUT" "Bash overuse"

EDIT_OUT=$(_interspect_generate_overlay "Edit" "tool" 2>&1)
assert_contains "Edit tool_edit_without_read produces 'Read before Edit' rule" "$EDIT_OUT" "Read before Edit"

# Test rejection_rate_high + low_diversity by seeding additional evidence
_interspect_insert_evidence "tool-sess-3" "MultiEdit" "tool_rejection_rate_high" "" \
    '{"rate":0.3}' "tool-time-pattern" "tool-sess-3:tool_rejection_rate_high:MultiEdit" "tool-time-stats" "" "tool"
_interspect_insert_evidence "tool-sess-3" "tool-time" "tool_low_diversity" "" \
    '{"distinct":3}' "tool-time-pattern" "tool-sess-3:tool_low_diversity:tool-time" "tool-time-stats" "" "tool"

ME_OUT=$(_interspect_generate_overlay "MultiEdit" "tool" 2>&1)
assert_contains "rejection_rate produces rejection rule" "$ME_OUT" "rejection rate"

DIV_OUT=$(_interspect_generate_overlay "tool-time" "tool" 2>&1)
assert_contains "tool-time low_diversity produces diversity rule" "$DIV_OUT" "tool diversity"

echo ""
echo "=== Group 5: source-name validation ==="
# Path traversal, shell injection, SQL injection should all be rejected
assert_fail "rejects path traversal" _interspect_generate_overlay "../etc/passwd" "tool"
assert_fail "rejects forward slash" _interspect_generate_overlay "Bash/foo" "tool"
assert_fail "rejects shell-injection \$()" _interspect_generate_overlay 'Bash$(whoami)' "tool"
assert_fail "rejects empty source" _interspect_generate_overlay "" "tool"
assert_fail "rejects leading hyphen" _interspect_generate_overlay "-Bash" "tool"
# Allowed: alphanumeric, hyphens, underscores
GOOD_OUT=$(_interspect_generate_overlay "tool-time" "tool" 2>&1) && assert_contains "allows hyphens (tool-time)" "$GOOD_OUT" "Tool Discipline" || true

echo ""
echo "=== Group 6: no-evidence path ==="
NOEV=$(_interspect_generate_overlay "NotInDb" "tool" 2>&1 || true)
assert_contains "no-evidence message returned" "$NOEV" "No tool-pattern evidence"

NOEV_AGENT=$(_interspect_generate_overlay "fd-doesnotexist" "agent" 2>&1 || true)
assert_contains "no-evidence message for agent" "$NOEV_AGENT" "No correction evidence"

echo ""
echo "=== Group 7: tool remediation does NOT write overlay files ==="
# The agent overlay path is .clavain/interspect/overlays/<agent>/...
# Tool generation must not create files there as a side effect.
OVERLAYS_DIR="$TEST_DIR/.clavain/interspect/overlays"
BEFORE_COUNT=$(find "$OVERLAYS_DIR" -type f 2>/dev/null | wc -l)
_interspect_generate_overlay "Bash" "tool" >/dev/null 2>&1
AFTER_COUNT=$(find "$OVERLAYS_DIR" -type f 2>/dev/null | wc -l)
assert_eq "tool generation creates no overlay files" "$AFTER_COUNT" "$BEFORE_COUNT"

echo ""
echo "=== Group 8: idempotent re-run + content shape ==="
RUN1=$(_interspect_generate_overlay "Bash" "tool" 2>&1)
RUN2=$(_interspect_generate_overlay "Bash" "tool" 2>&1)
assert_eq "same evidence produces deterministic output" "$RUN1" "$RUN2"
assert_contains "content has CLAUDE.md hint" "$RUN1" "CLAUDE.md"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"
[[ $FAIL -eq 0 ]]
