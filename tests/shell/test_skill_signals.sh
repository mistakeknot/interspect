#!/usr/bin/env bash
# Tests for sylveste-7aj8.3: skill-signal collectors (scripts/signals/).
#
# Each collector reads pending skill evidence rows (source_kind='skill', no
# matching skill_signals row for its signal_kind) and writes one normalized
# [0,1] signal. Verifies, with synthetic fixtures in a scratch DB:
#   error       — catch-up no-op writes default-success for rows lacking it
#   bead_close  — closed→1.0, deferred→0.0, still-open→SKIP, idempotent
#   no_redirect — clean follow-through→1.0, redirect markers lower the score,
#                 missing transcript→SKIP
#   tokens      — graceful skip when cass is forced unavailable (empty PATH)

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIGNALS="$SCRIPT_DIR/scripts/signals"
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

# ─── Seed helper: one skill evidence row (mirrors ingest-skill-audit.py) ──────
PROJECT_DIR="$TEST_DIR/proj"
mkdir -p "$PROJECT_DIR/.beads"

seed_evidence() {
    # seed_evidence <invocation_id> <session_id> <skill> <ts> [project]
    local inv="$1" sid="$2" skill="$3" ts="$4" project="${5:-$PROJECT_DIR}"
    sqlite3 "$DB" "INSERT INTO evidence
        (ts, session_id, seq, source, event, context, project, source_event_id,
         source_table, quarantine_until, source_kind)
        VALUES ('$ts', '$sid', 1, '$skill', 'skill_invocation', '{}',
                '$project', '$inv', 'skill_signals', 0, 'skill');"
}

# ─── error: catch-up writes default-success for rows lacking the signal ───────
echo "=== collect_error (catch-up) ==="
seed_evidence "inv-err-1" "sess-err" "interpath:roadmap" "2026-06-01T10:00:00Z"
python3 "$SIGNALS/collect_error.py" --db "$DB" 2>/dev/null

ERR_VAL=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-err-1' AND signal_kind='error';")
assert_eq "error catch-up writes default success 1.0" "$ERR_VAL" "1.0"
ERR_META=$(sqlite3 "$DB" "SELECT metadata FROM skill_signals WHERE invocation_id='inv-err-1' AND signal_kind='error';")
assert_eq "error catch-up notes recovered default" "$ERR_META" '{"recovered":"default-success"}'

# Idempotency: re-run inserts nothing new.
OUT=$(python3 "$SIGNALS/collect_error.py" --db "$DB" 2>&1)
assert_eq "error re-run written=0" "$(echo "$OUT" | grep -o 'written=[0-9]*')" "written=0"

# Inline-written error (by ingest) is NOT overwritten / re-counted.
sqlite3 "$DB" "INSERT INTO skill_signals
    (skill_name, session_id, invocation_id, signal_kind, value, raw_value, observed_at)
    VALUES ('clavain:work', 'sess-inline', 'inv-inline', 'error', 0.0, 1.0, '2026-06-01T09:00:00Z');"
seed_evidence "inv-inline" "sess-inline" "clavain:work" "2026-06-01T09:00:00Z"
python3 "$SIGNALS/collect_error.py" --db "$DB" 2>/dev/null
INLINE_VAL=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-inline' AND signal_kind='error';")
assert_eq "inline error signal preserved (not clobbered)" "$INLINE_VAL" "0.0"

# ─── bead_close fixtures ──────────────────────────────────────────────────────
echo ""
echo "=== collect_bead_close ==="
cat > "$PROJECT_DIR/.beads/issues.jsonl" <<'JSONL'
{"id":"proj-closed","status":"closed","started_at":"2026-06-01T09:00:00Z","closed_at":"2026-06-03T00:00:00Z","updated_at":"2026-06-03T00:00:00Z"}
{"id":"proj-deferred","status":"deferred","started_at":"2026-06-05T09:00:00Z","updated_at":"2026-06-06T00:00:00Z"}
{"id":"proj-open","status":"in_progress","started_at":"2026-06-10T09:00:00Z","updated_at":"2026-06-10T09:00:00Z"}
JSONL

# Invocation while proj-closed was active → bead later closed within 7d → 1.0
seed_evidence "inv-bc-close" "sess-bc1" "clavain:sprint" "2026-06-01T10:00:00Z"
# Invocation while proj-deferred was active → deferred → 0.0
seed_evidence "inv-bc-defer" "sess-bc2" "clavain:work" "2026-06-05T10:00:00Z"
# Invocation while proj-open was active → still open → SKIP
seed_evidence "inv-bc-open" "sess-bc3" "clavain:route" "2026-06-10T10:00:00Z"

python3 "$SIGNALS/collect_bead_close.py" --db "$DB" 2>/dev/null

BC_CLOSE=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-bc-close' AND signal_kind='bead_close';")
assert_eq "closed bead → 1.0" "$BC_CLOSE" "1.0"
BC_DEFER=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-bc-defer' AND signal_kind='bead_close';")
assert_eq "deferred bead → 0.0" "$BC_DEFER" "0.0"
BC_OPEN=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE invocation_id='inv-bc-open' AND signal_kind='bead_close';")
assert_eq "still-open bead → SKIP (no row)" "$BC_OPEN" "0"
BC_META=$(sqlite3 "$DB" "SELECT metadata FROM skill_signals WHERE invocation_id='inv-bc-close' AND signal_kind='bead_close';")
assert_eq "bead_close metadata records attributed bead" \
    "$(echo "$BC_META" | python3 -c 'import sys,json;print(json.load(sys.stdin)["bead"])')" "proj-closed"

# Idempotency
OUT=$(python3 "$SIGNALS/collect_bead_close.py" --db "$DB" 2>&1)
assert_eq "bead_close re-run written=0" "$(echo "$OUT" | grep -o 'written=[0-9]*')" "written=0"

