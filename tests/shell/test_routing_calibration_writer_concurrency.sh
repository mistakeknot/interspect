#!/usr/bin/env bash
# Regression coverage for the shared routing-calibration writer contract.

set -uo pipefail

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT_WRITER="$SCRIPT_DIR/scripts/write-routing-calibration.sh"
SKILL_WRITER="$SCRIPT_DIR/scripts/score-skills.py"
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

wait_for_path() {
    local path="$1"
    local attempts=0
    while [[ ! -e "$path" && "$attempts" -lt 500 ]]; do
        sleep 0.01
        attempts=$((attempts + 1))
    done
    [[ -e "$path" ]]
}

run_agent_writer() {
    CLAUDE_PROJECT_DIR="$1" bash "$AGENT_WRITER" >/dev/null 2>&1
}

run_skill_writer() {
    python3 "$SKILL_WRITER" --db "$1/.clavain/interspect/interspect.db" \
        --min-invocations 1 --half-life-days 100000 --static-weights \
        >/dev/null 2>&1
}

make_project() {
    local project="$1"
    mkdir -p "$project/.clavain/interspect"
    cp "$TEMPLATE_DB" "$project/.clavain/interspect/interspect.db"
}

echo "=== Shared routing calibration writer tests ==="

# Seed one reusable database that qualifies for both actual writer entry points.
template="$TEST_DIR/template"
mkdir -p "$template/.clavain/interspect"
export CLAUDE_PROJECT_DIR="$template"
# shellcheck source=/dev/null
source "$LIB"
unset _INTERSPECT_DB
_interspect_ensure_db
TEMPLATE_DB=$(_interspect_db_path)
sqlite3 "$TEMPLATE_DB" "
INSERT INTO sessions (session_id, start_ts, end_ts, project, source) VALUES
  ('agent-1', datetime('now','-3 days'), datetime('now','-3 days','+1 hour'), 'test', 'normal'),
  ('agent-2', datetime('now','-2 days'), datetime('now','-2 days','+1 hour'), 'test', 'normal'),
  ('agent-3', datetime('now','-1 days'), datetime('now','-1 days','+1 hour'), 'test', 'normal');
"
_interspect_record_verdict "agent-1" "fd-concurrent" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null
_interspect_record_verdict "agent-2" "fd-concurrent" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null
_interspect_record_verdict "agent-3" "fd-concurrent" "NEEDS_ATTENTION" 1 "sonnet" "ship" >/dev/null
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sqlite3 "$TEMPLATE_DB" "
INSERT INTO evidence
  (ts, session_id, seq, source, event, context, project, source_event_id,
   source_table, quarantine_until, source_kind)
VALUES
  ('$NOW', 'skill-session', 1, 'demo:skill', 'skill_invocation', '{}',
   'test', 'skill-invocation-1', 'skill_signals', 0, 'skill');
INSERT INTO skill_signals
  (skill_name, session_id, invocation_id, signal_kind, value, raw_value, observed_at)
VALUES
  ('demo:skill', 'skill-session', 'skill-invocation-1', 'error', 1.0, 1.0, '$NOW');
"

echo ""
echo "Group 1: an agent write preserves skill-owned and additive sibling data"
project="$TEST_DIR/agent-preserves-skills"
make_project "$project"
artifact="$project/.clavain/interspect/routing-calibration.json"
run_skill_writer "$project"
python3 - "$artifact" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path))
data["additive_sibling"] = {"owner": "future-writer", "kept": True}
with open(path, "w") as handle:
    json.dump(data, handle, sort_keys=True)
    handle.write("\n")
PY
set +e
run_agent_writer "$project"
agent_status=$?
set -e
assert_eq "agent writer succeeds" "$agent_status" "0"
assert_eq "agent writer keeps schema 3" "$(jq -r '.schema_version' "$artifact" 2>/dev/null)" "3"
assert_eq "agent writer keeps skill block" \
    "$(jq -r '.skills["demo:skill"].skill // empty' "$artifact" 2>/dev/null)" "demo:skill"
assert_eq "agent writer keeps skill metadata" \
    "$(jq -r '.skills_calibration.min_invocations // empty' "$artifact" 2>/dev/null)" "1"
assert_eq "agent writer keeps unknown sibling field" \
    "$(jq -r '.additive_sibling.owner // empty' "$artifact" 2>/dev/null)" "future-writer"
assert_eq "agent writer updates its own block" \
    "$(jq -r '.agents["fd-concurrent"].recommended_model // empty' "$artifact" 2>/dev/null)" "sonnet"

