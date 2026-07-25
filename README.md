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
/interspect:interspect-status       # Overview — sessions, evidence, canaries
/interspect:interspect-evidence     # Detailed agent evidence view

# Corrections
/interspect:interspect-correction   # Record a manual correction event

# Routing overrides
/interspect:interspect-propose      # Propose routing override from ready patterns
/interspect:interspect-override     # Apply override directly
/interspect:interspect-revert       # Revert override or disable overlays
/interspect:interspect-approve      # Approve pending modification

# Diagnostics
/interspect:interspect-health       # Signal diagnostics
/interspect:interspect-enable-autonomy   # Enable autonomous mode
/interspect:interspect-disable-autonomy  # Disable autonomous mode
/interspect:interspect-unblock      # Unblock stalled modification

# Skill calibration (add --source-kind=skill to most commands)
/interspect:interspect-tune --source-kind=skill <plugin>:<skill>   # Generate a skill overlay
/interspect:interspect-status --source-kind=skill                  # Skill calibration view
/interspect:interspect-health --source-kind=skill                  # Skill signal coverage
```

## How It Works

Three hooks collect evidence passively:
- **SessionStart** — records session, consumes kernel events, checks canary alerts
- **PostToolUse** — records evidence when Task tool is used
- **Stop** — closes session record

Evidence accumulates in SQLite (`.clavain/interspect/interspect.db`). When patterns reach counting-rule thresholds, routing overrides can be proposed and applied. Applied overrides enter a canary period (20 uses over 14 days, 20% regression threshold).

### SessionEnd routing calibration

Clavain calls one authoritative writer after evidence collection:

```bash
bash scripts/write-routing-calibration.sh
```

The exit contract is `0` when the routing artifact was updated, `2` for a
valid no-op with no scoreable evidence, and `1` for a hard failure. The general
`_interspect_auto_calibrate` helper deliberately does not write the routing
artifact, preventing duplicate writes in one SessionEnd sequence.

## Skill Calibration

The same closed loop also tunes **skills**, not just agents. Every Claude Code
`Skill` invocation captured in `~/.claude/audit.log` is drained into the evidence
store (`source_kind='skill'`), scored across four signals (`tokens`, `error`,
`no_redirect`, `bead_close`) against per-skill goal weights, and — when a skill's
description triggers too eagerly or too rarely — turned into an overlay at
`~/.claude/skill-overlays/<plugin>:<skill>.md` that the skill loader merges over
the source SKILL.md. Safe-list patches (`tighten_description`, `when_to_use_add`)
auto-apply under autonomy with canary monitoring; body rewrites and availability
changes are always propose-only. Interspect reads only `~/.claude/audit.log` for
completed Claude skill invocations; `tool-time` keeps its own `events.jsonl`
untouched.

When Intermesh is installed, the Stop hook also drains
`~/.local/state/intermesh/routes.jsonl`. Each `intermesh.route.v1` candidate is
stored as `skill_route` decision evidence with its `route_id`, but no success
signal is created until an actual outcome is observed. Intermesh never writes
the Interspect database or calibration artifacts.

## Architecture

- **12 commands** — analysis, override management, canary monitoring, diagnostics
- **3 hooks** — passive evidence collection (SessionStart, PostToolUse, Stop)
- **1 core library** — `hooks/lib-interspect.sh` (sourced by all commands and hooks)
- **SQLite storage** — per-project evidence database
- **Routing overrides** — `.claude/routing-overrides.json` (cross-repo contract)

## License

MIT
