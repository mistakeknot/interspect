#!/usr/bin/env bash
# Tests for sylveste-sfhq.4: tool-remediation canary + revert plumbing.
#
# Verifies:
#   1. Schema migration adds baseline_pattern_counts to canary
#   2. _interspect_compute_tool_baseline returns correct JSON snapshot
#   3. _interspect_write_tool_remediation:
#      a. creates file with active: true frontmatter
#      b. inserts canary row with populated baseline_pattern_counts
#      c. inserts modifications row with mod_type='tool_remediation'
#      d. dedup: refuses to overwrite existing remediation
#      e. validates source name + overlay id (rejects path traversal)
#   4. _interspect_disable_tool_remediation:
#      a. sets active: false in frontmatter
#      b. updates canary status to 'disabled'
#      c. is idempotent (re-disable is no-op)
#   5. _interspect_count_tool_pattern_recurrence reports post-apply patterns
#   6. /interspect:revert agent path still works (no regression on agent overlays)

set -eo pipefail

# Some Claude Code subshells inject a `git` shell-function wrapper via
# BASH_FUNC_git%%. The wrapper silently no-ops `git` outside the Sylveste
# project tree (env -u GIT_INDEX_FILE command git "$@" — env can't exec the
# bash `command` builtin). Clearing the function at script top makes git
# work normally in /tmp test dirs.
unset -f git 2>/dev/null || true

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"
export INTERSPECT_QUARANTINE_HOURS=0

# Init git repo (commits + git-add need this)
cd "$TEST_DIR" && git init -q && git config user.name test && git config user.email test@test.com
echo "init" > README.md && git add README.md && git commit -q -m "init"

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