# Missing .beads → SKIP (pending). Evidence whose project has no beads file.
seed_evidence "inv-bc-nobeads" "sess-bc4" "clavain:work" "2026-06-01T10:00:00Z" "$TEST_DIR/nowhere"
python3 "$SIGNALS/collect_bead_close.py" --db "$DB" 2>/dev/null
BC_NB=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE invocation_id='inv-bc-nobeads' AND signal_kind='bead_close';")
assert_eq "no .beads file → SKIP (no row)" "$BC_NB" "0"

# ─── no_redirect fixtures ─────────────────────────────────────────────────────
echo ""
echo "=== collect_no_redirect ==="
PROJ_TRANSCRIPTS="$TEST_DIR/claude-projects/-test-proj"
mkdir -p "$PROJ_TRANSCRIPTS"

# Clean session: skill ran, user follows up cooperatively → value 1.0
cat > "$PROJ_TRANSCRIPTS/sess-nr-clean.jsonl" <<'JSONL'
{"type":"assistant","timestamp":"2026-06-01T10:00:00Z","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"clavain:sprint"}}]}}
{"type":"user","timestamp":"2026-06-01T10:01:00Z","message":{"content":"great, thanks — proceed"}}
{"type":"user","timestamp":"2026-06-01T10:02:00Z","message":{"content":"looks good, ship it"}}
JSONL

# Redirect session: 2 of 2 user turns carry redirect markers → value 0.0
cat > "$PROJ_TRANSCRIPTS/sess-nr-redir.jsonl" <<'JSONL'
{"type":"assistant","timestamp":"2026-06-01T10:00:00Z","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"clavain:work"}}]}}
{"type":"user","timestamp":"2026-06-01T10:01:00Z","message":{"content":"wait, that's wrong"}}
{"type":"user","timestamp":"2026-06-01T10:02:00Z","message":{"content":"actually, stop — undo that"}}
JSONL

seed_evidence "inv-nr-clean" "sess-nr-clean" "clavain:sprint" "2026-06-01T10:00:00Z"
seed_evidence "inv-nr-redir" "sess-nr-redir" "clavain:work" "2026-06-01T10:00:00Z"
# Missing transcript → SKIP
seed_evidence "inv-nr-missing" "sess-nr-missing" "clavain:route" "2026-06-01T10:00:00Z"

python3 "$SIGNALS/collect_no_redirect.py" --db "$DB" \
    --projects-dir "$TEST_DIR/claude-projects" 2>/dev/null

NR_CLEAN=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-nr-clean' AND signal_kind='no_redirect';")
assert_eq "clean follow-through → 1.0" "$NR_CLEAN" "1.0"
NR_REDIR=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-nr-redir' AND signal_kind='no_redirect';")
assert_eq "two redirecting turns → 0.0" "$NR_REDIR" "0.0"
NR_MISS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE invocation_id='inv-nr-missing' AND signal_kind='no_redirect';")
assert_eq "missing transcript → SKIP (no row)" "$NR_MISS" "0"

# Idempotency
OUT=$(python3 "$SIGNALS/collect_no_redirect.py" --db "$DB" \
    --projects-dir "$TEST_DIR/claude-projects" 2>&1)
assert_eq "no_redirect re-run written=0" "$(echo "$OUT" | grep -o 'written=[0-9]*')" "written=0"

# Partial redirect: 1 marker over 2 turns → value 0.5
cat > "$PROJ_TRANSCRIPTS/sess-nr-half.jsonl" <<'JSONL'
{"type":"assistant","timestamp":"2026-06-01T10:00:00Z","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"clavain:sprint"}}]}}
{"type":"user","timestamp":"2026-06-01T10:01:00Z","message":{"content":"hmm, actually let me reconsider"}}
{"type":"user","timestamp":"2026-06-01T10:02:00Z","message":{"content":"ok that works"}}
JSONL
seed_evidence "inv-nr-half" "sess-nr-half" "clavain:sprint" "2026-06-01T10:00:00Z"
python3 "$SIGNALS/collect_no_redirect.py" --db "$DB" \
    --projects-dir "$TEST_DIR/claude-projects" 2>/dev/null
NR_HALF=$(sqlite3 "$DB" "SELECT value FROM skill_signals WHERE invocation_id='inv-nr-half' AND signal_kind='no_redirect';")
assert_eq "1 redirect / 2 turns → 0.5" "$NR_HALF" "0.5"

# ─── tokens: graceful skip when cass is unavailable ───────────────────────────
echo ""
echo "=== collect_tokens (cass unavailable) ==="
seed_evidence "inv-tok-1" "sess-tok" "clavain:work" "2026-06-01T10:00:00Z"
# Force cass off PATH so the collector hits the graceful-degrade branch.
OUT=$(PATH="/nonexistent" /usr/bin/env python3 "$SIGNALS/collect_tokens.py" --db "$DB" 2>&1 || true)
TOK_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE invocation_id='inv-tok-1' AND signal_kind='tokens';")
assert_eq "cass unavailable → no tokens signal written" "$TOK_COUNT" "0"
assert_eq "cass unavailable logged" \
    "$(echo "$OUT" | grep -c 'cass unavailable')" "1"

# ─── dry-run writes nothing ───────────────────────────────────────────────────
echo ""
echo "=== dry-run safety ==="
BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals;")
seed_evidence "inv-dry-1" "sess-dry" "clavain:work" "2026-06-01T10:00:00Z"
python3 "$SIGNALS/collect_error.py" --db "$DB" --dry-run 2>/dev/null
AFTER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals;")
assert_eq "dry-run inserts no signal rows" "$AFTER" "$BEFORE"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
