#!/usr/bin/env bash
# Tests for Sylveste-06i.4 Option A: severity-tier confidence gate.
#
# Verifies:
#   1. Migration adds low_confidence/corroborated_at/finding_key columns on
#      existing (pre-gate-schema) DBs, and existing evidence stays eligible
#   2. Fresh-DB CREATE includes all three columns
#   3. A low_confidence=true evidence row (review_id+finding_id) is
#      quarantined indefinitely (sentinel) and does NOT count toward
#      agent_wrong routing eligibility
#   4. A second, independent-session flag for the same review_id:finding_id
#      (matching override_reason) corroborates both rows: gate lifts,
#      eligibility now counts them
#   5. Same session flagging twice does NOT corroborate (M5.ii)
#   6. A different run's same positional finding_id (M1) does NOT corroborate
#   7. A disagreeing override_reason (M2) does NOT corroborate
#   8. A >500-char / sanitizer-mangled context does not break corroboration
#      for other findings (M3)
#   9. _interspect_corroborate_evidence lifts the gate explicitly, atomically,
#      and refuses to let the flagging session corroborate itself
#  10. _interspect_low_confidence_gate_stats reports flagged/corroborated counts
#  11. Existing (non-flagged) evidence behaviour is unchanged

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"

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

echo "=== Schema migration: existing pre-gate-schema DB (M5.i) ==="

# Build a DB with the pre-round-2 schema (no low_confidence/corroborated_at/
# finding_key columns) and a couple of rows, exactly as an already-deployed
# copy of the plugin would have left it, then run _interspect_ensure_db and
# confirm the migration adds the columns with safe defaults and existing rows
# stay equally eligible (matches the round-1 reviewer's own probe).
OLD_PROJECT_DIR="$TEST_DIR/old-project"
mkdir -p "$OLD_PROJECT_DIR/.clavain/interspect"
OLD_DB="$OLD_PROJECT_DIR/.clavain/interspect/interspect.db"
sqlite3 "$OLD_DB" "
    CREATE TABLE evidence (
        ts TEXT, session_id TEXT, seq INTEGER, source TEXT, source_version TEXT,
        event TEXT, override_reason TEXT, context TEXT, project TEXT,
        project_lang TEXT, project_type TEXT
    );
    INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, context, project)
    VALUES ('2026-01-01T00:00:00Z', 'old-sess-1', 1, 'fd-old', 'override', 'agent_wrong', '{}', 'test');
    INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, context, project)
    VALUES ('2026-01-01T00:00:01Z', 'old-sess-2', 2, 'fd-old', 'override', 'agent_wrong', '{}', 'test');
"
CLAUDE_PROJECT_DIR="$OLD_PROJECT_DIR" _interspect_ensure_db

for col in low_confidence corroborated_at finding_key; do
    COL_COUNT=$(sqlite3 "$OLD_DB" "SELECT COUNT(*) FROM pragma_table_info('evidence') WHERE name = '$col';")
    assert_eq "old-schema DB: $col column added by migration" "$COL_COUNT" "1"
done
OLD_ROWS=$(sqlite3 "$OLD_DB" "SELECT low_confidence, corroborated_at, finding_key FROM evidence WHERE source='fd-old' ORDER BY seq;")
assert_eq "old-schema DB: pre-existing rows got safe defaults (0/0/NULL)" "$OLD_ROWS" "0|0|
0|0|"
RESULT=$(CLAUDE_PROJECT_DIR="$OLD_PROJECT_DIR" _interspect_is_routing_eligible fd-old) || true
assert_eq "old-schema DB: pre-existing evidence equally eligible after migration" "$RESULT" "eligible"

echo ""
echo "=== Schema migration: fresh DB ==="

_interspect_ensure_db
DB=$(_interspect_db_path)
for col in low_confidence corroborated_at finding_key; do
    COL_INFO=$(sqlite3 "$DB" "SELECT name FROM pragma_table_info('evidence') WHERE name = '$col';")
    assert_eq "fresh DB: $col column exists" "$COL_INFO" "$col"
done

echo ""
echo "=== Gate: flag blocks exclusion until corroborated ==="

