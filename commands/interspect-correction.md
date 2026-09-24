---
name: interspect-correction
description: Record that an agent got something wrong — high-quality manual evidence for Interspect
argument-hint: "[agent-name] [description of what was wrong]"
---

# Interspect Correction

Record an explicit correction signal for an agent. This is the primary evidence collection mechanism for Interspect — high-quality human signals about agent accuracy.

<correction_input> #$ARGUMENTS </correction_input>

## Parse Arguments

If arguments provided, parse: first word = agent name, rest = description.

If no arguments (or incomplete), ask the user:

1. **Which agent?** — the agent or skill that was wrong (e.g., `fd-safety`, `fd-correctness`, `fd-architecture`)
2. **What happened?** — brief description of the incorrect behavior
3. **Override reason** — one of:
   - `agent_wrong` — the finding/recommendation was incorrect
   - `deprioritized` — correct but not worth acting on right now
   - `already_fixed` — correct but stale (issue was already addressed)

Default to `agent_wrong` if the user doesn't specify.

## Low-Confidence Finding Lookup (optional, best-effort)

If the description references a flux-drive finding ID (e.g. `P0-3`) and a `findings.json` from that run is known (most recent `findings.json` under the flux-drive output dir, if the caller has one in scope), check whether the finding carries the severity-tier confidence gate (`docs/spec/core/synthesis.md` Step 4a in interflux):

```bash
FINDING_ID=""   # e.g. "P0-3", parsed from the description if present
LOW_CONFIDENCE="false"
if [[ -n "$FINDING_ID" && -f "$FINDINGS_JSON" ]] && command -v jq &>/dev/null; then
    LOW_CONFIDENCE=$(jq -r --arg id "$FINDING_ID" \
        '(.findings[] | select(.id == $id) | .low_confidence) // false' \
        "$FINDINGS_JSON" 2>/dev/null) || LOW_CONFIDENCE="false"
fi
```

This is best-effort — if no finding ID is identifiable or no `findings.json` is in scope, `LOW_CONFIDENCE` stays `"false"` and the correction is recorded exactly as before. Never block on this lookup.

## Record Evidence

Locate and source the Interspect library:

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
```

Then initialize and insert:

```bash
_interspect_ensure_db

# Build context JSON (use jq for proper escaping). low_confidence/finding_id
# are the severity-tier confidence gate fields (Sylveste-06i.4) — when set,
# lib-interspect.sh quarantines this evidence until a second independent
# correction corroborates the same finding_id.
CONTEXT=$(jq -n \
    --arg desc "$DESCRIPTION" \
    --arg reason "$OVERRIDE_REASON" \
    --argjson low_confidence "${LOW_CONFIDENCE:-false}" \
    --arg finding_id "${FINDING_ID:-}" \
    '{description: $desc, override_reason: $reason} +
     (if $low_confidence then {low_confidence: true} else {} end) +
     (if $finding_id != "" then {finding_id: $finding_id} else {} end)')

_interspect_insert_evidence \
    "$CLAUDE_SESSION_ID" \
    "$AGENT_NAME" \
    "override" \
    "$OVERRIDE_REASON" \
    "$CONTEXT" \
    "interspect-correction"
```

## Kernel Dual-Write (best-effort)

After the legacy write succeeds, also write to the Intercore kernel's `interspect_events` table so corrections are visible to kernel-side consumers:

```bash
# Dual-write to intercore kernel (fail-open)
if command -v ic &>/dev/null; then
    PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
    RUN_ID=$(ic run current --project="$PROJECT_ROOT" 2>/dev/null) || RUN_ID=""
    ic interspect record \
        --agent="$AGENT_NAME" \
        --type="correction" \
        --reason="$OVERRIDE_REASON" \
        --context="$CONTEXT" \
        --session="$CLAUDE_SESSION_ID" \
        --project="$PROJECT_ROOT" \
        ${RUN_ID:+--run="$RUN_ID"} \
        2>/dev/null || true
fi
```

## Confirm

After inserting, query the total count for this agent:

```bash
DB=$(_interspect_db_path)
COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM evidence WHERE source = '${AGENT_NAME//\'/\'\'}';")
```

Report to the user:

```
Recorded correction for **{agent_name}**: {description}
Reason: {override_reason}
Total evidence for {agent_name}: {count} events

Run /interspect:interspect-evidence {agent_name} to see all evidence for this agent.
```
