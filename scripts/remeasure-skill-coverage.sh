#!/usr/bin/env bash
# Re-measure skill-calibration signal coverage against the live ~/.claude/audit.log.
#
# Scheduled via zklw cron for ~2026-07-05 (beads sylveste-ysny / sylveste-7aj8.9):
# checks whether current-session data accumulated since the audit.log producer was
# revived (2026-06-21) raised behavioral-signal coverage above the ~3% baseline.
#
# Read-only w.r.t. the real evidence DB: runs the whole pipeline against a scratch
# DB in a temp dir. Never enables autonomy, never edits code. Writes a dated
# markdown report to ~/.cache/interspect/ and best-effort appends a pointer to the
# bead. Safe to run anytime.
set -uo pipefail

REPO="/home/mk/projects/Sylveste/interverse/interspect"
OUT_DIR="$HOME/.cache/interspect"
DATE_UTC="$(date -u +%Y-%m-%d)"
REPORT="$OUT_DIR/skill-coverage-${DATE_UTC}.md"
WINDOW_DAYS="${REMEASURE_WINDOW_DAYS:-60}"   # inclusive of ~2wk of current data
MIN_INV="${REMEASURE_MIN_INV:-3}"            # lowered for sparse current data

mkdir -p "$OUT_DIR"
cd "$REPO" 2>/dev/null || { echo "interspect repo not found at $REPO" >&2; exit 0; }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export CLAUDE_PROJECT_DIR="$SCRATCH"
mkdir -p "$SCRATCH/.clavain/interspect"
DB="$SCRATCH/.clavain/interspect/interspect.db"

# Migrate the scratch schema via the lib.
( source hooks/lib-interspect.sh && _interspect_ensure_db ) >/dev/null 2>&1 || true

# 1) Ingest — adapter auto-selects ~/.claude/audit.log when present+non-empty,
#    else falls back to tool-time/events.jsonl. Capture which source it chose.
ING="$(python3 scripts/ingest-skill-audit.py --db "$DB" 2>&1)"
SRC="$(printf '%s' "$ING" | grep -oE 'format=[a-z]+' | head -1)"

# 2) Signal collectors.
for c in collect_error collect_bead_close collect_no_redirect collect_tokens; do
  python3 "scripts/signals/${c}.py" --db "$DB" >/dev/null 2>&1 || true
done

# 3) Goal classification (mock — no API spend).
python3 scripts/infer-skill-goals.py --mock --db "$DB" >/dev/null 2>&1 || true

# 4) Variance-aware scoring + leaderboard.
LEADER="$(python3 scripts/score-skills.py --db "$DB" --window-days "$WINDOW_DAYS" --min-invocations "$MIN_INV" 2>&1)"

# Coverage metrics (independent of scoring threshold).
TOTAL_INV="$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source_kind='skill';" 2>/dev/null)"
COVERAGE="$(sqlite3 "$DB" "SELECT signal_kind || ': ' || COUNT(*) || ' rows / ' || COUNT(DISTINCT skill_name) || ' skills' FROM skill_signals GROUP BY signal_kind;" 2>/dev/null)"

{
  echo "# Skill-calibration coverage re-measure — ${DATE_UTC} (UTC)"
  echo
  echo "Ingest source selected: **${SRC:-unknown}** (audit.log producer revived 2026-06-21)."
  echo "Total skill invocations ingested: **${TOTAL_INV:-0}** (window ${WINDOW_DAYS}d, min-inv ${MIN_INV})"
  echo
  echo "## Signal coverage now"
  echo '```'
  printf '%s\n' "${COVERAGE:-<none>}"
  echo '```'
  echo
  echo "## Baseline (2026-06-21, tool-time historical, 1552 invocations)"
  echo "- no_redirect: 48 rows / 14 of 22 skills"
  echo "- tokens: 28 rows"
  echo "- bead_close: 58 rows"
  echo "- error: 1552 rows, all 1.0 (saturated → variance-weighted to ~0)"
  echo
  echo "## Variance-aware leaderboard"
  echo '```'
  printf '%s\n' "${LEADER:-<no qualifying skills>}"
  echo '```'
  echo
  echo "## Read this against the baseline"
  echo "- If source=audit and no_redirect/tokens coverage climbed well above ~3% of invocations,"
  echo "  current-session transcript availability is fixing the gap as predicted."
  echo "- If the high-traffic surfaces (flux-drive/sprint/strategy/work) now hold DISTINCT"
  echo "  no_redirect values (not all 0.20) and separate in the leaderboard → the score is"
  echo "  getting trustworthy; consider unblocking sylveste-7aj8.6 (tune) + 7aj8.7 (canary)."
  echo "- If still sparse → sylveste-7aj8.9 (collector coverage) remains the binding constraint;"
  echo "  note whether transcript-path resolution or token attribution is dropping rows."
} > "$REPORT"

# Best-effort: point the bead at the report (Dolt write; fail-open under cron).
if command -v bd >/dev/null 2>&1; then
  ( cd /home/mk/projects/Sylveste && bd update sylveste-7aj8.9 \
      --append-notes "Auto re-measure ${DATE_UTC}: source=${SRC:-?}, invocations=${TOTAL_INV:-0}. Full report: ${REPORT}" ) >/dev/null 2>&1 || true
fi

echo "wrote ${REPORT}"