export INTERSPECT_QUARANTINE_HOURS=0

# Single-judge P0/P1 (or severity-boundary) finding: caller marks it
# low_confidence and passes a run-scoped review_id alongside the positional
# finding_id (M1) — the two combine into finding_key.
_interspect_insert_evidence "sess-1" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-x","finding_id":"P0-1"}' "interspect-correction"

ROW=$(sqlite3 "$DB" "SELECT low_confidence, quarantine_until, corroborated_at, finding_key FROM evidence WHERE session_id='sess-1';")
assert_eq "flagged row: low_confidence=1, sentinel quarantine, not corroborated, finding_key set" \
    "$ROW" "1|${_INTERSPECT_LOW_CONFIDENCE_SENTINEL}|0|run-x:P0-1"

RESULT=$(_interspect_is_routing_eligible fd-safety) || true
assert_contains "flagged-only evidence: not eligible (no counted override events)" "$RESULT" "not_eligible"

echo ""
echo "=== Corroboration via a second independent session (matching run + reason) ==="

_interspect_insert_evidence "sess-2" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-x","finding_id":"P0-1"}' "interspect-correction"

ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "both rows corroborated after 2nd independent flag" "$ROWS" "2"

RESULT=$(_interspect_is_routing_eligible fd-safety) || true
assert_eq "corroborated evidence now drives eligibility (100% wrong >= threshold)" "$RESULT" "eligible"

echo ""
echo "=== M5.ii: same session flagging twice does NOT corroborate ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-same" "fd-quality" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-same","finding_id":"P0-1"}' "interspect-correction"
_interspect_insert_evidence "sess-same" "fd-quality" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-same","finding_id":"P0-1"}' "interspect-correction"
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "same session flagging the same finding twice: still 0 corroborated" "$ROWS" "0"

echo ""
echo "=== M1 regression: cross-run finding_id collision does NOT corroborate ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-run1" "fd-arch" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-1","finding_id":"P0-1"}' "interspect-correction"
_interspect_insert_evidence "sess-run2" "fd-arch" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-2","finding_id":"P0-1"}' "interspect-correction"
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "different runs' same positional P0-1: 0 corroborated (namespaced key)" "$ROWS" "0"

echo ""
echo "=== M2 regression: disagreeing override_reason does NOT corroborate ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-r1" "fd-people" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-m2","finding_id":"P1-3"}' "interspect-correction"
_interspect_insert_evidence "sess-r2" "fd-people" "override" "deprioritized" \
    '{"low_confidence":true,"review_id":"run-m2","finding_id":"P1-3"}' "interspect-correction"
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "agent_wrong then deprioritized on same finding: 0 corroborated (reason mismatch)" "$ROWS" "0"
STILL_GATED=$(sqlite3 "$DB" "SELECT quarantine_until FROM evidence WHERE session_id='sess-r1';")
assert_eq "original agent_wrong row stays gated" "$STILL_GATED" "$_INTERSPECT_LOW_CONFIDENCE_SENTINEL"

echo ""
echo "=== M3 regression: oversized/sanitizer-mangled context on one row doesn't break another ==="

sqlite3 "$DB" "DELETE FROM evidence;"
LONG_DESC=$(printf 'x%.0s' {1..600})
# Row with a >500-char description (gets truncated/mangled by _interspect_sanitize)
_interspect_insert_evidence "sess-long" "fd-quality" "override" "agent_wrong" \
    "{\"low_confidence\":true,\"review_id\":\"run-m3\",\"finding_id\":\"P0-9\",\"description\":\"${LONG_DESC}\"}" "interspect-correction"
# A second, unrelated flagged+gated finding that should corroborate normally
_interspect_insert_evidence "sess-other-a" "fd-quality" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-m3","finding_id":"P0-8"}' "interspect-correction"
_interspect_insert_evidence "sess-other-b" "fd-quality" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-m3","finding_id":"P0-8"}' "interspect-correction"
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "P0-8 pair corroborates despite P0-9's oversized context (finding_key isn't json_extract)" "$ROWS" "2"
LONG_ROW_KEY=$(sqlite3 "$DB" "SELECT finding_key FROM evidence WHERE session_id='sess-long';")
assert_eq "oversized-context row still got a clean finding_key (read pre-sanitize)" "$LONG_ROW_KEY" "run-m3:P0-9"

