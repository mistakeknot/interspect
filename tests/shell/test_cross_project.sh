#!/usr/bin/env bash
set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

# Create fake project structure with interspect DBs
for proj in proj-alpha proj-beta proj-gamma; do
    mkdir -p "$TEST_DIR/projects/${proj}/.clavain/interspect"
done

# Use proj-alpha as the "current" project
export CLAUDE_PROJECT_DIR="$TEST_DIR/projects/proj-alpha"
export HOME="$TEST_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"

# Initialize all three DBs
for proj in proj-alpha proj-beta proj-gamma; do
    local_db="$TEST_DIR/projects/${proj}/.clavain/interspect/interspect.db"
    sqlite3 "$local_db" "$(cat <<'SQL'
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, start_ts TEXT, end_ts TEXT, project TEXT, run_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, seq INTEGER, source TEXT, source_version TEXT, event TEXT, override_reason TEXT, context TEXT, project TEXT, project_lang TEXT, project_type TEXT, source_event_id TEXT, source_table TEXT, raw_override_reason TEXT);
SQL
    )" 2>/dev/null
done

# Populate proj-alpha: fd-safety has high override rate
sqlite3 "$TEST_DIR/projects/proj-alpha/.clavain/interspect/interspect.db" "
INSERT INTO sessions VALUES ('a1', datetime('now','-5 days'), datetime('now','-5 days','+1 hour'), 'proj-alpha', '', '');
INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, project) VALUES
  (datetime('now','-5 days'), 'a1', 1, 'fd-game-design', 'agent_dispatch', '', 'proj-alpha'),
  (datetime('now','-5 days'), 'a1', 2, 'fd-game-design', 'override', 'agent_wrong', 'proj-alpha'),
  (datetime('now','-5 days'), 'a1', 3, 'fd-safety', 'agent_dispatch', '', 'proj-alpha');
"

# Populate proj-beta: fd-game-design also has overrides
sqlite3 "$TEST_DIR/projects/proj-beta/.clavain/interspect/interspect.db" "
INSERT INTO sessions VALUES ('b1', datetime('now','-3 days'), datetime('now','-3 days','+1 hour'), 'proj-beta', '', '');
INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, project) VALUES
  (datetime('now','-3 days'), 'b1', 1, 'fd-game-design', 'agent_dispatch', '', 'proj-beta'),
  (datetime('now','-3 days'), 'b1', 2, 'fd-game-design', 'override', 'agent_wrong', 'proj-beta');
"

# Populate proj-gamma: fd-game-design again
sqlite3 "$TEST_DIR/projects/proj-gamma/.clavain/interspect/interspect.db" "
INSERT INTO sessions VALUES ('g1', datetime('now','-1 days'), datetime('now','-1 days','+1 hour'), 'proj-gamma', '', '');
INSERT INTO evidence (ts, session_id, seq, source, event, override_reason, project) VALUES
  (datetime('now','-1 days'), 'g1', 1, 'fd-game-design', 'agent_dispatch', '', 'proj-gamma'),
  (datetime('now','-1 days'), 'g1', 2, 'fd-game-design', 'override', 'agent_wrong', 'proj-gamma');
"

# Initialize current project DB
_interspect_ensure_db

PASS=0
FAIL=0

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
    if [[ -n "$val" && "$val" != "null" ]]; then
        echo "  PASS: $desc"
        ((PASS++)) || true
    else
        echo "  FAIL: $desc (empty/null)"
        ((FAIL++)) || true
    fi
}

echo "=== Cross-Project Aggregation Tests ==="

echo ""
echo "Group 1: DB Discovery"
dbs=$(_interspect_discover_project_dbs | wc -l | tr -d ' ')
assert_ge "discovers other project DBs" "$dbs" 2

echo ""
echo "Group 2: Cross-project report"
report=$(_interspect_cross_project_report 30)
valid=$(echo "$report" | jq '.agents | length' 2>/dev/null || echo "INVALID")
assert_true "report returns valid JSON" "$valid"

project_count=$(echo "$report" | jq '.project_count')
assert_ge "multiple projects found" "$project_count" 2

# fd-game-design should appear in 3 projects
gd_projects=$(echo "$report" | jq '[.agents[] | select(.agent == "fd-game-design")] | .[0].project_count')
assert_eq "fd-game-design in 3 projects" "$gd_projects" "3"

gd_corrections=$(echo "$report" | jq '[.agents[] | select(.agent == "fd-game-design")] | .[0].total_corrections')
assert_ge "fd-game-design has 3 corrections" "$gd_corrections" 3

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
