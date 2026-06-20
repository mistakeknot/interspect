#!/usr/bin/env bash
# Tests for sylveste-7aj8.6/.7: skill tune overlays + canary + per-action autonomy.
#
# Mirrors the dual-mode pattern in test_tune_dual_mode.sh. Verifies:
#   1. skill_canary_samples table exists after _interspect_ensure_db
#   2. Skill-name validation rejects traversal / injection / bad shapes
#   3. Action selection maps the dominant signal deficit per the plan
#   4. Overlay generation emits the right named section per action
#   5. Propose path: modifications 'proposed' + override entry, NO overlay file
#   6. Per-action autonomy gate: off→propose; on+safe-list→auto;
#      on+propose-only(body_rewrite)→propose
#   7. Auto-apply: overlay file written, modifications 'applied', override 'active'
#   8. Canary regression (>20%/signal AND >10%/composite) → auto-revert
#   9. Healthy canary → 'ok', no revert
#  10. Manual revert removes the overlay file and flips state

set -eo pipefail

TEST_DIR=$(mktemp -d)
trap 'cd /; rm -rf "$TEST_DIR"' EXIT

# Isolate HOME so ~/.claude/skill-overlays writes never touch the real home.
export HOME="$TEST_DIR/home"
mkdir -p "$HOME/.claude"
export CLAUDE_PROJECT_DIR="$TEST_DIR/proj"
mkdir -p "$CLAUDE_PROJECT_DIR/.clavain/interspect" "$CLAUDE_PROJECT_DIR/.claude"

cd "$CLAUDE_PROJECT_DIR" && git init -q && git config user.name test && git config user.email test@test.com

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/hooks/lib-interspect.sh"
_interspect_ensure_db
DB=$(_interspect_db_path)

PASS=0
FAIL=0
assert_eq()       { local d="$1" g="$2" e="$3"; [[ "$g" == "$e" ]] && { echo "  PASS: $d"; ((PASS++))||true; } || { echo "  FAIL: $d (got '$g', expected '$e')"; ((FAIL++))||true; }; }
assert_contains() { local d="$1" h="$2" n="$3"; [[ "$h" == *"$n"* ]] && { echo "  PASS: $d"; ((PASS++))||true; } || { echo "  FAIL: $d (no '$n')"; ((FAIL++))||true; }; }
assert_fail()     { local d="$1"; shift; if ! "$@" >/dev/null 2>&1; then echo "  PASS: $d"; ((PASS++))||true; else echo "  FAIL: $d (expected non-zero)"; ((FAIL++))||true; fi; }
overlay_path()    { printf '%s/.claude/skill-overlays/%s.md' "$HOME" "$1"; }

# Seed skill_signals for a skill across N invocations. $1=skill $2=n $3=no_redirect $4=tokens $5=error
seed_signals() {
    local skill="$1" n="$2" nr="$3" tok="$4" err="$5"
    local i
    for i in $(seq 1 "$n"); do
        sqlite3 "$DB" "INSERT INTO skill_signals (skill_name,session_id,invocation_id,signal_kind,value,observed_at) VALUES
          ('$skill','s$i','${skill//:/_}-inv$i','no_redirect',$nr,datetime('now')),
          ('$skill','s$i','${skill//:/_}-inv$i','tokens',$tok,datetime('now')),
          ('$skill','s$i','${skill//:/_}-inv$i','error',$err,datetime('now'));"
    done
}

echo "=== Group 1: schema ==="
HAS_TABLE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='skill_canary_samples';")
assert_eq "skill_canary_samples table exists" "$HAS_TABLE" "1"
HAS_DELTA=$(sqlite3 "$DB" ".schema skill_canary_samples" | grep -c per_signal_delta)
assert_eq "skill_canary_samples has per_signal_delta column" "$HAS_DELTA" "1"

