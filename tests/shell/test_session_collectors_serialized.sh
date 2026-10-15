#!/usr/bin/env bash
# Regression test: concurrent SessionStart hooks must not pile up the slow
# signal collectors. One catch-up pass runs per DB at a time (flock -n), and
# it runs niced.

set -uo pipefail

command -v flock &>/dev/null || { echo "SKIP: flock not available"; exit 0; }
command -v nice &>/dev/null || { echo "SKIP: nice not available"; exit 0; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

# Minimal plugin tree: the real hook and lib, stub collectors that log their
# start, their nice value and then hold for a moment.
PLUGIN="$TEST_DIR/plugin"
mkdir -p "$PLUGIN/hooks" "$PLUGIN/scripts/signals"
cp "$SRC_DIR/hooks/"*.sh "$PLUGIN/hooks/"
for c in collect_bead_close collect_no_redirect collect_tokens; do
    cat > "$PLUGIN/scripts/signals/$c.py" <<PY
import os, sys, time
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("$c nice=%d\n" % os.nice(0))
time.sleep(1.5)
PY
done
printf 'import sys\n' > "$PLUGIN/scripts/ingest-skill-audit.py"

export STUB_LOG="$TEST_DIR/stub.log"
export CLAUDE_PROJECT_DIR="$TEST_DIR/project"
mkdir -p "$CLAUDE_PROJECT_DIR/.clavain/interspect"
cd "$CLAUDE_PROJECT_DIR" && git init -q . 2>/dev/null

PASS=0
FAIL=0
assert_eq() {
    local desc="$1" got="$2" expected="$3"
    if [[ "$got" == "$expected" ]]; then echo "  PASS: $desc"; ((PASS++)) || true
    else echo "  FAIL: $desc (got '$got', expected '$expected')"; ((FAIL++)) || true; fi
}

echo "=== Concurrent session starts: one collector pass per DB ==="
for n in 1 2 3 4; do
    echo "{\"session_id\":\"sess-$n\"}" | bash "$PLUGIN/hooks/interspect-session.sh" >/dev/null 2>&1 &
done
wait
sleep 6   # let the backgrounded pass finish (3 collectors x 1.5s)

assert_eq "each collector ran once, not once per session" \
    "$(wc -l < "$STUB_LOG" | tr -d ' ')" "3"
assert_eq "collectors ran at nice 19" \
    "$(/usr/bin/grep -c 'nice=19' "$STUB_LOG")" "3"

echo "=== Lock released: a later session runs a fresh pass ==="
echo '{"session_id":"sess-5"}' | bash "$PLUGIN/hooks/interspect-session.sh" >/dev/null 2>&1
sleep 6
assert_eq "second pass ran after the first finished" \
    "$(wc -l < "$STUB_LOG" | tr -d ' ')" "6"

echo ""
echo "Passed: $PASS  Failed: $FAIL"
(( FAIL == 0 ))
