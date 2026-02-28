# interspect

Agent performance profiler and routing optimizer for Claude Code. Collects evidence about flux-drive agent accuracy, proposes routing overrides for underperforming agents, and monitors canary periods. Clavain companion plugin.

## Installation

```bash
claude plugin add interspect@interagency-marketplace
claude plugin update interspect@interagency-marketplace
```

## Usage

```bash
# Core workflow
/interspect              # Analyze evidence, detect patterns, report readiness
/interspect:status       # Overview — sessions, evidence, canaries
/interspect:evidence     # Detailed agent evidence view

# Corrections
/interspect:correction   # Record a manual correction event

# Routing overrides
/interspect:propose      # Propose routing override from ready patterns
/interspect:override     # Apply override directly
/interspect:revert       # Revert override or disable overlays
/interspect:approve      # Approve pending modification

# Diagnostics
/interspect:health       # Signal diagnostics
/interspect:enable-autonomy   # Enable autonomous mode
/interspect:disable-autonomy  # Disable autonomous mode
/interspect:unblock      # Unblock stalled modification
```

## How It Works

Three hooks collect evidence passively:
- **SessionStart** — records session, consumes kernel events, checks canary alerts
- **PostToolUse** — records evidence when Task tool is used
- **Stop** — closes session record

Evidence accumulates in SQLite (`.clavain/interspect/interspect.db`). When patterns reach counting-rule thresholds, routing overrides can be proposed and applied. Applied overrides enter a canary period (20 uses over 14 days, 20% regression threshold).

## Architecture

- **12 commands** — analysis, override management, canary monitoring, diagnostics
- **3 hooks** — passive evidence collection (SessionStart, PostToolUse, Stop)
- **1 core library** — `hooks/lib-interspect.sh` (sourced by all commands and hooks)
- **SQLite storage** — per-project evidence database
- **Routing overrides** — `.claude/routing-overrides.json` (cross-repo contract)

## License

MIT
