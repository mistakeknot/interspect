#!/usr/bin/env bash
# Tests for sylveste-ioe7: interlab -> interspect mutation feedback loop.
#
# Verifies:
#   1. interspect-interlab-mutation hook_id is allowlisted
#   2. _interspect_consume_interlab_mutations ingests is_new_best mutations as
#      pattern-kind evidence (source=interlab:<task_type>, event=mutation_best)
#   3. Non-winning mutations (is_new_best=0) are NOT ingested
#   4. The cursor advances and the consumer is idempotent (re-run adds nothing)
#   5. A missing mutations.db is a clean no-op (exit 0, no error)
#   6. The ingested evidence is picked up by the pattern classifier (loop closed)

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

# Point the consumer at a synthetic interlab mutation store (not the real one).
export INTERLAB_MUTATIONS_DB="$TEST_DIR/mutations.db"
# Quarantine off so ingested rows are immediately visible to the classifier.
export INTERSPECT_QUARANTINE_HOURS=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)

PASS=0
FAIL=0
assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then
        echo "  PASS: $desc"; ((PASS++)) || true
    else
        echo "  FAIL: $desc (got '$got', expected '$expected')"; ((FAIL++)) || true
    fi
}
assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  PASS: $desc"; ((PASS++)) || true
    else
        echo "  FAIL: $desc (no match for '$needle' in '$haystack')"; ((FAIL++)) || true
    fi
}

# `ic` is required for cursor persistence; skip gracefully if absent.
if ! command -v ic &>/dev/null; then
    echo "SKIP: ic not available — cursor persistence cannot be tested"
    exit 0
fi
CURSOR_KEY="interspect-interlab-mutation-cursor"
# The cursor lives in shared global `ic state`. Save the real value and restore
# it on exit so running this test never corrupts a production cursor (which
# would cause duplicate re-ingest of real mutations on the next session-end).
_SAVED_CURSOR=$(ic state get "$CURSOR_KEY" "global" 2>/dev/null || echo "")
restore_cursor() {
    if [[ -n "$_SAVED_CURSOR" ]]; then
        echo "$_SAVED_CURSOR" | ic state set "$CURSOR_KEY" "global" 2>/dev/null || true
    fi
}
trap 'restore_cursor; rm -rf "$TEST_DIR"' EXIT
echo "0" | ic state set "$CURSOR_KEY" "global" 2>/dev/null || true

echo "=== Allowlist ==="
if _interspect_validate_hook_id "interspect-interlab-mutation"; then
    echo "  PASS: interspect-interlab-mutation hook_id allowlisted"; ((PASS++)) || true
else
    echo "  FAIL: interspect-interlab-mutation hook_id not allowlisted"; ((FAIL++)) || true
fi

echo ""
echo "=== Missing mutations.db is a clean no-op ==="
rm -f "$INTERLAB_MUTATIONS_DB"
if _interspect_consume_interlab_mutations "sess-nodb"; then
    echo "  PASS: missing db returns 0"; ((PASS++)) || true
else
    echo "  FAIL: missing db returned non-zero"; ((FAIL++)) || true
fi
NODB_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "no evidence ingested when db absent" "$NODB_ROWS" "0"

echo ""
echo "=== Build a synthetic interlab mutation store ==="
sqlite3 "$INTERLAB_MUTATIONS_DB" <<'SQL'
CREATE TABLE mutations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    quality_signal REAL NOT NULL,
    is_new_best INTEGER NOT NULL DEFAULT 0,
    inspired_by TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
INSERT INTO mutations (task_type, hypothesis, quality_signal, is_new_best, campaign_id, created_at) VALUES
  ('routing-tune', 'Baseline approach',        2.0, 1, 'camp-a', '2026-06-01T00:00:00Z'),
  ('routing-tune', 'Tweaked decay - no gain',  2.0, 0, 'camp-a', '2026-06-01T01:00:00Z'),
  ('routing-tune', 'New best decay schedule',  5.0, 1, 'camp-a', '2026-06-01T02:00:00Z');
SQL

echo ""
echo "=== Consume: only is_new_best rows become pattern evidence ==="
_interspect_consume_interlab_mutations "sess-1"
WIN_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "ingested 2 winning mutations (ids 1,3), skipped the non-winner" "$WIN_ROWS" "2"

SRC_KIND=$(sqlite3 "$DB" "SELECT DISTINCT source_kind FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "evidence is pattern-kind, not agent" "$SRC_KIND" "pattern"

SRC=$(sqlite3 "$DB" "SELECT DISTINCT source FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "source is interlab:<task_type>" "$SRC" "interlab:routing-tune"

EVT=$(sqlite3 "$DB" "SELECT DISTINCT event FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "event is mutation_best" "$EVT" "mutation_best"

IDS=$(sqlite3 "$DB" "SELECT GROUP_CONCAT(source_event_id) FROM evidence WHERE source LIKE 'interlab:%' ORDER BY source_event_id;")
assert_eq "ingested mutation ids 1 and 3 (skipped 2)" "$IDS" "1,3"

CTX=$(sqlite3 "$DB" "SELECT context FROM evidence WHERE source LIKE 'interlab:%' AND source_event_id='3';")
assert_contains "winning hypothesis carried in context" "$CTX" "New best decay schedule"

echo ""
echo "=== Idempotency: re-run ingests nothing new ==="
_interspect_consume_interlab_mutations "sess-2"
RERUN_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "re-run did not duplicate (still 2)" "$RERUN_ROWS" "2"

echo ""
echo "=== Cursor advanced to the max mutation id ==="
CURSOR=$(ic state get "$CURSOR_KEY" "global" 2>/dev/null)
assert_eq "cursor advanced to 3" "$CURSOR" "3"

echo ""
echo "=== Loop is closed: classifier surfaces the mutation pattern ==="
# Two winning routing-tune mutations → >=2 of same (source,event) → classified.
PATTERNS=$(_interspect_get_classified_patterns 2>/dev/null || true)
assert_contains "interlab pattern surfaced by classifier" "$PATTERNS" "interlab:routing-tune|mutation_best"

echo ""
echo "=== New winner after the cursor is picked up incrementally ==="
sqlite3 "$INTERLAB_MUTATIONS_DB" "INSERT INTO mutations (task_type, hypothesis, quality_signal, is_new_best, created_at) VALUES ('routing-tune', 'Even better', 9.0, 1, '2026-06-02T00:00:00Z');"
_interspect_consume_interlab_mutations "sess-3"
INCR_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source LIKE 'interlab:%';")
assert_eq "incremental ingest picked up the new winner (now 3)" "$INCR_ROWS" "3"

echo ""
echo "================================"
echo "PASS: $PASS  FAIL: $FAIL"
[[ "$FAIL" -eq 0 ]]
