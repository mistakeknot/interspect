#!/usr/bin/env bash
# Regression test for sylveste-3hyi: fresh-DB path must work under set -u.
#
# Bug: lib-interspect.sh:248-249 used $db (unset in _interspect_ensure_db scope)
# instead of $_INTERSPECT_DB. Symptoms:
#   - Trips set -u in defensive testing
#   - The two ALTER TABLE migrations for canary cohort columns silently no-op'd
#     because sqlite3 received an empty path argument

set -uo pipefail   # -u is the whole point — DO NOT REMOVE

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/hooks/lib-interspect.sh"

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (got '$got', expected '$expected')"; ((FAIL++)) || true; fi
}

echo "=== Fresh-DB initialization under set -u ==="

# This is the regression — pre-fix, _interspect_ensure_db tripped set -u on
# the unset $db variable at lines 248-249.
if _interspect_ensure_db; then
    echo "  PASS: _interspect_ensure_db succeeds under set -u"
    ((PASS++)) || true
else
    echo "  FAIL: _interspect_ensure_db failed"
    ((FAIL++)) || true
fi

DB=$(_interspect_db_path)
[[ -f "$DB" ]] && echo "  PASS: DB file created" && ((PASS++))

echo ""
echo "=== Canary cohort columns present (functional impact of bug) ==="

# Pre-fix, these ALTER TABLE statements ran with an empty path argument and
# silently no-op'd. Post-fix, the columns must exist on a fresh DB.
COHORT_KEY=$(sqlite3 "$DB" "SELECT name FROM pragma_table_info('canary') WHERE name = 'cohort_key';")
assert_eq "canary.cohort_key column exists" "$COHORT_KEY" "cohort_key"

PROJECT_COL=$(sqlite3 "$DB" "SELECT name FROM pragma_table_info('canary') WHERE name = 'project';")
assert_eq "canary.project column exists" "$PROJECT_COL" "project"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"
[[ $FAIL -eq 0 ]]
