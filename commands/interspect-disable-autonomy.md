---
name: interspect-disable-autonomy
description: Disable autonomous mode — all overrides require explicit approval
argument-hint: ""
---

# Interspect: Disable Autonomy

Return to propose mode (default). All future routing overrides will require explicit approval via `/interspect:approve`.

Existing overrides are not affected — they remain active with their canary monitoring.

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
```

## Check Current State

```bash
_interspect_load_confidence
CURRENT_STATE="${_INTERSPECT_AUTONOMY:-false}"
```

If already disabled:
> "Propose mode is already active (autonomy disabled). Overrides require explicit approval via `/interspect:approve`."

## Apply

```bash
_interspect_set_autonomy "false"
```

## Report

```
Autonomous mode **disabled**. Returned to propose mode.

- Future routing overrides will require `/interspect:approve`
- Existing overrides and canaries are unaffected
- Run `/interspect:enable-autonomy` to re-enable
```
