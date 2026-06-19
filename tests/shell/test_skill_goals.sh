#!/usr/bin/env bash
# Tests for sylveste-7aj8.4: per-skill goal classifier.
#
# Verifies scripts/infer-skill-goals.py classifies registered skills into
# {speed, precision, completeness} goal weights in the skill_goals table,
# using a synthetic --skills-root fixture and --mock (no API spend):
#   1. Weights persist and sum to 1.0
#   2. classified_from is 'skill_md' on first classification
#   3. Hash short-circuit: a second --mock run re-classifies 0 unchanged skills
#   4. --force re-classifies despite an unchanged hash
#   5. Refine pass flips classified_from to 'observed' when >= 20 signals exist
#   6. Malformed model output is handled (skip, no crash, errors counted)
#   7. --skill targets a single skill on-demand

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFER="$SCRIPT_DIR/scripts/infer-skill-goals.py"
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

# ─── Fixture skills-root (cache-style layout: <mkt>/<plugin>/<ver>/skills/<skill>) ──
# Use the deep cache layout so the namespaced <plugin>:<skill> naming is exercised.
ROOT="$TEST_DIR/skills_cache"
mk_skill() {
    # mk_skill <plugin> <skill> <body>
    local plugin="$1" skill="$2" body="$3"
    local dir="$ROOT/testmkt/$plugin/0.1.0/skills/$skill"
    mkdir -p "$dir"
    cat > "$dir/SKILL.md" <<EOF
---
name: $skill
description: $body
user_invocable: true
---

# $skill

$body
EOF
}

# Three archetypes the --mock keyword bucketer can classify deterministically.
mk_skill intersearch session-search "Search past sessions, retrieve and find by query and index."
mk_skill clavain work "Execute work plans and implement features with reasoning and care."
mk_skill interwatch audit "Run a correctness audit and review for coverage; verify and check everything."

echo "=== mock classification persists + sums to 1.0 ==="
python3 "$INFER" --skills-root "$ROOT" --db "$DB" --mock --no-refine 2>/dev/null

GOALS_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_goals;")
assert_eq "3 skill_goals rows written" "$GOALS_COUNT" "3"

# Namespacing: <plugin>:<skill>.
SS_FROM=$(sqlite3 "$DB" "SELECT classified_from FROM skill_goals WHERE skill_name='intersearch:session-search';")
assert_eq "classified_from is skill_md" "$SS_FROM" "skill_md"

