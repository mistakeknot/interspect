#!/usr/bin/env bash
# Tests for sylveste-7aj8.2: skill-evidence ingestion adapter.
#
# Verifies scripts/ingest-skill-audit.py drains Skill rows from either source
# format into the evidence + skill_signals tables:
#   1. tool-time-format fixture → evidence (source_kind='skill') + error signals
#   2. Idempotency: second run inserts 0 new rows
#   3. Failed skill (error non-null) → error signal value 0.0
#   4. audit-log-format fixture → ingests correctly (proves the adapter)
#   5. Watermark: rows at/below the stored watermark are skipped

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INGEST="$SCRIPT_DIR/scripts/ingest-skill-audit.py"
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

# ─── Fixtures ────────────────────────────────────────────────────────────────

TT_FIXTURE="$TEST_DIR/tooltime.jsonl"
cat > "$TT_FIXTURE" <<'JSONL'
{"v":1,"id":"sess-aaa-1","ts":"2026-06-01T10:00:00Z","event":"PostToolUse","tool":"Skill","project":"/home/mk/projects/Demarch","error":null,"source":"claude-code","skill":"interwatch:watch"}
{"v":1,"id":"sess-aaa-2","ts":"2026-06-01T10:01:00Z","event":"PostToolUse","tool":"Skill","project":"/home/mk/projects/Demarch","error":null,"source":"claude-code","skill":"interpath:roadmap"}
{"v":1,"id":"sess-bbb-5","ts":"2026-06-01T11:00:00Z","event":"PostToolUse","tool":"Skill","project":"/home/mk/projects/Sylveste","error":"boom","source":"claude-code","skill":"clavain:work"}
{"v":1,"id":"sess-bbb-6","ts":"2026-06-01T11:01:00Z","event":"PostToolUse","tool":"Bash","project":"/home/mk/projects/Sylveste","error":null,"source":"claude-code"}
{"v":1,"id":"sess-bbb-7","ts":"2026-06-01T11:02:00Z","event":"SessionStart","tool":"","project":"/home/mk/projects/Sylveste","error":null,"source":"claude-code"}
JSONL
# 3 Skill rows (2 success, 1 failure); 1 Bash + 1 SessionStart are non-Skill noise.

echo "=== tool-time ingest ==="
python3 "$INGEST" --source "$TT_FIXTURE" --format tooltime --db "$DB" 2>/dev/null

EV_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source_kind='skill';")
assert_eq "3 skill evidence rows inserted" "$EV_COUNT" "3"

EV_EVENT=$(sqlite3 "$DB" "SELECT DISTINCT event FROM evidence WHERE source_kind='skill';")
assert_eq "evidence event is skill_invocation" "$EV_EVENT" "skill_invocation"

EV_WATCH=$(sqlite3 "$DB" "SELECT source FROM evidence WHERE source_kind='skill' AND source='interwatch:watch';")
assert_eq "skill name stored in evidence.source" "$EV_WATCH" "interwatch:watch"

EV_SID=$(sqlite3 "$DB" "SELECT session_id FROM evidence WHERE source='interwatch:watch';")
assert_eq "session_id strips trailing -counter" "$EV_SID" "sess-aaa"

EV_PROJ=$(sqlite3 "$DB" "SELECT project FROM evidence WHERE source='interwatch:watch';")
assert_eq "project carried from tool-time row" "$EV_PROJ" "/home/mk/projects/Demarch"

SIG_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE signal_kind='error';")
assert_eq "3 error signals inserted" "$SIG_COUNT" "3"

echo ""
echo "=== failed skill → value 0.0 ==="
SIG_FAIL=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='sess-bbb-5';")
assert_eq "failed skill error signal value = 0.0" "$SIG_FAIL" "0.0"
RAW_FAIL=$(sqlite3 "$DB" "SELECT raw_value FROM skill_signals WHERE invocation_id='sess-bbb-5';")
assert_eq "failed skill raw_value = 1" "$RAW_FAIL" "1.0"

SIG_OK=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='sess-aaa-1';")
assert_eq "success skill error signal value = 1.0" "$SIG_OK" "1.0"
RAW_OK=$(sqlite3 "$DB" "SELECT raw_value FROM skill_signals WHERE invocation_id='sess-aaa-1';")
assert_eq "success skill raw_value = 0" "$RAW_OK" "0.0"