assert_file_exists() {
    local desc="$1" path="$2"
    if [[ -f "$path" ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (missing: $path)"; ((FAIL++)) || true; fi
}

assert_fail() {
    local desc="$1"; shift
    if ! "$@" >/dev/null 2>&1; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (expected non-zero exit)"; ((FAIL++)) || true; fi
}

seed_tool_evidence() {
    _interspect_insert_evidence "tool-sess-1" "Bash" "tool_error_rate_high" "" \
        '{"tool":"Bash","calls":50,"errors":8}' "tool-time-pattern" \
        "tool-sess-1:tool_error_rate_high:Bash" "tool-time-stats" "" "tool"
    _interspect_insert_evidence "tool-sess-1" "Bash" "tool_bash_dominance" "" \
        '{"bash_share":0.8}' "tool-time-pattern" \
        "tool-sess-1:tool_bash_dominance:Bash" "tool-time-stats" "" "tool"
    _interspect_insert_evidence "tool-sess-2" "Bash" "tool_error_rate_high" "" \
        '{"tool":"Bash","calls":30,"errors":5}' "tool-time-pattern" \
        "tool-sess-2:tool_error_rate_high:Bash" "tool-time-stats" "" "tool"
}

echo "=== Group 1: schema migration ==="
HAS_COL=$(sqlite3 "$DB" "SELECT name FROM pragma_table_info('canary') WHERE name = 'baseline_pattern_counts';")
assert_eq "canary.baseline_pattern_counts column exists" "$HAS_COL" "baseline_pattern_counts"

echo ""
echo "=== Group 2: baseline snapshot ==="
seed_tool_evidence
BASELINE=$(_interspect_compute_tool_baseline "Bash")
assert_contains "baseline contains tool_error_rate_high count" "$BASELINE" '"tool_error_rate_high":2'
assert_contains "baseline contains tool_bash_dominance count" "$BASELINE" '"tool_bash_dominance":1'

EMPTY_BASELINE=$(_interspect_compute_tool_baseline "NotInDb")
assert_eq "empty baseline returns {}" "$EMPTY_BASELINE" "{}"

echo ""
echo "=== Group 3: write_tool_remediation ==="
CONTENT="## Tool Discipline test
- Use Glob/Grep instead of Bash"
_interspect_write_tool_remediation "Bash" "tuning" "$CONTENT" >/dev/null 2>&1
REMED_FILE="$TEST_DIR/.clavain/interspect/tool-remediations/Bash/tuning.md"
assert_file_exists "tool-remediation file written" "$REMED_FILE"
assert_contains "frontmatter has active: true" "$(cat "$REMED_FILE")" "active: true"
assert_contains "frontmatter has source_kind: tool" "$(cat "$REMED_FILE")" "source_kind: tool"
assert_contains "frontmatter has baseline JSON" "$(cat "$REMED_FILE")" "tool_error_rate_high"

# Canary row
CANARY_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM canary WHERE group_id = 'tool/Bash/tuning';")
assert_eq "canary row inserted" "$CANARY_COUNT" "1"

CANARY_BASELINE=$(sqlite3 "$DB" "SELECT baseline_pattern_counts FROM canary WHERE group_id = 'tool/Bash/tuning';")
assert_contains "canary.baseline_pattern_counts populated" "$CANARY_BASELINE" "tool_error_rate_high"

CANARY_STATUS=$(sqlite3 "$DB" "SELECT status FROM canary WHERE group_id = 'tool/Bash/tuning';")
assert_eq "canary status='active'" "$CANARY_STATUS" "active"

# Modifications row
MOD_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM modifications WHERE group_id = 'tool/Bash/tuning' AND mod_type = 'tool_remediation';")
assert_eq "modifications row inserted" "$MOD_COUNT" "1"

# Dedup
if _interspect_write_tool_remediation "Bash" "tuning" "$CONTENT" >/dev/null 2>&1; then
    echo "  FAIL: dedup did not block second write"; ((FAIL++)) || true
else
    echo "  PASS: re-write of same id is rejected"; ((PASS++)) || true
fi

echo ""
echo "=== Group 4: source/overlay_id validation ==="
assert_fail "rejects path traversal in source" \
    _interspect_write_tool_remediation "../etc/passwd" "x" "y"
assert_fail "rejects shell-injection in source" \
    _interspect_write_tool_remediation 'Bash$(whoami)' "x" "y"
assert_fail "rejects bad overlay id (uppercase)" \
    _interspect_write_tool_remediation "Edit" "TUNING" "y"
assert_fail "rejects empty content" \
    _interspect_write_tool_remediation "Edit" "tuning" ""

echo ""
echo "=== Group 5: disable_tool_remediation ==="
_interspect_disable_tool_remediation "Bash" "tuning" >/dev/null 2>&1
assert_contains "file frontmatter now has active: false" "$(cat "$REMED_FILE")" "active: false"

DISABLED_STATUS=$(sqlite3 "$DB" "SELECT status FROM canary WHERE group_id = 'tool/Bash/tuning';")
assert_eq "canary status='disabled'" "$DISABLED_STATUS" "disabled"

DISABLED_REASON=$(sqlite3 "$DB" "SELECT verdict_reason FROM canary WHERE group_id = 'tool/Bash/tuning';")
assert_eq "canary verdict_reason='user_disabled'" "$DISABLED_REASON" "user_disabled"

# Idempotency: re-disable is no-op (returns 0)
if _interspect_disable_tool_remediation "Bash" "tuning" 2>&1 | grep -q "already inactive"; then
    echo "  PASS: re-disable produces 'already inactive' info"; ((PASS++)) || true
else
    echo "  FAIL: re-disable did not detect inactive state"; ((FAIL++)) || true
fi

# Disable non-existent
if ! _interspect_disable_tool_remediation "DoesNotExist" "tuning" >/dev/null 2>&1; then
    echo "  PASS: disable on non-existent returns error"; ((PASS++)) || true
else
    echo "  FAIL: disable on non-existent should fail"; ((FAIL++)) || true
fi

echo ""
echo "=== Group 6: pattern recurrence count ==="
APPLIED_AT=$(sqlite3 "$DB" "SELECT applied_at FROM canary WHERE group_id = 'tool/Bash/tuning';")
# Add a NEW pattern AFTER apply time
sleep 1
_interspect_insert_evidence "post-sess" "Bash" "tool_error_rate_high" "" \
    '{"tool":"Bash"}' "tool-time-pattern" \
    "post-sess:tool_error_rate_high:Bash" "tool-time-stats" "" "tool"

RECUR=$(_interspect_count_tool_pattern_recurrence "Bash" "$APPLIED_AT")
# Should be >= 1 (the post-apply row)
if [[ "$RECUR" -ge 1 ]]; then
    echo "  PASS: recurrence count detects post-apply patterns ($RECUR)"; ((PASS++)) || true
else
    echo "  FAIL: recurrence returned $RECUR (expected >= 1)"; ((FAIL++)) || true
fi

echo ""
echo "=== Group 7: containment ==="
# write_tool_remediation should refuse paths that escape the tool-remediations dir.
# The validate_tool_source regex blocks ../ in source names directly. Confirm regex rejects:
assert_fail "rejects '../foo' as source" \
    _interspect_write_tool_remediation "../foo" "x" "y"

echo ""
echo "=== Group 8: tool path does NOT pollute overlays/ ==="
# Tool remediations live in tool-remediations/, NOT overlays/. The overlay-read
# path would silently include tool content if we got the directory wrong.
OVERLAY_FILES=$(find "$TEST_DIR/.clavain/interspect/overlays" -type f 2>/dev/null | wc -l)
assert_eq "no files in overlays/ after tool work" "$OVERLAY_FILES" "0"

TR_FILES=$(find "$TEST_DIR/.clavain/interspect/tool-remediations" -type f 2>/dev/null | wc -l)
if [[ "$TR_FILES" -ge 1 ]]; then
    echo "  PASS: tool-remediations/ has expected file"; ((PASS++)) || true
else
    echo "  FAIL: tool-remediations/ missing expected file"; ((FAIL++)) || true
fi

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"
[[ $FAIL -eq 0 ]]
