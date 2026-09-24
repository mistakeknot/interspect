#!/usr/bin/env bash
# Tests for Sylveste-06i.4 Option A: severity-tier confidence gate.
#
# Verifies:
#   1. Migration adds low_confidence/corroborated_at columns on existing DBs
#   2. Fresh-DB CREATE includes both columns
#   3. A low_confidence=true evidence row is quarantined indefinitely (sentinel)
#      and does NOT count toward agent_wrong routing eligibility
#   4. A second, independent-session flag for the same finding_id corroborates
#      both rows: gate lifts, eligibility now counts them
#   5. _interspect_corroborate_evidence lifts the gate explicitly
#   6. _interspect_low_confidence_gate_stats reports flagged/corroborated counts
#   7. Existing (non-flagged) evidence behaviour is unchanged

set -eo pipefail

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

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (no match for '$needle' in '$haystack')"
        ((FAIL++)) || true
    fi
}

echo "=== Schema migration ==="

for col in low_confidence corroborated_at; do
    COL_INFO=$(sqlite3 "$DB" "SELECT name, dflt_value FROM pragma_table_info('evidence') WHERE name = '$col';")
    assert_eq "$col column exists with default 0" "$COL_INFO" "${col}|0"
done

echo ""
echo "=== Gate: flag blocks exclusion until corroborated ==="

export INTERSPECT_QUARANTINE_HOURS=0

# Single-judge P0/P1 (or severity-boundary) finding: caller marks it low_confidence.
_interspect_insert_evidence "sess-1" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P0-1"}' "interspect-correction"

ROW=$(sqlite3 "$DB" "SELECT low_confidence, quarantine_until, corroborated_at FROM evidence WHERE session_id='sess-1';")
assert_eq "flagged row: low_confidence=1, sentinel quarantine, not corroborated" \
    "$ROW" "1|${_INTERSPECT_LOW_CONFIDENCE_SENTINEL}|0"

RESULT=$(_interspect_is_routing_eligible fd-safety) || true
assert_contains "flagged-only evidence: not eligible (no counted override events)" "$RESULT" "not_eligible"

echo ""
echo "=== Corroboration via a second independent session ==="

# A different judge/person independently flags the same finding_id.
_interspect_insert_evidence "sess-2" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P0-1"}' "interspect-correction"

ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "both rows corroborated after 2nd independent flag" "$ROWS" "2"

RESULT=$(_interspect_is_routing_eligible fd-safety) || true
assert_eq "corroborated evidence now drives eligibility (100% wrong >= threshold)" "$RESULT" "eligible"

echo ""
echo "=== _interspect_corroborate_evidence: explicit corroboration ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-3" "fd-correctness" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P1-9"}' "interspect-correction"
GATED_BEFORE=$(sqlite3 "$DB" "SELECT quarantine_until FROM evidence WHERE session_id='sess-3';")
assert_eq "explicit-corroborate test: starts gated" "$GATED_BEFORE" "$_INTERSPECT_LOW_CONFIDENCE_SENTINEL"

AFFECTED=$(_interspect_corroborate_evidence "fd-correctness" "P1-9")
assert_eq "_interspect_corroborate_evidence reports 1 row lifted" "$AFFECTED" "1"

GATED_AFTER=$(sqlite3 "$DB" "SELECT quarantine_until, corroborated_at > 0 FROM evidence WHERE session_id='sess-3';")
assert_eq "gate lifted, corroborated_at stamped" "$GATED_AFTER" "0|1"

echo ""
echo "=== Gate stats (Option B decision data) ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-a" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P0-a"}' "interspect-correction"
_interspect_insert_evidence "sess-b" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P0-b"}' "interspect-correction"
_interspect_insert_evidence "sess-c" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"finding_id":"P0-b"}' "interspect-correction"

STATS=$(_interspect_low_confidence_gate_stats)
assert_eq "3 flagged, 2 corroborated (P0-b pair), 1 still gated (P0-a)" "$STATS" "3|2|1"

echo ""
echo "=== Existing (non-flagged) behaviour is unchanged ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-x" "fd-quality" "override" "agent_wrong" '{}' "interspect-correction"
ROW=$(sqlite3 "$DB" "SELECT low_confidence, quarantine_until FROM evidence WHERE session_id='sess-x';")
assert_eq "unflagged evidence: low_confidence=0, normal (non-sentinel) quarantine" "$ROW" "0|0"

RESULT=$(_interspect_is_routing_eligible fd-quality) || true
assert_eq "unflagged evidence still drives eligibility normally" "$RESULT" "eligible"

echo ""
echo "=== Idempotency ==="

_interspect_ensure_db
for col in low_confidence corroborated_at; do
    COL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('evidence') WHERE name = '$col';")
    assert_eq "$col column count = 1 after re-run" "$COL_COUNT" "1"
done

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