echo ""
echo "=== idempotency (second run inserts 0) ==="
# Force --since older than all rows so the watermark does not pre-filter; this
# proves the source_event_id + UNIQUE dedup gates (not just the watermark).
OUT=$(python3 "$INGEST" --source "$TT_FIXTURE" --format tooltime --db "$DB" --since "2026-01-01T00:00:00Z" 2>&1)
EV_COUNT2=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source_kind='skill';")
assert_eq "evidence count unchanged after re-run" "$EV_COUNT2" "3"
SIG_COUNT2=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE signal_kind='error';")
assert_eq "signal count unchanged after re-run" "$SIG_COUNT2" "3"
assert_eq "re-run reports evidence_inserted=0" \
    "$(echo "$OUT" | grep -o 'evidence_inserted=[0-9]*' | tail -1)" "evidence_inserted=0"
assert_eq "re-run reports signals_inserted=0" \
    "$(echo "$OUT" | grep -o 'signals_inserted=[0-9]*' | tail -1)" "signals_inserted=0"

echo ""
echo "=== audit-log-format adapter ==="
# Fresh DB so the audit fixture counts are isolated from the tool-time run.
AUDIT_DIR="$TEST_DIR/audit_proj"
mkdir -p "$AUDIT_DIR/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$AUDIT_DIR"
unset _INTERSPECT_DB _INTERSPECT_MANIFEST_LOADED _INTERSPECT_CONFIDENCE_LOADED
_interspect_ensure_db
ADB=$(_interspect_db_path)

AUDIT_FIXTURE="$TEST_DIR/audit.log"
cat > "$AUDIT_FIXTURE" <<'JSONL'
{"ts":"2026-06-02T09:00:00Z","session_id":"abc123","tool":"Skill","name":"clavain:campaign","duration_ms":42,"exit_code":0}
{"ts":"2026-06-02T09:01:00Z","session_id":"abc123","tool":"Skill","name":"interpath:prd","duration_ms":99,"exit_code":7}
{"ts":"2026-06-02T09:02:00Z","session_id":"abc123","tool":"Edit","name":"/tmp/x.py","duration_ms":1,"exit_code":0}
JSONL
python3 "$INGEST" --source "$AUDIT_FIXTURE" --format audit --db "$ADB" 2>/dev/null

A_EV=$(sqlite3 "$ADB" "SELECT COUNT(*) FROM evidence WHERE source_kind='skill';")
assert_eq "audit-log: 2 skill evidence rows (Edit excluded)" "$A_EV" "2"
A_SIG_FAIL=$(sqlite3 "$ADB" "SELECT value FROM skill_signals ss JOIN evidence e ON e.source='interpath:prd' WHERE ss.skill_name='interpath:prd';")
assert_eq "audit-log: exit_code!=0 → signal value 0.0" "$A_SIG_FAIL" "0.0"
A_SIG_OK=$(sqlite3 "$ADB" "SELECT value FROM skill_signals WHERE skill_name='clavain:campaign';")
assert_eq "audit-log: exit_code=0 → signal value 1.0" "$A_SIG_OK" "1.0"

echo ""
echo "=== watermark skips old rows ==="
# Restore the tool-time DB context. Its watermark is now at the max ts of the
# tool-time fixture (2026-06-01T11:00:00Z). Append a row OLDER than the
# watermark and one NEWER; only the newer one should ingest.
export CLAUDE_PROJECT_DIR="$TEST_DIR"
unset _INTERSPECT_DB
_interspect_ensure_db >/dev/null
WM=$(sqlite3 "$DB" "SELECT value FROM sentinels WHERE key='skill_ingest_watermark';")
assert_eq "watermark advanced to max tool-time ts" "$WM" "2026-06-01T11:00:00Z"

WM_FIXTURE="$TEST_DIR/watermark.jsonl"
cat > "$WM_FIXTURE" <<'JSONL'
{"v":1,"id":"sess-ccc-1","ts":"2026-05-01T00:00:00Z","event":"PostToolUse","tool":"Skill","project":"/p","error":null,"source":"claude-code","skill":"old:skill"}
{"v":1,"id":"sess-ccc-2","ts":"2026-07-01T00:00:00Z","event":"PostToolUse","tool":"Skill","project":"/p","error":null,"source":"claude-code","skill":"new:skill"}
JSONL
# Use default (no --since) so the stored watermark applies.
python3 "$INGEST" --source "$WM_FIXTURE" --format tooltime --db "$DB" 2>/dev/null

OLD_PRESENT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source='old:skill';")
assert_eq "row older than watermark skipped" "$OLD_PRESENT" "0"
NEW_PRESENT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source='new:skill';")
assert_eq "row newer than watermark ingested" "$NEW_PRESENT" "1"
WM2=$(sqlite3 "$DB" "SELECT value FROM sentinels WHERE key='skill_ingest_watermark';")
assert_eq "watermark advanced to newest ts" "$WM2" "2026-07-01T00:00:00Z"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
