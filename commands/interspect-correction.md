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

## Locate and Source the Interspect Library

Needed both for the low-confidence lookup below and for recording evidence:

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

## Finding Lookup and Recording

Use a canonical lowercase `fd-*` agent name. Session identifiers are ASCII and
case-insensitive; review and finding IDs preserve ASCII case and match
exactly (including `P0-1` and ISO timestamp review IDs). Never lowercase them.
Names and identities are
asserted by the caller, not authenticated.

When a correction references a finding, require its exact `findings.json` from
that run. Do not select the newest unrelated file. Python validates the original
JSON, including duplicate keys and boolean types, before extraction. A missing
file, failed lookup, ambiguous match or invalid classification is an error;
never assume `low_confidence=false` after a lookup failure. A correction without
any finding reference retains ordinary evidence behavior only when it is not
flagged low-confidence. Preserve an explicit `low_confidence=true` from the
correction input in `LOW_CONFIDENCE`; a flagged correction without a valid
finding reference is refused, never retried without that flag.

```bash
_interspect_ensure_db || exit 1
# Check the sanitizer's status before constructing another command substitution.
SANITIZED_DESCRIPTION=$(_interspect_sanitize "$DESCRIPTION") || {
    echo "Correction not recorded: description rejected by sanitizer." >&2
    exit 1
}
# FINDING_ID is empty for an ordinary correction without a finding reference.
REQUEST=$(python3 - "$SANITIZED_DESCRIPTION" "${FINDING_ID:-}" "${FINDINGS_JSON:-}" "${LOW_CONFIDENCE:-false}" <<'PY'
import json, sys
print(json.dumps(dict(description=sys.argv[1], finding_id=sys.argv[2],
                     findings_path=sys.argv[3], low_confidence=json.loads(sys.argv[4]))))
PY
) || exit 1
CONTEXT=$(printf '%s' "$REQUEST" | python3 "$_INTERSPECT_GATE_MODULE" correction-context) || {
    echo "Correction not recorded: finding lookup or identity validation failed." >&2
    exit 1
}
_interspect_insert_evidence \
    "${CLAUDE_SESSION_ID:-}" "$AGENT_NAME" override "$OVERRIDE_REASON" \
    "$CONTEXT" interspect-correction || {
    echo "Correction not recorded: validation, migration or database write failed." >&2
    exit 1
}
```

Low-confidence rows use a SQL JSON-array key containing version, scope, scope ID
and finding ID. Colons in individual components cannot collide. Matching also
requires canonical source and reason, plus distinct canonical session IDs.
Diagnostic context is redacted separately and is never used to lift a gate.
Known low-confidence rows with invalid identities remain held with version zero
and no usable key; recording such a diagnostic does not corroborate anything.

A missing synthesis timestamp selects explicit session scope. A present invalid
timestamp is rejected. The original session is the scope ID; `unscoped:` has no
special meaning and is ordinary review text. Session-scoped rows cannot lift
automatically. To confirm one explicitly:

```bash
if AFFECTED=$(_interspect_corroborate_evidence \
    "$AGENT_NAME" "$ORIGINAL_SESSION" "$FINDING_ID" "$OTHER_SESSION" \
    "$OVERRIDE_REASON" session); then
    echo "Corroborated rows: $AFFECTED"  # zero is a successful no-op
else
    echo "Corroboration rejected or database unavailable; no success recorded." >&2
    exit 1
fi
```

Review-scoped confirmation uses the review ID and omits the final `session`
argument (or passes `review`). Invalid requests return 1 without stdout;
database failures return 2. Never hide either failure with `|| true`.

## Kernel Dual-Write (best-effort)

Only after the local write above succeeds, also write to the Intercore kernel's `interspect_events` table so corrections are visible to kernel-side consumers:

```bash
# Optional kernel replication after local success; never replicate a rejected request.
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
