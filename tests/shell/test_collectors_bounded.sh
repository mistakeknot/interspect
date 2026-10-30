#!/usr/bin/env bash
# Regression test: signal collectors are bounded. The session hook runs each
# collector under a timeout and a virtual-memory cap, collect_no_redirect
# streams transcripts (and skips oversize ones), and collect_tokens caches
# per-transcript totals so a pass only parses new transcripts.
set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR=$(mktemp -d) && [[ -n "$TEST_DIR" && -d "$TEST_DIR" ]] || { echo "FAIL: mktemp -d"; exit 1; }
trap 'rm -rf "$TEST_DIR"' EXIT
PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== hook bounds ==="
grep -q 'timeout -k' "$SRC_DIR/hooks/interspect-session.sh" && ok "hook wraps collectors in timeout" || bad "hook has no timeout"
grep -q 'ulimit -v' "$SRC_DIR/hooks/interspect-session.sh" && ok "hook sets a memory cap" || bad "hook has no memory cap"

echo "=== no_redirect streams and caps ==="
T="$TEST_DIR/s.jsonl"
cat > "$T" <<'J'
{"type":"user","message":{"content":"hello"},"timestamp":"2026-01-01T00:00:00Z"}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"foo"}}]},"timestamp":"2026-01-01T00:00:01Z"}
{"type":"user","message":{"content":"wait, stop"},"timestamp":"2026-01-01T00:00:02Z"}
{"type":"user","message":{"content":"looks fine"},"timestamp":"2026-01-01T00:00:03Z"}
J
V=$(python3 -I - "$SRC_DIR/scripts/signals" "$T" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import collect_no_redirect as c
from pathlib import Path
print(c.score_followup(Path(sys.argv[2]), "foo", "", 5))
c.MAX_TRANSCRIPT_BYTES = 10
print(c.score_followup(Path(sys.argv[2]), "foo", "", 5))
PY
)
[[ "$(echo "$V" | sed -n 1p)" == "0.0" ]] && ok "scores redirects (both user turns redirect)" || bad "score wrong: $V"
[[ "$(echo "$V" | sed -n 2p)" == "None" ]] && ok "oversize transcript skipped" || bad "oversize not skipped: $V"

echo "=== tokens cache ==="
R=$(python3 -I - "$SRC_DIR/scripts/signals" "$TEST_DIR" <<'PY'
import sys, json
sys.path.insert(0, sys.argv[1])
import collect_tokens as c
from pathlib import Path
d = Path(sys.argv[2])
p = d / "t.jsonl"
p.write_text(json.dumps({"type":"assistant","message":{"usage":{"input_tokens":5,"output_tokens":7}}}) + "\n")
cache_path = d / "cache.json"
cache = c._load_cache(cache_path)
st = p.stat()
cache[str(p)] = {"sig": [st.st_size, int(st.st_mtime)], "tokens": 12}
c._save_cache(cache_path, cache, {str(p)})
back = c._load_cache(cache_path)
print(back[str(p)]["tokens"])
c._save_cache(cache_path, back, set())
print(len(c._load_cache(cache_path)))
PY
)
[[ "$(echo "$R" | sed -n 1p)" == "12" ]] && ok "cache round-trips" || bad "cache round-trip: $R"
[[ "$(echo "$R" | sed -n 2p)" == "0" ]] && ok "cache prunes dead transcripts" || bad "cache not pruned: $R"

echo; echo "Passed: $PASS  Failed: $FAIL"
[[ $FAIL -eq 0 ]]