echo ""
echo "=== Group 2: name validation ==="
assert_fail "rejects path traversal" _interspect_validate_skill_name "../etc/passwd"
assert_fail "rejects slash" _interspect_validate_skill_name "clavain/work"
assert_fail "rejects shell injection" _interspect_validate_skill_name 'work$(whoami)'
assert_fail "rejects double colon" _interspect_validate_skill_name "a:b:c"
assert_fail "rejects uppercase" _interspect_validate_skill_name "Clavain:Work"
_interspect_validate_skill_name "clavain:work" && { echo "  PASS: accepts plugin:skill"; ((PASS++))||true; } || { echo "  FAIL: plugin:skill rejected"; ((FAIL++))||true; }
_interspect_validate_skill_name "deep-research" && { echo "  PASS: accepts bare hyphenated skill"; ((PASS++))||true; } || { echo "  FAIL: bare skill rejected"; ((FAIL++))||true; }

echo ""
echo "=== Group 3: action selection ==="
seed_signals "clavain:work" 12 0.4 0.85 0.95     # no_redirect worst → tighten_description
assert_eq "no_redirect deficit → tighten_description" "$(_interspect_select_skill_action clavain:work)" "tighten_description"
seed_signals "clavain:lint" 12 0.9 0.4 0.95      # tokens worst → when_to_use_add
assert_eq "tokens deficit → when_to_use_add" "$(_interspect_select_skill_action clavain:lint)" "when_to_use_add"
seed_signals "clavain:audit" 12 0.9 0.9 0.5      # error worst → skill_md_body_rewrite
assert_eq "error deficit → skill_md_body_rewrite" "$(_interspect_select_skill_action clavain:audit)" "skill_md_body_rewrite"
seed_signals "clavain:rare" 4 0.95 0.95 0.98     # healthy + low utilization → availability
assert_eq "healthy + low usage → availability" "$(_interspect_select_skill_action clavain:rare)" "availability"

echo ""
echo "=== Group 4: overlay generation ==="
GEN=$(_interspect_generate_skill_overlay "clavain:work" "tighten_description")
assert_contains "tighten_description → description-overlay section" "$GEN" "## description-overlay"
GEN2=$(_interspect_generate_skill_overlay "clavain:lint" "when_to_use_add")
assert_contains "when_to_use_add → when-to-use-overlay section" "$GEN2" "## when-to-use-overlay"
assert_fail "no-evidence skill returns error" _interspect_generate_skill_overlay "clavain:ghost" "tighten_description"

echo ""
echo "=== Group 5: propose path (no overlay file) ==="
CONTENT=$(_interspect_generate_skill_overlay "clavain:work" "tighten_description")
PROP=$(_interspect_propose_skill_tune "clavain:work" "tighten_description" "$CONTENT" "[1,2]")
assert_contains "propose reports PROPOSED" "$PROP" "PROPOSED"
PMOD=$(echo "$PROP" | sed -n 's/.*modification_id=\([0-9]*\).*/\1/p')
assert_eq "proposed modification recorded" "$(sqlite3 "$DB" "SELECT status FROM modifications WHERE id=$PMOD;")" "proposed"
assert_eq "override entry state=proposed" "$(jq -r '.overrides[]|select(.kind=="skill_tune" and .skill=="clavain:work")|.state' .claude/routing-overrides.json)" "proposed"
[[ ! -f "$(overlay_path clavain:work)" ]] && { echo "  PASS: propose writes NO overlay file"; ((PASS++))||true; } || { echo "  FAIL: overlay file written on propose"; ((FAIL++))||true; }

echo ""
echo "=== Group 6: per-action autonomy gate ==="
# Autonomy OFF (no confidence.json autonomy) → propose
assert_fail "autonomy off → not auto-apply" _interspect_skill_should_auto_apply "clavain:lint" "tighten_description"
# Turn autonomy ON + policy file
echo '{"autonomy":true}' > .clavain/interspect/confidence.json
echo '{"auto_apply":["tighten_description","when_to_use_add"],"propose_only":["skill_md_body_rewrite","availability"]}' > .clavain/interspect/skill-autonomy-policy.json
unset _INTERSPECT_CONFIDENCE_LOADED _INTERSPECT_SKILL_POLICY_LOADED
_interspect_skill_should_auto_apply "clavain:lint" "when_to_use_add" && { echo "  PASS: autonomy on + safe-list → auto-apply"; ((PASS++))||true; } || { echo "  FAIL: safe-list action did not auto-apply"; ((FAIL++))||true; }
assert_fail "autonomy on + body_rewrite stays propose (safe-list)" _interspect_skill_should_auto_apply "clavain:audit" "skill_md_body_rewrite"