echo ""
echo "Group 2: invalid existing evidence is rejected without mutation"
write_invalid_fixture() {
    local kind="$1" path="$2"
    case "$kind" in
        malformed) printf '{"schema_version":2,\n' > "$path" ;;
        duplicate) printf '{"schema_version":2,"agents":{},"agents":{"duplicate":{}}}\n' > "$path" ;;
        nonfinite) printf '{"schema_version":2,"future_metric":NaN}\n' > "$path" ;;
        missing_schema) printf '{"agents":{},"skills":{}}\n' > "$path" ;;
        overflow_agent) printf '{"schema_version":2,"agents":{"old":{"confidence":1e999}}}\n' > "$path" ;;
        overflow_skill) printf '{"schema_version":3,"skills":{"old":{"score":1e999}}}\n' > "$path" ;;
        unsupported) printf '{"schema_version":4,"future":true}\n' > "$path" ;;
        nonobject) printf '[{"schema_version":2}]\n' > "$path" ;;
    esac
}

for writer in agent skill; do
    for invalid in malformed duplicate nonfinite unsupported nonobject missing_schema overflow_agent overflow_skill; do
        project="$TEST_DIR/invalid-$writer-$invalid"
        make_project "$project"
        artifact="$project/.clavain/interspect/routing-calibration.json"
        write_invalid_fixture "$invalid" "$artifact"
        cp "$artifact" "$project/original.json"
        set +e
        if [[ "$writer" == agent ]]; then
            run_agent_writer "$project"
        else
            run_skill_writer "$project"
        fi
        status=$?
        set -e
        if [[ "$status" -ne 0 ]]; then
            visible="yes"
        else
            visible="no"
        fi
        assert_eq "$writer rejects $invalid evidence visibly" "$visible" "yes"
        if cmp -s "$project/original.json" "$artifact"; then
            preserved="yes"
        else
            preserved="no"
        fi
        assert_eq "$writer preserves $invalid evidence bytes" "$preserved" "yes"
    done
done

