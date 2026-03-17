---
name: interspect-effectiveness
description: Show routing effectiveness metrics — override rate trends, per-agent impact, and actionable recommendations
argument-hint: "[--window=30]"
---

# Interspect Effectiveness

Show the impact of routing changes on review quality.

<effectiveness_args> #$ARGUMENTS </effectiveness_args>

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
```

## Parse Arguments

Extract `--window=N` from `<effectiveness_args>` (default 30). Accept bare number too.

## Generate Report

```bash
WINDOW=${window:-30}
REPORT=$(_interspect_effectiveness_report "$WINDOW")
```

## Display

Present the effectiveness dashboard in this format:

### 1. Header
`Routing Effectiveness — Last ${WINDOW} days`

### 2. Active Overrides
Read from `_interspect_read_routing_overrides`. For each override with `action: "exclude"`:
- Show agent name, how long ago it was applied
- Query canary table for canary status (active/passed/alert)

### 3. Aggregate Metrics
From REPORT JSON:
- `Override rate: X%` with trend vs prior window from `.prior`
- `Dispatches: N across M sessions`
- `Corrections: N`

### 4. Per-Agent Table
From REPORT `.agents` array, cross-referenced with `.prior`:

| Agent | Dispatches | Rate | Trend |
|-------|-----------|------|-------|

Compute trend by comparing current override_rate with prior:
- Rate decreased by >2%: `↓ improving`
- Rate increased by >2%: `↑ declining ⚠`
- Within ±2%: `→ stable`
- No prior data: `— new`

### 5. Recommendations
- Agent with override_rate > 50%: suggest `/interspect:propose`
- Agent with declining trend (rate up >10%): warn and suggest `/interspect:evidence <agent>`
- All stable/improving: "Routing is healthy"