echo ""
echo "=== Group 7: auto-apply write ==="
LC=$(_interspect_generate_skill_overlay "clavain:lint" "when_to_use_add")
OUT=$(_interspect_write_skill_overlay "clavain:lint" "when_to_use_add" "$LC" "[3,4]")
MOD=$(echo "$OUT" | sed -n 's/.*modification_id=\([0-9]*\).*/\1/p')
[[ -f "$(overlay_path clavain:lint)" ]] && { echo "  PASS: overlay file written"; ((PASS++))||true; } || { echo "  FAIL: overlay file missing"; ((FAIL++))||true; }
assert_contains "overlay frontmatter carries modification_id" "$(cat "$(overlay_path clavain:lint)")" "modification_id: $MOD"
assert_eq "modification applied" "$(sqlite3 "$DB" "SELECT status FROM modifications WHERE id=$MOD;")" "applied"
assert_eq "override entry state=active" "$(jq -r '.overrides[]|select(.skill=="clavain:lint")|.state' .claude/routing-overrides.json)" "active"

echo ""
echo "=== Group 8: canary regression → auto-revert ==="
# baseline 0.8 → canary 0.5 (no_redirect −37%), 0.55 (tokens −31%); composite ~−34%
for i in $(seq 1 5); do
    _interspect_record_skill_canary_sample "$MOD" "clavain:lint" "creg$i" "no_redirect" "0.8" "0.5" >/dev/null
    _interspect_record_skill_canary_sample "$MOD" "clavain:lint" "creg$i" "tokens" "0.8" "0.55" >/dev/null
done
VERDICT=$(_interspect_evaluate_skill_canary "$MOD")
assert_eq "regression verdict is 'reverted'" "$VERDICT" "reverted"
assert_eq "modification flipped to reverted" "$(sqlite3 "$DB" "SELECT status FROM modifications WHERE id=$MOD;")" "reverted"
[[ ! -f "$(overlay_path clavain:lint)" ]] && { echo "  PASS: overlay file removed on revert"; ((PASS++))||true; } || { echo "  FAIL: overlay file survived revert"; ((FAIL++))||true; }
assert_eq "override entry state=reverted" "$(jq -r '.overrides[]|select(.skill=="clavain:lint")|.state' .claude/routing-overrides.json)" "reverted"

echo ""
echo "=== Group 9: healthy canary → ok ==="
seed_signals "clavain:keep" 12 0.7 0.7 0.9
KC=$(_interspect_generate_skill_overlay "clavain:keep" "tighten_description")
OUT2=$(_interspect_write_skill_overlay "clavain:keep" "tighten_description" "$KC" "[]")
MOD2=$(echo "$OUT2" | sed -n 's/.*modification_id=\([0-9]*\).*/\1/p')
for i in $(seq 1 5); do
    _interspect_record_skill_canary_sample "$MOD2" "clavain:keep" "cok$i" "no_redirect" "0.7" "0.71" >/dev/null
    _interspect_record_skill_canary_sample "$MOD2" "clavain:keep" "cok$i" "tokens" "0.7" "0.72" >/dev/null
done
assert_eq "healthy canary verdict is 'ok'" "$(_interspect_evaluate_skill_canary "$MOD2")" "ok"
assert_eq "healthy modification stays applied" "$(sqlite3 "$DB" "SELECT status FROM modifications WHERE id=$MOD2;")" "applied"

echo ""
echo "=== Group 10: manual revert ==="
[[ -f "$(overlay_path clavain:keep)" ]] && { echo "  PASS: overlay present pre-revert"; ((PASS++))||true; } || { echo "  FAIL: overlay missing pre-revert"; ((FAIL++))||true; }
_interspect_disable_skill_overlay "clavain:keep" >/dev/null
[[ ! -f "$(overlay_path clavain:keep)" ]] && { echo "  PASS: manual revert removes overlay"; ((PASS++))||true; } || { echo "  FAIL: overlay survived manual revert"; ((FAIL++))||true; }
assert_eq "manual revert flips modification" "$(sqlite3 "$DB" "SELECT status FROM modifications WHERE id=$MOD2;")" "reverted"

echo ""
echo "─────────────────────────"
echo "PASS: $PASS  FAIL: $FAIL"
echo "─────────────────────────"
[[ $FAIL -eq 0 ]]