echo ""
echo "Group 3: every successful write archives unique exact committed bytes"
project="$TEST_DIR/history"
make_project "$project"
artifact="$project/.clavain/interspect/routing-calibration.json"
run_skill_writer "$project"
cp "$artifact" "$project/skill-committed.json"
run_agent_writer "$project"
cp "$artifact" "$project/agent-committed.json"
history="$project/.clavain/interspect/calibration-history"
snapshot_count=$(find "$history" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
assert_eq "two writes produce two history snapshots" "$snapshot_count" "2"
skill_snapshot=no
agent_snapshot=no
while IFS= read -r snapshot; do
    cmp -s "$snapshot" "$project/skill-committed.json" && skill_snapshot=yes
    cmp -s "$snapshot" "$project/agent-committed.json" && agent_snapshot=yes
done < <(find "$history" -maxdepth 1 -type f -name '*.json' 2>/dev/null)
assert_eq "history contains exact skill commit bytes" "$skill_snapshot" "yes"
assert_eq "history contains exact agent commit bytes" "$agent_snapshot" "yes"
history_compatible=$(python3 - "$SCRIPT_DIR/scripts/calibrate-audit.py" "$history" <<'PY'
import importlib.util
import sys

module_path, history_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("calibrate_audit", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
print("yes" if module.find_oldest_snapshot(module.Path(history_path), -1) else "no")
PY
)
assert_eq "new snapshot names remain calibration-audit compatible" "$history_compatible" "yes"

echo ""
echo "Group 4: archive failure remains best-effort"
project="$TEST_DIR/history-failure"
make_project "$project"
history="$project/.clavain/interspect/calibration-history"
printf 'not-a-directory\n' > "$history"
set +e
run_skill_writer "$project"
skill_status=$?
run_agent_writer "$project"
agent_status=$?
set -e
assert_eq "skill write survives archive failure" "$skill_status" "0"
assert_eq "agent write survives archive failure" "$agent_status" "0"
artifact="$project/.clavain/interspect/routing-calibration.json"
assert_eq "archive failure still commits both sibling blocks" \
    "$(jq -r 'has("agents") and has("skills")' "$artifact" 2>/dev/null)" "true"

echo ""
echo "Group 5: simultaneous actual writers serialize behind the same lock"
project="$TEST_DIR/concurrent"
make_project "$project"
artifact="$project/.clavain/interspect/routing-calibration.json"
printf '{"schema_version":1,"additive_sibling":{"seed":true}}\n' > "$artifact"
lock_path="${artifact}.lock"
lock_ready="$project/lock-ready"
lock_release="$project/lock-release"
python3 - "$lock_path" "$lock_ready" "$lock_release" <<'PY' &
import fcntl
import os
import sys
import time

lock_path, ready_path, release_path = sys.argv[1:]
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
with os.fdopen(fd, "r+") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    open(ready_path, "w").close()
    while not os.path.exists(release_path):
        time.sleep(0.01)
PY
lock_pid=$!
wait_for_path "$lock_ready"

go="$project/go"
agent_started="$project/agent-started"
skill_started="$project/skill-started"
agent_done="$project/agent-done"
skill_done="$project/skill-done"
(
    : > "$agent_started"
    wait_for_path "$go"
    set +e
    run_agent_writer "$project"
    echo "$?" > "$project/agent-status"
    : > "$agent_done"
) &
agent_pid=$!
(
    : > "$skill_started"
    wait_for_path "$go"
    set +e
    run_skill_writer "$project"
    echo "$?" > "$project/skill-status"
    : > "$skill_done"
) &
skill_pid=$!
wait_for_path "$agent_started"
wait_for_path "$skill_started"
: > "$go"

# The start barrier makes the workers concurrent; the independently held lock
# must keep both entry points from committing until it is released.
for _ in $(seq 1 150); do
    [[ -e "$agent_done" || -e "$skill_done" ]] && break
    sleep 0.01
done
if [[ -e "$agent_done" || -e "$skill_done" ]]; then
    blocked=no
else
    blocked=yes
fi
assert_eq "both writers wait for the shared advisory lock" "$blocked" "yes"
: > "$lock_release"
wait "$lock_pid"
wait "$agent_pid"
wait "$skill_pid"
assert_eq "concurrent agent writer exits 0" "$(cat "$project/agent-status")" "0"
assert_eq "concurrent skill writer exits 0" "$(cat "$project/skill-status")" "0"
assert_eq "concurrent result keeps agents" \
    "$(jq -r '.agents["fd-concurrent"].recommended_model // empty' "$artifact" 2>/dev/null)" "sonnet"
assert_eq "concurrent result keeps skills" \
    "$(jq -r '.skills["demo:skill"].skill // empty' "$artifact" 2>/dev/null)" "demo:skill"
assert_eq "concurrent skill-bearing result is schema 3" \
    "$(jq -r '.schema_version // empty' "$artifact" 2>/dev/null)" "3"
concurrent_snapshots=$(find "$project/.clavain/interspect/calibration-history" \
    -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
assert_eq "concurrent writes retain two distinct snapshots" "$concurrent_snapshots" "2"

echo ""
echo "Group 6: lock failures are bounded, visible, and preserve evidence"
cp "$artifact" "$project/before-timeout.json"
timeout_ready="$project/timeout-lock-ready"
timeout_release="$project/timeout-lock-release"
python3 - "$lock_path" "$timeout_ready" "$timeout_release" <<'PY' &
import fcntl
import os
import sys
import time

lock_path, ready_path, release_path = sys.argv[1:]
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
with os.fdopen(fd, "r+") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    open(ready_path, "w").close()
    while not os.path.exists(release_path):
        time.sleep(0.01)
PY
timeout_lock_pid=$!
wait_for_path "$timeout_ready"
set +e
python3 "$SCRIPT_DIR/scripts/routing_calibration_writer.py" \
    --calibration "$artifact" --writer agent --lock-timeout 0.1 \
    >"$project/timeout-out" 2>"$project/timeout-err" <<'JSON'
{"calibrated_at":"2026-09-13T00:00:00Z","min_sessions":3,"min_non_bootstrap_sessions":3,"source_weights":{},"agents":{}}
JSON
timeout_status=$?
set -e
: > "$timeout_release"
wait "$timeout_lock_pid"
assert_eq "lock timeout returns a hard failure" "$timeout_status" "1"
assert_eq "lock timeout is reported" \
    "$(grep -c 'timed out.*waiting for calibration lock' "$project/timeout-err" || true)" "1"
if cmp -s "$project/before-timeout.json" "$artifact"; then
    timeout_preserved=yes
else
    timeout_preserved=no
fi
assert_eq "lock timeout preserves artifact bytes" "$timeout_preserved" "yes"

unsupported_result=$(python3 - "$SCRIPT_DIR/scripts/routing_calibration_writer.py" \
    "$project/unsupported-parent/routing-calibration.json" <<'PY'
import importlib.util
import sys
from pathlib import Path

module_path, artifact_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("routing_calibration_writer", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

path = Path(artifact_path)
update = {
    "calibrated_at": "2026-09-13T00:00:00Z",
    "min_sessions": 3,
    "min_non_bootstrap_sessions": 3,
    "source_weights": {},
    "agents": {},
}

def unsupported():
    raise module.CalibrationWriteError("advisory file locking is unsupported")

module._locking_module = unsupported
try:
    module.merge_calibration(path, writer="agent", update=update)
except module.CalibrationWriteError as exc:
    visible = "unsupported" in str(exc)
else:
    visible = False
print(f"{'visible' if visible else 'silent'}:{'mutated' if path.parent.exists() else 'clean'}")
PY
)
assert_eq "unsupported locking fails before filesystem mutation" \
    "$unsupported_result" "visible:clean"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] && echo "All tests passed." || exit 1
