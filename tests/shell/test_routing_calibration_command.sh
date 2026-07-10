#!/usr/bin/env bash
set -uo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMMAND="$SCRIPT_DIR/scripts/write-routing-calibration.sh"
LIB="$SCRIPT_DIR/hooks/lib-interspect.sh"

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

run_command() {
    local project_dir="$1"
    set +e
    CLAUDE_PROJECT_DIR="$project_dir" bash "$COMMAND" >/dev/null 2>&1
    local status=$?
    set -e
    printf '%s\n' "$status"
}

echo "=== Interspect routing calibration command tests ==="

echo ""
echo "Group 1: empty evidence is a valid no-op"
empty_project="$TEST_DIR/empty"
mkdir -p "$empty_project"
status=$(run_command "$empty_project")
assert_eq "empty evidence returns 2" "$status" "2"
if [[ -e "$empty_project/.clavain/interspect/routing-calibration.json" ]]; then
    assert_eq "empty evidence does not write an artifact" "present" "absent"
else
    assert_eq "empty evidence does not write an artifact" "absent" "absent"
fi

echo ""
echo "Group 2: sufficient evidence replaces the calibration artifact"
valid_project="$TEST_DIR/valid"
mkdir -p "$valid_project/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$valid_project"
# shellcheck source=/dev/null
source "$LIB"
unset _INTERSPECT_DB
_interspect_ensure_db
db=$(_interspect_db_path)
sqlite3 "$db" "
INSERT INTO sessions (session_id, start_ts, end_ts, project, source) VALUES
  ('route-1', datetime('now','-3 days'), datetime('now','-3 days','+1 hour'), 'test', 'normal'),
  ('route-2', datetime('now','-2 days'), datetime('now','-2 days','+1 hour'), 'test', 'normal'),
  ('route-3', datetime('now','-1 days'), datetime('now','-1 days','+1 hour'), 'test', 'normal');
"
_interspect_record_verdict "route-1" "fd-quality" "NEEDS_ATTENTION" 1 "sonnet" "shipping" >/dev/null
_interspect_record_verdict "route-2" "fd-quality" "NEEDS_ATTENTION" 1 "sonnet" "shipping" >/dev/null
_interspect_record_verdict "route-3" "fd-quality" "NEEDS_ATTENTION" 1 "sonnet" "shipping" >/dev/null
artifact="$valid_project/.clavain/interspect/routing-calibration.json"
printf '{"stale":true}\n' > "$artifact"
before=$(cat "$artifact")
status=$(run_command "$valid_project")
after=$(cat "$artifact" 2>/dev/null || true)
assert_eq "sufficient evidence returns 0" "$status" "0"
if [[ "$after" != "$before" ]]; then
    assert_eq "calibration artifact changes" "changed" "changed"
else
    assert_eq "calibration artifact changes" "unchanged" "changed"
fi
assert_eq "artifact contains scored agent" \
    "$(jq -r '.agents["fd-quality"].recommended_model // empty' "$artifact" 2>/dev/null)" \
    "sonnet"

echo ""
echo "Group 3: the SessionEnd sequence has exactly one routing writer"
if [[ -f "$COMMAND" ]]; then
    # shellcheck source=/dev/null
    source "$COMMAND"
    writer_calls=0
    _interspect_ensure_db() { return 0; }
    _interspect_write_routing_calibration() {
        ((writer_calls++)) || true
        return 0
    }
    fake_bin="$TEST_DIR/bin"
    mkdir -p "$fake_bin"
    cat > "$fake_bin/clavain-cli" <<'SH'
#!/usr/bin/env bash
exit 0
SH
    chmod +x "$fake_bin/clavain-cli"
    PATH="$fake_bin:$PATH"
    interspect_write_routing_calibration_main >/dev/null 2>&1
    main_status=$?
    _interspect_auto_calibrate >/dev/null 2>&1
    assert_eq "authoritative command succeeds" "$main_status" "0"
    assert_eq "writer called exactly once" "$writer_calls" "1"
else
    assert_eq "authoritative command exists" "missing" "present"
fi

echo ""
echo "Group 4: setup failure is a hard failure"
if declare -F interspect_write_routing_calibration_main >/dev/null; then
    _interspect_ensure_db() { return 1; }
    set +e
    interspect_write_routing_calibration_main >/dev/null 2>&1
    hard_status=$?
    set -e
    assert_eq "setup failure returns 1" "$hard_status" "1"
else
    assert_eq "hard-failure contract is callable" "missing" "present"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
