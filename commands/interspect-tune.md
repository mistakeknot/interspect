---
name: interspect-tune
description: Generate a prompt tuning overlay for an agent, a CLAUDE.md remediation for a tool source, or a skill overlay for a skill, from accumulated evidence
argument-hint: "<agent-name> | tool:<tool-source> | --source-kind=skill <plugin>:<skill> [--action=<a>] [--dry-run] [--apply]"
---

# Interspect Tune

Generate a tuning artifact from accumulated evidence. Three modes:
- **agent** (default): prompt overlay from override/correction evidence
- **tool** (`tool:<src>`): CLAUDE.md remediation from tool-pattern evidence
- **skill** (`--source-kind=skill <name>` or `skill:<name>`): skill overlay from
  per-skill signal evidence (`skill_signals` + `skill_goals`)

<tune_target> #$ARGUMENTS </tune_target>

## Locate Library

```bash
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

## Parse Target

Extract the argument from `<tune_target>`. If empty, show usage:
```
Usage: /interspect:tune <agent-name>                         # agent prompt overlay
       /interspect:tune tool:<tool-source>                   # CLAUDE.md remediation
       /interspect:tune --source-kind=skill <plugin>:<skill> # skill overlay
              [--action=tighten_description|when_to_use_add|skill_md_body_rewrite|availability]
              [--dry-run] [--apply]
```

Determine `KIND`:
- If args contain `--source-kind=skill`, or the argument starts with `skill:`, set `KIND=skill` and strip into `TARGET` (skill name). Collect optional `--action=<a>`, `--dry-run`, `--apply` flags.
- Else if the argument starts with `tool:`, set `KIND=tool`, strip prefix into `TARGET`.
- Else `KIND=agent`, `TARGET=<argument>`.

## Validate

### Agent mode (KIND=agent)

```bash
CORRECTION_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE (source = '$TARGET' OR source LIKE '%$TARGET') AND source_kind = 'agent' AND event = 'override';" 2>/dev/null || echo "0")
```
If 0: "No corrections found for agent $TARGET. Run `/interspect:correction $TARGET` first."
Check `.clavain/interspect/overlays/$TARGET/tuning.md`; if it exists, ask "Regenerate?".

### Tool mode (KIND=tool)

```bash
PATTERN_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source = '$TARGET' AND source_kind = 'tool';" 2>/dev/null || echo "0")
```
If 0: "No tool-pattern evidence found for $TARGET."

### Skill mode (KIND=skill)

```bash
_interspect_validate_skill_name "$TARGET" || { echo "Invalid skill name"; exit 1; }
SIGNAL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_signals WHERE skill_name = '$TARGET';" 2>/dev/null || echo "0")
INVS=$(_interspect_skill_invocation_count "$TARGET")
HAS_GOALS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM skill_goals WHERE skill_name = '$TARGET';" 2>/dev/null || echo "0")
```
- If `SIGNAL_COUNT` is 0: "No skill-signal evidence for $TARGET. Run the collectors (`/interspect:calibrate` runs them) after the skill has been invoked." → stop.
- If `HAS_GOALS` is 0: warn "No goal weights for $TARGET — run `scripts/infer-skill-goals.py --skill $TARGET` for a precise score. Proceeding with signal-only action selection."
- Existing overlay at `~/.claude/skill-overlays/$TARGET.md` → ask "Revert and regenerate?" (revert via `_interspect_disable_skill_overlay`).

## Generate

### Agent / Tool

```bash
CONTENT=$(_interspect_generate_overlay "$TARGET" "$KIND")
```

### Skill

Resolve the action (explicit `--action` wins; otherwise auto-select from the dominant signal deficit), then generate the overlay body:
```bash
ACTION="${EXPLICIT_ACTION:-$(_interspect_select_skill_action "$TARGET")}"
CONTENT=$(_interspect_generate_skill_overlay "$TARGET" "$ACTION") || { echo "$CONTENT"; exit 1; }
```
Action mapping (informational): `no_redirect` deficit → `tighten_description`; `tokens` deficit → `when_to_use_add`; `error` deficit → `skill_md_body_rewrite` (propose-only); healthy signals + low utilization → `availability` (propose-only).

## Dry run

If `--dry-run` was passed (any mode), print the generated `CONTENT` with a header and **exit without writing anything**:
```
═══ DRY RUN — proposed <KIND> patch for <TARGET> (<ACTION>) ═══
<CONTENT>
══════════════════════════════════════════════════════════════
```

## Preview and Confirm

### Agent / Tool

(unchanged — see agent overlay / tool remediation flows below)

### Skill

Show `CONTENT` and the resolved `ACTION`. Decide auto-apply vs propose:
```bash
if _interspect_skill_should_auto_apply "$TARGET" "$ACTION"; then DECISION=auto; else DECISION=propose; fi
```
Offer via AskUserQuestion:
- "Apply now" → forces the write path (records canary). Honors the per-action safe-list: body rewrites / availability remain propose-only even here.
- "Propose only" → routing-overrides entry, no overlay file.
- "Cancel" → no changes.

If invoked non-interactively with `--apply`, use the safe-list decision directly.

## Write

### Agent mode

```bash
_interspect_write_overlay "$TARGET" "tuning" "$CONTENT" "$DB"
```

### Tool mode

```bash
_interspect_write_tool_remediation "$TARGET" "tuning" "$CONTENT"
```

### Skill mode

Branch on the safe-list decision (and the user's choice above):
```bash
if [[ "$DECISION" == "auto" ]] && _interspect_skill_action_is_auto "$ACTION"; then
    _interspect_write_skill_overlay "$TARGET" "$ACTION" "$CONTENT" "$EVIDENCE_IDS"
else
    _interspect_propose_skill_tune "$TARGET" "$ACTION" "$CONTENT" "$EVIDENCE_IDS"
fi
```
- **Auto-apply** writes `~/.claude/skill-overlays/$TARGET.md` (the skill loader merges it over the source SKILL.md), records a `modifications` row (`mod_type='skill_tune'`), a `routing-overrides.json` entry (`kind='skill_tune'`, `state='active'`), and arms the canary (`skill_canary_samples`).
- **Propose** records the `modifications` row (`status='proposed'`) and the override entry (`state='proposed'`) only — no overlay file is written, so the loader does not pick it up.

## Summary

### Skill mode

Report:
- Action chosen and whether it auto-applied or was proposed
- Overlay path (`~/.claude/skill-overlays/$TARGET.md`) when applied; `modification_id`
- Canary window (20 invocations / 14 days; per-signal deltas in `skill_canary_samples`)
- Next: "`/interspect:status --source-kind=skill` to monitor, `/interspect:revert --source-kind=skill $TARGET` to undo"

### Agent mode

Report: overlay path, token estimate, canary status (20 uses / 14 days), next steps.

### Tool mode

Report: remediation path, baseline snapshot, canary status (14-day window), next steps.
