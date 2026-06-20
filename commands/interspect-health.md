---
name: interspect-health
description: Signal collection diagnostics — check evidence channels, dark sessions, DB health
---

# Interspect Health

Diagnostics for Interspect's signal collection system. Checks each evidence channel and reports overall health.

## Locate Library

```bash
# Prefer own copy, fall back to Clavain cache, then monorepo dev path
INTERSPECT_LIB="${CLAUDE_PLUGIN_ROOT}/hooks/lib-interspect.sh"
if [[ ! -f "$INTERSPECT_LIB" ]]; then
    INTERSPECT_LIB=$(find ~/.claude/plugins/cache -path '*/interspect/*/hooks/lib-interspect.sh' -o -path '*/clavain/*/hooks/lib-interspect.sh' 2>/dev/null | head -1)
fi
if [[ -z "$INTERSPECT_LIB" || ! -f "$INTERSPECT_LIB" ]]; then
    echo "Error: Could not locate hooks/lib-interspect.sh" >&2
    exit 1
fi
source "$INTERSPECT_LIB"
_interspect_ensure_db
DB=$(_interspect_db_path)
```

## Channel Diagnostics

Check each evidence collection channel for the last 7 days:

### Session Tracking

```bash
SESSION_COUNT_7D=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sessions WHERE start_ts > datetime('now', '-7 days');")
DARK_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sessions WHERE end_ts IS NULL AND start_ts < datetime('now', '-24 hours');")
TOTAL_SESSIONS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sessions;")
LAST_SESSION=$(sqlite3 "$DB" "SELECT MAX(start_ts) FROM sessions;")
```

Status logic:
- **OK**: >= 1 session in last 7 days with both start and end
- **WARN**: sessions exist but all in last 7 days are dark (no end_ts)
- **INACTIVE**: no sessions in last 7 days

### Evidence Hook (agent dispatch)

```bash
DISPATCH_7D=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE event = 'agent_dispatch' AND ts > datetime('now', '-7 days');")
LAST_DISPATCH=$(sqlite3 "$DB" "SELECT MAX(ts) FROM evidence WHERE event = 'agent_dispatch';")
```

Status logic:
- **OK**: >= 1 dispatch event in last 7 days
- **WARN**: dispatch events exist but none in last 7 days
- **INACTIVE**: no dispatch events ever recorded

### Correction Signals

```bash
CORRECTION_7D=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE event = 'override' AND ts > datetime('now', '-7 days');")
CORRECTION_TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE event = 'override';")
LAST_CORRECTION=$(sqlite3 "$DB" "SELECT MAX(ts) FROM evidence WHERE event = 'override';")
```

Status logic:
- **OK**: >= 1 correction in last 7 days
- **WARN**: corrections exist but none in last 7 days
- **INACTIVE**: no corrections ever recorded

### DB Health

```bash
DB_SIZE=$(stat -c %s "$DB" 2>/dev/null || stat -f %z "$DB" 2>/dev/null || echo "unknown")
EVIDENCE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence;")
WAL_MODE=$(sqlite3 "$DB" "PRAGMA journal_mode;")
INTEGRITY=$(sqlite3 "$DB" "PRAGMA integrity_check;" | head -1)
```

## Report Format

```
## Interspect Health Report

### Signal Channels
| Channel | Status | Last 7d | Total | Last Event |
|---------|--------|---------|-------|------------|
| Session tracking | {OK/WARN/INACTIVE} | {count} | {total} | {last} |
| Evidence hook | {OK/WARN/INACTIVE} | {count} | {total} | {last} |
| Correction signals | {OK/WARN/INACTIVE} | {count} | {total} | {last} |

### Dark Sessions
{dark_count} sessions with no recorded end (started > 24h ago).
{if dark > 0: "These represent crashed or killed sessions. Evidence from these sessions is still counted."}

### Database
- Size: {db_size} bytes
- Total evidence: {evidence_count} events
- Journal mode: {wal_mode}
- Integrity: {integrity}

### Recommendations
{Generate recommendations based on findings:
- If INACTIVE channels: "Run /interspect:correction to generate correction signals"
- If many dark sessions: "Session end hook may not be firing — check hooks.json registration"
- If no dispatch events: "Task tool PostToolUse matcher may not be supported — this is expected and acceptable. /interspect:correction is the primary evidence source."
- If DB integrity fails: "Database corruption detected — consider deleting and re-creating"
- If all OK: "All channels healthy. Continue using /interspect:correction to build evidence."}
```

## Skill Signal Coverage (`--source-kind=skill`)

When `--source-kind=skill` is passed, diagnose the skill-calibration channels
instead of (or in addition to) the agent channels.

```bash
# Skill evidence ingestion
SKILL_EV_7D=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source_kind='skill' AND ts > datetime('now','-7 days');")
SKILL_WATERMARK=$(sqlite3 "$DB" "SELECT MAX(last_skill_audit_ts) FROM sessions;")

# Per-signal coverage (which collectors are populating skill_signals)
SIGNAL_COVERAGE=$(sqlite3 -separator ' | ' "$DB" "
  SELECT signal_kind, COUNT(*) AS rows, COUNT(DISTINCT skill_name) AS skills,
         MAX(observed_at) AS last
  FROM skill_signals
  GROUP BY signal_kind;")

# Dark skills: invoked (evidence) but no signals collected yet
DARK_SKILLS=$(sqlite3 "$DB" "
  SELECT COUNT(DISTINCT e.source) FROM evidence e
  WHERE e.source_kind='skill'
    AND NOT EXISTS (SELECT 1 FROM skill_signals s WHERE s.skill_name = e.source);")

# Goal classification coverage
GOALS_CLASSIFIED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_goals;")
GOALS_OBSERVED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_goals WHERE classified_from='observed';")
```

Report:
```
### Skill Signal Channels
| Signal | Rows | Skills | Last Event | Status |
|--------|-----:|-------:|------------|--------|
| tokens | … | … | … | {OK if any in 7d} |
| error | … | … | … | … |
| no_redirect | … | … | … | … |
| bead_close | … | … | … | … |

- Skill evidence (7d): {SKILL_EV_7D}; audit watermark: {SKILL_WATERMARK}
- Dark skills (invoked, no signals yet): {DARK_SKILLS}
- Goals classified: {GOALS_CLASSIFIED} ({GOALS_OBSERVED} refined from observed signal mix)
```

Recommendations:
- A signal with 0 rows: that collector isn't running — check `commands/calibrate.md` wiring and `scripts/signals/collect_<signal>.py`.
- `tokens` empty but others present: the CASS analytics join (`os/Alwe`) is unavailable — expected until CASS data lands.
- Many dark skills: run `/interspect:calibrate` (drives the collectors) after invocations accumulate.

## Edge Cases

- **No database:** Report "Interspect database not initialized. It will be created automatically on next session start or when running `/interspect:correction`."
- **All channels inactive:** This is normal for a fresh install. Guide the user to generate first evidence.
- **No skill signals:** Skill calibration needs the collectors to have run at least once after a tracked skill was invoked; this is normal pre-adoption.
