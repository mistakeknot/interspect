#!/usr/bin/env bash
# Tests for sylveste-7aj8.5: weighted per-skill scoring (scripts/score-skills.py).
#
# Seeds a scratch DB with synthetic skill evidence + signals + goals and verifies:
#   1. Only skills with >= --min-invocations distinct invocations in the window score
#   2. Composite math is correct for a hand-computed case
#   3. Missing-signal renormalization works (skill missing a goal still scores)
#   4. Uniform fallback when no skill_goals row (goal_source='uniform')
#   5. routing-calibration.json gains a `skills` block + bumped schema_version
#   6. Idempotent re-run (skills block re-computes to the same values)
#   7. --dry-run does not write the json
#   8. Out-of-window invocations excluded from the qualifying count

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export CLAUDE_PROJECT_DIR="$TEST_DIR"
mkdir -p "$TEST_DIR/.clavain/interspect"
mkdir -p "$TEST_DIR/.claude"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCORE="$SCRIPT_DIR/scripts/score-skills.py"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)
CALIB="$TEST_DIR/.clavain/interspect/routing-calibration.json"

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

# Recent ts inside the 30d window (collectors store observed_at == invocation ts).
# Use a fixed-but-recent date relative to "now"; tests run with default 30d window,
# so anchor signals within ~3 days of now.
TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TS_RECENT=$(date -u -d '2 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-2d +%Y-%m-%dT%H:%M:%SZ)
TS_OLD=$(date -u -d '90 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-90d +%Y-%m-%dT%H:%M:%SZ)

# ─── Seed helpers ─────────────────────────────────────────────────────────────
seed_evidence() {
    # seed_evidence <invocation_id> <skill> <ts>
    local inv="$1" skill="$2" ts="$3"
    sqlite3 "$DB" "INSERT INTO evidence
        (ts, session_id, seq, source, event, context, project, source_event_id,
         source_table, quarantine_until, source_kind)
        VALUES ('$ts', 'sess-$inv', 1, '$skill', 'skill_invocation', '{}',
                '$TEST_DIR', '$inv', 'skill_signals', 0, 'skill');"
}

seed_signal() {
    # seed_signal <skill> <invocation_id> <signal_kind> <value> <ts>
    local skill="$1" inv="$2" kind="$3" val="$4" ts="$5"
    sqlite3 "$DB" "INSERT OR IGNORE INTO skill_signals
        (skill_name, session_id, invocation_id, signal_kind, value, raw_value, observed_at)
        VALUES ('$skill', 'sess-$inv', '$inv', '$kind', $val, $val, '$ts');"
}

seed_goals() {
    # seed_goals <skill> <speed> <precision> <completeness>
    local skill="$1" sp="$2" pr="$3" co="$4"
    sqlite3 "$DB" "INSERT OR REPLACE INTO skill_goals
        (skill_name, goal_weights, classified_from, classifier_version, classified_at)
        VALUES ('$skill',
                json_object('speed', $sp, 'precision', $pr, 'completeness', $co),
                'skill_md', 'test-v1', '$TS_NOW');"
}

# ─── Skill A: hand-computed full case ─────────────────────────────────────────
# 12 invocations (clears the default >=10). All four signals present.
#   tokens=0.8 -> speed
#   error=1.0, no_redirect=0.6 -> precision = (1.0*1.0 + 0.5*0.6)/(1.0+0.5)
#                                           = (1.0 + 0.3)/1.5 = 0.866666...
#   bead_close=0.5 -> completeness
# goal_weights = {speed:0.2, precision:0.6, completeness:0.2}
# All goals present -> renorm divisor = 1.0
# score = 0.2*0.8 + 0.6*0.8666667 + 0.2*0.5 = 0.16 + 0.52 + 0.10 = 0.78
# Use half-life huge so recency weighting collapses to a plain mean (all ts equal anyway).
SKILL_A="alpha:full"
for i in $(seq 1 12); do
    seed_evidence "a-$i" "$SKILL_A" "$TS_RECENT"
    seed_signal "$SKILL_A" "a-$i" tokens 0.8 "$TS_RECENT"
    seed_signal "$SKILL_A" "a-$i" error 1.0 "$TS_RECENT"
    seed_signal "$SKILL_A" "a-$i" no_redirect 0.6 "$TS_RECENT"
    seed_signal "$SKILL_A" "a-$i" bead_close 0.5 "$TS_RECENT"
done
seed_goals "$SKILL_A" 0.2 0.6 0.2

# ─── Skill B: below threshold (9 invocations) — must NOT score ────────────────
SKILL_B="beta:thin"
for i in $(seq 1 9); do
    seed_evidence "b-$i" "$SKILL_B" "$TS_RECENT"
    seed_signal "$SKILL_B" "b-$i" error 1.0 "$TS_RECENT"
done
seed_goals "$SKILL_B" 0.3 0.4 0.3

# ─── Skill C: missing-signal renormalization (only precision present) ─────────
# 11 invocations. Only an 'error' signal (=0.4) -> precision goal only.
# goal_weights = {speed:0.5, precision:0.3, completeness:0.2}.
# Only precision present -> renorm over {precision}: score = error value = 0.4.
SKILL_C="gamma:partial"
for i in $(seq 1 11); do
    seed_evidence "c-$i" "$SKILL_C" "$TS_RECENT"
    seed_signal "$SKILL_C" "c-$i" error 0.4 "$TS_RECENT"
done
seed_goals "$SKILL_C" 0.5 0.3 0.2

# ─── Skill D: uniform fallback (no skill_goals row) ───────────────────────────
# 10 invocations. tokens=0.9 (speed), bead_close=0.3 (completeness). No goals row.
# Uniform weights {1/3,1/3,1/3}; present goals {speed, completeness}.
# renorm divisor = 1/3 + 1/3 = 2/3.
# score = (1/3*0.9 + 1/3*0.3) / (2/3) = (0.3 + 0.1)/0.6667 = 0.4/0.6667 = 0.6
SKILL_D="delta:nogoals"
for i in $(seq 1 10); do
    seed_evidence "d-$i" "$SKILL_D" "$TS_RECENT"
    seed_signal "$SKILL_D" "d-$i" tokens 0.9 "$TS_RECENT"
    seed_signal "$SKILL_D" "d-$i" bead_close 0.3 "$TS_RECENT"
done
# (no seed_goals for D)

# ─── Skill E: out-of-window invocations don't count toward threshold ──────────
# 20 OLD invocations (90 days ago) + 2 recent. Only 2 in window < 10 -> excluded.
SKILL_E="epsilon:stale"
for i in $(seq 1 20); do
    seed_evidence "e-old-$i" "$SKILL_E" "$TS_OLD"
done
for i in $(seq 1 2); do
    seed_evidence "e-new-$i" "$SKILL_E" "$TS_RECENT"
done
seed_goals "$SKILL_E" 0.3 0.4 0.3

# ─── Run scoring (half-life huge so the math is plain-mean / deterministic) ───
echo "=== run scoring (window 30d, min 10, plain mean) ==="
python3 "$SCORE" --db "$DB" --half-life-days 100000 --json > "$TEST_DIR/out.json" 2>"$TEST_DIR/err.txt"
cat "$TEST_DIR/err.txt" | tail -3

# 1. Qualifying set is exactly {A, C, D} (B thin, E mostly stale)
QUAL=$(python3 -c "import json;d=json.load(open('$TEST_DIR/out.json'));print(' '.join(sorted(d['skills'])))")
assert_eq "qualifying skills are A,C,D only" "$QUAL" "alpha:full delta:nogoals gamma:partial"

assert_eq "skill B (9 inv) excluded" \
    "$(python3 -c "import json;print('beta:thin' in json.load(open('$TEST_DIR/out.json'))['skills'])")" "False"
assert_eq "skill E (2 in-window) excluded" \
    "$(python3 -c "import json;print('epsilon:stale' in json.load(open('$TEST_DIR/out.json'))['skills'])")" "False"

# 2. Composite math for A == 0.78
A_SCORE=$(python3 -c "import json;print(round(json.load(open('$TEST_DIR/out.json'))['skills']['alpha:full']['score'],4))")
assert_eq "skill A composite == 0.78" "$A_SCORE" "0.78"

# precision aggregate for A == 0.8667 (error 1.0 @1.0x + no_redirect 0.6 @0.5x)
A_PREC=$(python3 -c "import json;print(round(json.load(open('$TEST_DIR/out.json'))['skills']['alpha:full']['goals']['precision'],4))")
assert_eq "skill A precision goal == 0.8667" "$A_PREC" "0.8667"

# 3. Missing-signal renorm: C scores 0.4 (precision-only) and has only precision goal
C_SCORE=$(python3 -c "import json;print(round(json.load(open('$TEST_DIR/out.json'))['skills']['gamma:partial']['score'],4))")
assert_eq "skill C (precision-only) score == 0.4" "$C_SCORE" "0.4"
C_GOALS=$(python3 -c "import json;print(','.join(sorted(json.load(open('$TEST_DIR/out.json'))['skills']['gamma:partial']['goals'])))")
assert_eq "skill C has only precision goal" "$C_GOALS" "precision"

# 4. Uniform fallback for D
D_SRC=$(python3 -c "import json;print(json.load(open('$TEST_DIR/out.json'))['skills']['delta:nogoals']['goal_source'])")
assert_eq "skill D goal_source is uniform" "$D_SRC" "uniform"
D_SCORE=$(python3 -c "import json;print(round(json.load(open('$TEST_DIR/out.json'))['skills']['delta:nogoals']['score'],4))")
assert_eq "skill D uniform-renorm score == 0.6" "$D_SCORE" "0.6"

# Sorted high->low: A(0.78) > D(0.6) > C(0.4)
ORDER=$(python3 -c "
import json
d=json.load(open('$TEST_DIR/out.json'))['skills']
print(' '.join(k for k,_ in sorted(d.items(), key=lambda kv:(-kv[1]['score'], kv[0]))))
")
assert_eq "leaderboard order A>D>C" "$ORDER" "alpha:full delta:nogoals gamma:partial"

echo ""
echo "=== routing-calibration.json gains skills block + schema bump ==="
python3 "$SCORE" --db "$DB" --half-life-days 100000 >/dev/null 2>&1
assert_eq "routing-calibration.json exists" "$([[ -f "$CALIB" ]] && echo yes || echo no)" "yes"
HAS_SKILLS=$(python3 -c "import json;print('skills' in json.load(open('$CALIB')))")
assert_eq "calibration has skills block" "$HAS_SKILLS" "True"
SCHEMA=$(python3 -c "import json;print(json.load(open('$CALIB'))['schema_version'])")
assert_eq "schema_version bumped to 3" "$SCHEMA" "3"
NSKILLS=$(python3 -c "import json;print(len(json.load(open('$CALIB'))['skills']))")
assert_eq "3 skills in calibration block" "$NSKILLS" "3"

echo ""
echo "=== additive: a pre-existing agents block is preserved ==="
# Write a calibration with an agents block, re-run, confirm agents survive.
python3 - "$CALIB" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["agents"] = {"some-agent": {"hit_rate": 0.9}}
d["schema_version"] = 2
json.dump(d, open(p, "w"), indent=2)
PY
python3 "$SCORE" --db "$DB" --half-life-days 100000 >/dev/null 2>&1
AGENTS_KEPT=$(python3 -c "import json;print('some-agent' in json.load(open('$CALIB')).get('agents',{}))")
assert_eq "existing agents block preserved" "$AGENTS_KEPT" "True"
SCHEMA2=$(python3 -c "import json;print(json.load(open('$CALIB'))['schema_version'])")
assert_eq "schema not downgraded (stays 3)" "$SCHEMA2" "3"

echo ""
echo "=== idempotent re-run (same scores) ==="
BEFORE=$(python3 -c "import json;d=json.load(open('$CALIB'))['skills'];print(json.dumps({k:v['score'] for k,v in d.items()}, sort_keys=True))")
python3 "$SCORE" --db "$DB" --half-life-days 100000 >/dev/null 2>&1
AFTER=$(python3 -c "import json;d=json.load(open('$CALIB'))['skills'];print(json.dumps({k:v['score'] for k,v in d.items()}, sort_keys=True))")
assert_eq "scores identical on re-run" "$AFTER" "$BEFORE"

echo ""
echo "=== --dry-run does not write ==="
rm -f "$CALIB"
python3 "$SCORE" --db "$DB" --half-life-days 100000 --dry-run >/dev/null 2>&1
assert_eq "dry-run leaves no calibration file" \
    "$([[ -f "$CALIB" ]] && echo yes || echo no)" "no"

echo ""
echo "=== --min-invocations override lets B in ==="
OUT=$(python3 "$SCORE" --db "$DB" --min-invocations 9 --half-life-days 100000 --json 2>/dev/null)
B_IN=$(echo "$OUT" | python3 -c "import json,sys;print('beta:thin' in json.load(sys.stdin)['skills'])")
assert_eq "--min-invocations 9 admits skill B" "$B_IN" "True"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"

[[ $FAIL -eq 0 ]]
