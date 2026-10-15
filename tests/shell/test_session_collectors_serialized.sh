#!/usr/bin/env bash
# Regression test: concurrent SessionStart hooks must not pile up the slow
# signal collectors. One catch-up pass runs per DB at a time (flock -n), and
# it runs niced. Also: an unwritable lock location must stay silent and
# fail-open.
#
# Synchronization is by files, not fixed sleeps: the stub collectors hold the
# first pass open until the test releases it, and every wait is a bounded poll.

set -uo pipefail

command -v flock &>/dev/null || { echo "SKIP: flock not available"; exit 0; }
command -v nice &>/dev/null || { echo "SKIP: nice not available"; exit 0; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

PLUGIN="$TEST_DIR/plugin"
mkdir -p "$PLUGIN/hooks" "$PLUGIN/scripts/signals"
cp "$SRC_DIR/hooks/"*.sh "$PLUGIN/hooks/"
# Stub collectors: log name + nice value, then wait for the release file.
for c in collect_bead_close collect_no_redirect collect_tokens; do
    cat > "$PLUGIN/scripts/signals/$c.py" <<PY
import os, time
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("$c nice=%d\n" % os.nice(0))
for _ in range(600):
    if os.path.exists(os.environ["STUB_RELEASE"]):
        break
    time.sleep(0.1)
PY
done
printf 'import sys\n' > "$PLUGIN/scripts/ingest-skill-audit.py"

export STUB_LOG="$TEST_DIR/stub.log"
export STUB_RELEASE="$TEST_DIR/release"
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
mkdir -p "$CLAUDE_PROJECT_DIR/.clavain/interspect"
(cd "$CLAUDE_PROJECT_DIR" && git init -q .) 2>/dev/null
LOCK="$CLAUDE_PROJECT_DIR/.clavain/interspect/interspect.db.collect.lock"

PASS=0
FAIL=0
assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (got '$got', expected '$expected')"; ((FAIL++)) || true; fi
}
log_lines() { [[ -f "$STUB_LOG" ]] && wc -l < "$STUB_LOG" | tr -d ' ' || echo 0; }
# wait_until <seconds> <command...>: bounded poll, returns the command's status.
wait_until() {
    local deadline=$((SECONDS + $1)); shift
    until "$@"; do (( SECONDS >= deadline )) && return 1; sleep 0.1; done
}
lock_free() { ( exec 8>"$LOCK" && flock -n 8 ) 2>/dev/null; }
first_started() { [[ -f "$STUB_LOG" ]] && /usr/bin/grep -q collect_bead_close "$STUB_LOG"; }
pass_done() { [[ "$(log_lines)" == "$1" ]] && lock_free; }

run_hook() { echo "{\"session_id\":\"$1\"}" | bash "$PLUGIN/hooks/interspect-session.sh" >/dev/null 2>&1; }

echo "=== Concurrent session starts: one collector pass per DB ==="
for n in 1 2 3 4; do run_hook "sess-$n" & done
wait
wait_until 30 first_started || echo "  (first collector never started)"
sleep 2   # let any skipped passes attempt, and lose, the lock while it is held
touch "$STUB_RELEASE"
wait_until 60 pass_done 3
assert_eq "each collector ran once, not once per session" "$(log_lines)" "3"
assert_eq "collectors ran at nice 19" "$(/usr/bin/grep -c 'nice=19' "$STUB_LOG")" "3"

echo "=== Lock released: a later session runs a fresh pass ==="
run_hook "sess-5"
wait_until 60 pass_done 6
assert_eq "second pass ran after the first finished" "$(log_lines)" "6"

echo "=== Unwritable lock path: silent and fail-open ==="
rm -f "$LOCK"
mkdir "$LOCK"   # a directory where the lock file should be: the redirection fails
ERR=$(echo '{"session_id":"sess-6"}' | bash "$PLUGIN/hooks/interspect-session.sh" 2>&1 >/dev/null)
sleep 1
assert_eq "no stderr output" "$ERR" ""
assert_eq "no collector started" "$(log_lines)" "6"

echo ""
echo "Passed: $PASS  Failed: $FAIL"
(( FAIL == 0 ))
