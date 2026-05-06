---
name: interspect-tune
description: Generate a prompt tuning overlay for an agent, or a CLAUDE.md remediation for a tool source, from accumulated correction/pattern evidence
argument-hint: "<agent-name> | tool:<tool-source>"
---

# Interspect Tune

Generate a tuning overlay (agent) or CLAUDE.md remediation (tool) from accumulated evidence.

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

Extract argument from `<tune_target>`. If empty, show usage:
```
Usage: /interspect:tune <agent-name>           # generate agent prompt overlay
       /interspect:tune tool:<tool-source>     # generate CLAUDE.md remediation
```

If the argument starts with `tool:`, set `KIND=tool` and strip the prefix into `TARGET`. Otherwise `KIND=agent`, `TARGET=<argument>`.

## Validate

### Agent mode (KIND=agent)

Verify agent has correction evidence:
```bash
CORRECTION_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE (source = '$TARGET' OR source LIKE '%$TARGET') AND source_kind = 'agent' AND event = 'override';" 2>/dev/null || echo "0")
```

If 0: "No corrections found for agent $TARGET. Run `/interspect:correction $TARGET` to record evidence first."

Check if overlay already exists at `.clavain/interspect/overlays/$TARGET/tuning.md`. If so, ask: "Overlay already exists. Regenerate?"

### Tool mode (KIND=tool)

Verify tool source has pattern evidence:
```bash
PATTERN_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source = '$TARGET' AND source_kind = 'tool';" 2>/dev/null || echo "0")
```

If 0: "No tool-pattern evidence found for $TARGET. tool-time's bridge populates this on SessionEnd — run a session with the tool first, or check `~/.claude/tool-time/stats.json` for current patterns."

## Generate

Both modes call the same dispatcher:
```bash
CONTENT=$(_interspect_generate_overlay "$TARGET" "$KIND")
```

If `_interspect_generate_overlay` returns non-zero: report the error and stop.

## Preview and Confirm

### Agent mode

Show the generated overlay content via AskUserQuestion:
- "Apply this tuning overlay" → write file + create canary
- "Edit content first" → let user modify, then write
- "Cancel" → no changes

### Tool mode

Tool-mode output is a CLAUDE.md *patch proposal*. Show the generated content with a clear header:

```
═══ CLAUDE.md remediation for tool: $TARGET ═══
<content>
══════════════════════════════════════════════
```

Offer the user via AskUserQuestion:
- "Apply" → call `_interspect_write_tool_remediation` (interspect-internal record + canary), then optionally use Edit tool to add the rules to user's CLAUDE.md
- "Copy to clipboard" → emit content (stdout / clipboard tool)
- "Cancel" → no changes

Tool mode does NOT write to `.clavain/interspect/overlays/`. Records live at `.clavain/interspect/tool-remediations/<source>/<overlay_id>.md` (separate namespace; agent prompt injection won't pick them up).

## Write

### Agent mode

```bash
_interspect_write_overlay "$TARGET" "tuning" "$CONTENT" "$DB"
```

Creates `.clavain/interspect/overlays/$TARGET/tuning.md`, sets `active: true`, checks token budget (500), creates canary record, git commits.

### Tool mode

```bash
OVERLAY_ID="tuning"   # generate a unique slug if user already has an active record
_interspect_write_tool_remediation "$TARGET" "$OVERLAY_ID" "$CONTENT"
```

Atomically:
- Snapshots baseline pattern counts (JSON: event_type → count) via `_interspect_compute_tool_baseline`
- Writes `.clavain/interspect/tool-remediations/$TARGET/$OVERLAY_ID.md` with `active: true` + baseline frontmatter
- Inserts canary row (status='active', 14-day window, `baseline_pattern_counts` populated)
- Inserts `modifications` row with `mod_type='tool_remediation'`
- Git commits

After the interspect-internal record lands, optionally use the Edit tool to add the suggested rules to the user's CLAUDE.md under a `## Tool Usage` section (separate user-confirmed step).

## Summary

### Agent mode

Report:
- Overlay path
- Token estimate
- Canary status (20 uses / 14 days)
- Next: "Use `/interspect:status` to monitor canary, `/interspect:revert $TARGET` to disable"

### Tool mode

Report:
- Tool remediation path: `.clavain/interspect/tool-remediations/$TARGET/$OVERLAY_ID.md`
- Baseline snapshot (event types + counts at apply time)
- Canary status (14-day window; recurrence visible in `/interspect:status`)
- Next: "Use `/interspect:revert tool:$TARGET` to disable"