echo ""
echo "=== _interspect_corroborate_evidence: explicit corroboration ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-3" "fd-correctness" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-y","finding_id":"P1-9"}' "interspect-correction"
GATED_BEFORE=$(sqlite3 "$DB" "SELECT quarantine_until FROM evidence WHERE session_id='sess-3';")
assert_eq "explicit-corroborate test: starts gated" "$GATED_BEFORE" "$_INTERSPECT_LOW_CONFIDENCE_SENTINEL"

# The flagging session itself may not self-corroborate (N3).
SELF_AFFECTED=$(_interspect_corroborate_evidence "fd-correctness" "run-y" "P1-9" "sess-3")
assert_eq "self-corroboration (same session) is refused" "$SELF_AFFECTED" "0"

AFFECTED=$(_interspect_corroborate_evidence "fd-correctness" "run-y" "P1-9" "sess-reviewer")
assert_eq "_interspect_corroborate_evidence reports 1 row lifted by a different actor" "$AFFECTED" "1"

GATED_AFTER=$(sqlite3 "$DB" "SELECT quarantine_until, corroborated_at > 0 FROM evidence WHERE session_id='sess-3';")
assert_eq "gate lifted, corroborated_at stamped" "$GATED_AFTER" "0|1"

echo ""
echo "=== Gate stats (Option B decision data) ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-a" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-z","finding_id":"P0-a"}' "interspect-correction"
_interspect_insert_evidence "sess-b" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-z","finding_id":"P0-b"}' "interspect-correction"
_interspect_insert_evidence "sess-c" "fd-safety" "override" "agent_wrong" \
    '{"low_confidence":true,"review_id":"run-z","finding_id":"P0-b"}' "interspect-correction"

STATS=$(_interspect_low_confidence_gate_stats)
assert_eq "3 flagged, 2 corroborated (P0-b pair), 1 still gated (P0-a)" "$STATS" "3|2|1"

echo ""
echo "=== Gate stats on an empty DB (N1: no bare '0||') ==="

sqlite3 "$DB" "DELETE FROM evidence;"
STATS=$(_interspect_low_confidence_gate_stats)
assert_eq "empty DB: 0 flagged, 0 corroborated, 0 still gated" "$STATS" "0|0|0"

echo ""
echo "=== Round-2 M1: rerun of the same target must not share a review_id ==="

# flux-drive's output-dir basename is deliberately stable across reruns of
# the same target (SKILL.md ~128-133), so the bare basename used to be the
# whole review_id — two reruns collided. _interspect_review_id_from_findings
# must fold in synthesis_timestamp so reruns diverge.
RUN_DIR="$TEST_DIR/flux-drive-abc12345"
mkdir -p "$RUN_DIR"

if command -v _interspect_review_id_from_findings >/dev/null 2>&1; then
    cat > "$RUN_DIR/findings.json" <<'JSON'
{"synthesis_timestamp": "2026-09-24T01:00:00Z", "findings": []}
JSON
    RID1=$(_interspect_review_id_from_findings "$RUN_DIR/findings.json")

    cat > "$RUN_DIR/findings.json" <<'JSON'
{"synthesis_timestamp": "2026-09-24T02:00:00Z", "findings": []}
JSON
    RID2=$(_interspect_review_id_from_findings "$RUN_DIR/findings.json")
else
    RID1="<missing-fn>"
    RID2="<missing-fn>"
fi

assert_eq "M1: rerun 1 review_id incorporates synthesis_timestamp" \
    "$RID1" "flux-drive-abc12345@2026-09-24T01:00:00Z"
