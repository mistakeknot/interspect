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

Tool-mode output is a CLAUDE.md *patch proposal*, not an auto-applied overlay. Show the generated content with a clear header:

```
═══ CLAUDE.md remediation for tool: $TARGET ═══
<content>
══════════════════════════════════════════════
```

Offer the user via AskUserQuestion:
- "Copy to clipboard" → emit content for clipboard tool (or just stdout)
- "Apply to CLAUDE.md" → use Edit tool to insert under a "## Tool Usage" section (asks user for confirmation per Edit semantics)
- "Cancel" → no changes

Tool mode does NOT write to `.clavain/interspect/overlays/`. The agent overlay infrastructure is reserved for prompt-tuning of named agents; tool remediations belong in CLAUDE.md where they can shape *all* tool calls.

## Write (Agent mode only)

```bash
_interspect_write_overlay "$TARGET" "tuning" "$CONTENT" "$DB"
```

This automatically:
- Creates `.clavain/interspect/overlays/$TARGET/tuning.md`
- Sets `active: true` in frontmatter
- Checks token budget (500 max)
- Creates canary record
- Git commits

## Summary

### Agent mode

Report:
- Overlay path
- Token estimate
- Canary status (20 uses / 14 days)
- Next: "Use `/interspect:status` to monitor canary, `/interspect:revert $TARGET --overlay` to disable"

### Tool mode

Report:
- Whether the user copied / applied / cancelled
- "Re-run `/interspect:tune tool:$TARGET` to refresh as new patterns accumulate"
- Note: tool-mode tuning currently produces a preview only. Canary integration for tool remediations is planned (sylveste-sfhq.3 follow-up).