# Sum to 1.0 (rounded) for every row.
BAD_SUMS=$(sqlite3 "$DB" "
SELECT COUNT(*) FROM skill_goals
WHERE ROUND(
  json_extract(goal_weights,'\$.speed') +
  json_extract(goal_weights,'\$.precision') +
  json_extract(goal_weights,'\$.completeness'), 4) != 1.0;")
assert_eq "all weight vectors sum to 1.0" "$BAD_SUMS" "0"

# Archetype routing: search -> speed-leaning; audit -> completeness-leaning.
SEARCH_SPEED=$(sqlite3 "$DB" "SELECT json_extract(goal_weights,'\$.speed') FROM skill_goals WHERE skill_name='intersearch:session-search';")
assert_eq "search skill is speed-leaning (0.7)" "$SEARCH_SPEED" "0.7"
AUDIT_COMP=$(sqlite3 "$DB" "SELECT json_extract(goal_weights,'\$.completeness') FROM skill_goals WHERE skill_name='interwatch:audit';")
assert_eq "audit skill is completeness-leaning (0.6)" "$AUDIT_COMP" "0.6"
WORK_PREC=$(sqlite3 "$DB" "SELECT json_extract(goal_weights,'\$.precision') FROM skill_goals WHERE skill_name='clavain:work';")
assert_eq "work skill is precision-leaning (0.6)" "$WORK_PREC" "0.6"

echo ""
echo "=== hash short-circuit (second run re-classifies 0) ==="
OUT=$(python3 "$INFER" --skills-root "$ROOT" --db "$DB" --mock --no-refine 2>&1)
assert_eq "second run short-circuits all 3" \
    "$(echo "$OUT" | grep -o 'short_circuited=[0-9]*' | tail -1)" "short_circuited=3"
assert_eq "second run classifies 0" \
    "$(echo "$OUT" | grep -o 'classified=[0-9]*' | tail -1)" "classified=0"

echo ""
echo "=== --force re-classifies despite unchanged hash ==="
OUT=$(python3 "$INFER" --skills-root "$ROOT" --db "$DB" --mock --force --no-refine 2>&1)
assert_eq "--force re-classifies all 3" \
    "$(echo "$OUT" | grep -o 'classified=[0-9]*' | tail -1)" "classified=3"

echo ""
echo "=== refine pass flips classified_from to 'observed' with >= 20 signals ==="
# Seed 25 'bead_close' signals for clavain:work (maps to completeness).
python3 - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
for i in range(25):
    conn.execute(
        "INSERT INTO skill_signals "
        "(skill_name, session_id, invocation_id, signal_kind, value, raw_value, observed_at) "
        "VALUES ('clavain:work', ?, ?, 'bead_close', 1.0, 1.0, '2026-06-19T00:00:00Z')",
        (f"sess-{i}", f"inv-{i}"),
    )
conn.commit()
conn.close()
PY

OUT=$(python3 "$INFER" --skills-root "$ROOT" --db "$DB" --mock --refine 2>&1)
WORK_FROM=$(sqlite3 "$DB" "SELECT classified_from FROM skill_goals WHERE skill_name='clavain:work';")
assert_eq "clavain:work refined to 'observed'" "$WORK_FROM" "observed"
assert_eq "refine reports 1 refined" \
    "$(echo "$OUT" | grep -o 'refined=[0-9]*' | tail -1)" "refined=1"

# A skill with < 20 signals stays 'skill_md'.
SS_FROM=$(sqlite3 "$DB" "SELECT classified_from FROM skill_goals WHERE skill_name='intersearch:session-search';")
assert_eq "under-threshold skill stays skill_md" "$SS_FROM" "skill_md"

# Refined row still sums to 1.0 and leans completeness (observed bead_close mass).
WORK_SUM=$(sqlite3 "$DB" "
SELECT ROUND(
  json_extract(goal_weights,'\$.speed') +
  json_extract(goal_weights,'\$.precision') +
  json_extract(goal_weights,'\$.completeness'), 4)
FROM skill_goals WHERE skill_name='clavain:work';")
assert_eq "refined weights still sum to 1.0" "$WORK_SUM" "1.0"

echo ""
echo "=== malformed model output handled (skip, no crash) ==="
# Fresh DB + a single fixture skill, with a stub 'claude' on PATH that emits junk.
BAD_DIR="$TEST_DIR/bad_proj"
mkdir -p "$BAD_DIR/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$BAD_DIR"
unset _INTERSPECT_DB
_interspect_ensure_db >/dev/null
BDB=$(_interspect_db_path)

BADROOT="$TEST_DIR/bad_skills/testmkt/p/0.1.0/skills/s"
mkdir -p "$BADROOT"
cat > "$BADROOT/SKILL.md" <<'EOF'
---
name: s
description: a skill whose classifier will misbehave
---
# s
body
EOF

STUB_BIN="$TEST_DIR/stubbin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/claude" <<'EOF'
#!/usr/bin/env bash
echo "I am a chatty model with no JSON here, sorry."
EOF
chmod +x "$STUB_BIN/claude"

# Real (non-mock) path so claude_classify runs against the stub; expect a clean
# skip + nonzero error count + exit 0 (batch survives).
set +e
OUT=$(PATH="$STUB_BIN:$PATH" python3 "$INFER" --skills-root "$TEST_DIR/bad_skills" --db "$BDB" --no-refine 2>&1)
RC=$?
set -e
assert_eq "malformed output exits 0 (batch survives)" "$RC" "0"
assert_eq "malformed output counted as error" \
    "$(echo "$OUT" | grep -o 'errors=[0-9]*' | tail -1)" "errors=1"
BAD_ROWS=$(sqlite3 "$BDB" "SELECT COUNT(*) FROM skill_goals;")
assert_eq "no skill_goals row written on parse failure" "$BAD_ROWS" "0"

echo ""
echo "=== --skill targets a single skill on-demand ==="
export CLAUDE_PROJECT_DIR="$TEST_DIR"
unset _INTERSPECT_DB
_interspect_ensure_db >/dev/null
ONE_DB="$TEST_DIR/.clavain/interspect/interspect.db"
OUT=$(python3 "$INFER" --skills-root "$ROOT" --db "$ONE_DB" --mock --force --no-refine --skill clavain:work 2>&1)
assert_eq "--skill classifies exactly 1" \
    "$(echo "$OUT" | grep -o 'classified=[0-9]*' | tail -1)" "classified=1"
assert_eq "--skill found exactly 1" \
    "$(echo "$OUT" | grep -o 'found=[0-9]*' | tail -1)" "found=1"

echo ""
echo "=== COMMAND discovery + classification (sylveste-7aj8.8) ==="
# Commands are SINGLE .md files in a commands/ dir (NOT a dir with SKILL.md).
# Cache layout: <mkt>/<plugin>/<ver>/commands/<cmd>.md → <plugin>:<cmd>.
# Flat layout:  <root>/commands/<cmd>.md (user/project) → bare <cmd>.
CMD_DIR="$TEST_DIR/cmd_proj"
mkdir -p "$CMD_DIR/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$CMD_DIR"
unset _INTERSPECT_DB
_interspect_ensure_db >/dev/null
CDB=$(_interspect_db_path)

CMD_ROOT="$TEST_DIR/cmds_cache"
mk_command() {
    # mk_command <plugin> <cmd> <desc>
    local plugin="$1" cmd="$2" desc="$3"
    local dir="$CMD_ROOT/testmkt/$plugin/0.1.0/commands"
    mkdir -p "$dir"
    cat > "$dir/$cmd.md" <<EOF
---
name: $cmd
description: $desc
argument-hint: "[args]"
---

# $cmd

$desc
EOF
}

# A sibling non-command file in the commands dir must be ignored (only *.md taken,
# and only files whose parent dir is 'commands').
mk_command clavain sprint "Phase sequencer that orchestrates the lifecycle pipeline: brainstorm, plan, execute, ship."
mk_command interflux flux-drive "Dispatch a parallel multi-agent workflow that sequences triage and synthesis phases."
echo "irrelevant: not a command" > "$CMD_ROOT/testmkt/clavain/0.1.0/commands/degraded-modes.yaml"

# Flat user-style command root.
FLAT_CMD_ROOT="$TEST_DIR/flat_cmds/commands"
mkdir -p "$FLAT_CMD_ROOT"
cat > "$FLAT_CMD_ROOT/mycmd.md" <<'EOF'
---
name: mycmd
description: A bare user command that searches and finds things by query.
---
# mycmd
search and find by query
EOF

python3 "$INFER" --commands-root "$CMD_ROOT" --commands-root "$TEST_DIR/flat_cmds" \
    --skills-root "$TEST_DIR/empty_skills" --db "$CDB" --mock --no-refine 2>/dev/null

# 3 commands discovered + persisted (2 namespaced + 1 bare); no skills (empty root).
CMD_COUNT=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM skill_goals;")
assert_eq "3 command goal rows written" "$CMD_COUNT" "3"

# Namespacing: <plugin>:<cmd-stem> from the cache layout.
SPRINT_EXISTS=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name='clavain:sprint';")
assert_eq "clavain:sprint discovered + named correctly" "$SPRINT_EXISTS" "1"
FD_EXISTS=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name='interflux:flux-drive';")
assert_eq "interflux:flux-drive discovered + named correctly" "$FD_EXISTS" "1"

# Flat command → bare name.
BARE_EXISTS=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name='mycmd';")
assert_eq "flat user command named bare 'mycmd'" "$BARE_EXISTS" "1"

# entity_kind discriminator: commands recorded with classified_from='command_md'.
SPRINT_FROM=$(sqlite3 "$CDB" "SELECT classified_from FROM skill_goals WHERE skill_name='clavain:sprint';")
assert_eq "command classified_from is command_md" "$SPRINT_FROM" "command_md"

# Command weights persisted + sum to 1.0.
CMD_BAD_SUMS=$(sqlite3 "$CDB" "
SELECT COUNT(*) FROM skill_goals
WHERE ROUND(
  json_extract(goal_weights,'\$.speed') +
  json_extract(goal_weights,'\$.precision') +
  json_extract(goal_weights,'\$.completeness'), 4) != 1.0;")
assert_eq "all command weight vectors sum to 1.0" "$CMD_BAD_SUMS" "0"

# The sibling .yaml must NOT have produced a row.
YAML_ROW=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name LIKE '%degraded%';")
assert_eq "non-.md sibling ignored (no degraded-modes row)" "$YAML_ROW" "0"

echo ""
echo "=== --skill works for a COMMAND name ==="
OUT=$(python3 "$INFER" --commands-root "$CMD_ROOT" --skills-root "$TEST_DIR/empty_skills" \
    --db "$CDB" --mock --force --no-refine --skill clavain:sprint 2>&1)
assert_eq "--skill clavain:sprint classifies exactly 1" \
    "$(echo "$OUT" | grep -o 'classified=[0-9]*' | tail -1)" "classified=1"
assert_eq "--skill clavain:sprint found exactly 1" \
    "$(echo "$OUT" | grep -o 'found=[0-9]*' | tail -1)" "found=1"

echo ""
echo "=== skill/command name collision prefers the skill (tie-break) ==="
COLL_DIR="$TEST_DIR/coll_proj"
mkdir -p "$COLL_DIR/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$COLL_DIR"
unset _INTERSPECT_DB
_interspect_ensure_db >/dev/null
COLLDB=$(_interspect_db_path)
# Same canonical name 'dup:thing' as BOTH a skill and a command.
DUP_SKILL="$TEST_DIR/coll/skills/testmkt/dup/0.1.0/skills/thing"
mkdir -p "$DUP_SKILL"
cat > "$DUP_SKILL/SKILL.md" <<'EOF'
---
name: thing
description: the skill surface of thing
---
# thing
skill body
EOF
DUP_CMD="$TEST_DIR/coll/cmds/testmkt/dup/0.1.0/commands"
mkdir -p "$DUP_CMD"
cat > "$DUP_CMD/thing.md" <<'EOF'
---
name: thing
description: the command surface of thing
---
# thing
command body
EOF
python3 "$INFER" --skills-root "$TEST_DIR/coll/skills" --commands-root "$TEST_DIR/coll/cmds" \
    --db "$COLLDB" --mock --no-refine 2>/dev/null
COLL_ROWS=$(sqlite3 "$COLLDB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name='dup:thing';")
assert_eq "collision yields exactly 1 row" "$COLL_ROWS" "1"
COLL_FROM=$(sqlite3 "$COLLDB" "SELECT classified_from FROM skill_goals WHERE skill_name='dup:thing';")
assert_eq "collision resolves to the SKILL (skill_md wins)" "$COLL_FROM" "skill_md"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