assert_eq "M1: rerun 2 (same output-dir basename) gets a DIFFERENT review_id" \
    "$RID2" "flux-drive-abc12345@2026-09-24T02:00:00Z"

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-rerun1" "fd-m1" "override" "agent_wrong" \
    "{\"low_confidence\":true,\"review_id\":\"$RID1\",\"finding_id\":\"P0-1\"}" "interspect-correction"
_interspect_insert_evidence "sess-rerun2" "fd-m1" "override" "agent_wrong" \
    "{\"low_confidence\":true,\"review_id\":\"$RID2\",\"finding_id\":\"P0-1\"}" "interspect-correction"
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE low_confidence=1 AND corroborated_at > 0;")
assert_eq "M1: two reruns of the same target (same basename, different synthesis_timestamp) do NOT corroborate" "$ROWS" "0"

echo ""
echo "=== Round-2 M4: automatic disagreement path (severity_miscalibrated) must be gated ==="

# resolve.md's step 5b emits disagreement_resolved events from a single
# resolving session with no second judge. The severity_overridden/no-
# dismissal-reason branch (chosen_severity = mechanical max of the panel's
# own ratings, synthesis.md Rule 4) is exactly the boundary-noise case
# Option A exists to gate — it must NOT auto-drive agent_wrong/
# severity_miscalibrated exclusion from one session's resolution alone.
sqlite3 "$DB" "DELETE FROM evidence;"

EVENT_JSON=$(jq -n '{
    id: "evt-m4-1",
    finding_id: "P1-4",
    resolution: "accepted",
    chosen_severity: "P0",
    impact: "severity_overridden",
    dismissal_reason: "",
    session_id: "sess-resolver-1",
    agents_json: {"fd-safety": "P1"}
}')
_interspect_process_disagreement_event "$EVENT_JSON" 2>/dev/null || true

ROW=$(sqlite3 "$DB" "SELECT low_confidence, quarantine_until FROM evidence WHERE source='fd-safety' AND override_reason='severity_miscalibrated';")
assert_eq "M4: severity_miscalibrated evidence from a single resolving session is gated (low_confidence=1, sentinel quarantine)" \
    "$ROW" "1|${_INTERSPECT_LOW_CONFIDENCE_SENTINEL}"

RESULT=$(_interspect_is_routing_eligible fd-safety) || true
assert_contains "M4: single-session severity_miscalibrated evidence does NOT drive agent_wrong routing eligibility by itself" "$RESULT" "not_eligible"

# A second, independent resolving session hitting the SAME finding
# corroborates it exactly like a manual correction would, once the event
# producer supplies a matching review_run_id (accepted via optional
# .review_run_id on the event; absent today, so this row stays gated on its
# own finding_id-only key until a producer-side follow-up threads one
# through — see the fix comment in lib-interspect.sh).
EVENT_JSON_2=$(jq -n '{
    id: "evt-m4-2",
    finding_id: "P1-4",
    resolution: "accepted",
    chosen_severity: "P0",
    impact: "severity_overridden",
    dismissal_reason: "",
    session_id: "sess-resolver-2",
    agents_json: {"fd-safety": "P1"}
}')
_interspect_process_disagreement_event "$EVENT_JSON_2" 2>/dev/null || true
ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source='fd-safety' AND override_reason='severity_miscalibrated' AND low_confidence=1 AND corroborated_at > 0;")
assert_eq "M4: a second independent resolving session on the same finding corroborates the gate" "$ROWS" "2"

echo ""
echo "=== Existing (non-flagged) behaviour is unchanged ==="

sqlite3 "$DB" "DELETE FROM evidence;"
_interspect_insert_evidence "sess-x" "fd-quality" "override" "agent_wrong" '{}' "interspect-correction"
ROW=$(sqlite3 "$DB" "SELECT low_confidence, quarantine_until, finding_key FROM evidence WHERE session_id='sess-x';")
assert_eq "unflagged evidence: low_confidence=0, normal (non-sentinel) quarantine, no finding_key" "$ROW" "0|0|"

RESULT=$(_interspect_is_routing_eligible fd-quality) || true
assert_eq "unflagged evidence still drives eligibility normally" "$RESULT" "eligible"

echo ""
echo "=== Idempotency ==="

_interspect_ensure_db
for col in low_confidence corroborated_at finding_key; do
    COL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('evidence') WHERE name = '$col';")
    assert_eq "$col column count = 1 after re-run" "$COL_COUNT" "1"
done

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
