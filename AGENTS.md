# interspect — Development Guide

## Canonical References
1. [`PHILOSOPHY.md`](./PHILOSOPHY.md) — direction for ideation and planning decisions.
2. `CLAUDE.md` — implementation details, architecture, testing, and release workflow.

> Cross-AI documentation for interspect. Works with Claude Code, Codex CLI, and other AI coding tools.

## Quick Reference

| Item | Value |
|------|-------|
| Repo | `https://github.com/mistakeknot/interspect` |
| Namespace | `interspect:` |
| Manifest | `.claude-plugin/plugin.json` |
| Components | 0 skills, 12 commands, 0 agents, 3 hooks (SessionStart + PostToolUse + Stop), 1 script |
| License | MIT |

### Release workflow
```bash
scripts/bump-version.sh <version>   # bump, commit, push, publish
```

## Overview

**interspect** is an agent performance profiler and routing optimizer — a Clavain companion plugin. Collects evidence about flux-drive agent accuracy, proposes routing overrides for underperforming agents, and monitors canary periods.

**Problem:** Flux-drive agents have variable quality per domain. Bad routing wastes tokens and produces poor reviews. No evidence-based way to tune agent selection.

**Solution:** Three hooks collect evidence passively. 12 commands provide analysis, override management, and canary monitoring. Evidence stored in SQLite; routing overrides written to `.claude/routing-overrides.json`.

**Plugin Type:** Claude Code command + hook plugin (Clavain companion)
**Current Version:** 0.1.5

## Architecture

```
interspect/
├── .claude-plugin/
│   └── plugin.json               # Metadata only (commands/hooks via convention)
├── commands/
│   ├── interspect.md             # Main analysis — detect patterns, classify, report
│   ├── interspect-status.md      # Overview — sessions, evidence, canaries
│   ├── interspect-evidence.md    # Detailed agent evidence view
│   ├── interspect-correction.md  # Record manual correction event
│   ├── interspect-propose.md     # Propose routing override from patterns
│   ├── interspect-override.md    # Apply routing override directly
│   ├── interspect-revert.md      # Revert override or disable overlays
│   ├── interspect-approve.md     # Approve pending modification
│   ├── interspect-health.md      # Signal diagnostics
│   ├── interspect-enable-autonomy.md
│   ├── interspect-disable-autonomy.md
│   └── interspect-unblock.md     # Unblock stalled modification
├── hooks/
│   ├── hooks.json                # SessionStart + PostToolUse(Task) + Stop
│   ├── lib-interspect.sh         # Core library (114KB monolith)
│   ├── interspect-session.sh     # SessionStart: record, consume kernel events, canary check
│   ├── interspect-evidence.sh    # PostToolUse: record evidence on Task tool use
│   └── interspect-session-end.sh # Stop: close session record
├── scripts/
│   └── bump-version.sh
├── docs/
│   └── research/                 # Architecture/quality/correctness/safety reviews
├── CLAUDE.md
├── AGENTS.md                     # This file
├── PHILOSOPHY.md
└── LICENSE
```

## Commands

| Command | Purpose |
|---------|---------|
| `/interspect` | Analyze evidence — detect patterns, classify by counting-rule thresholds |
| `/interspect:status` | Overview — session counts, evidence stats, active canaries |
| `/interspect:evidence` | Detailed agent evidence view |
| `/interspect:correction` | Record a manual correction event |
| `/interspect:propose` | Propose routing override from ready patterns |
| `/interspect:override` | Apply a routing override directly |
| `/interspect:revert` | Revert override or disable overlays |
| `/interspect:approve` | Approve pending modification |
| `/interspect:health` | Signal diagnostics |
| `/interspect:enable-autonomy` | Enable autonomous modification mode |
| `/interspect:disable-autonomy` | Disable autonomous modification mode |
| `/interspect:unblock` | Unblock a stalled modification |

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `interspect-session.sh` | SessionStart | Record session, consume kernel events, check canary alerts |
| `interspect-evidence.sh` | PostToolUse (Task) | Record evidence from Task tool use |
| `interspect-session-end.sh` | Stop | Close session record |

## Core Library

`hooks/lib-interspect.sh` (114KB) is the monolithic core. All commands source it. Discovery path: first checks `~/.claude/plugins/cache/*/clavain/*/hooks/lib-interspect.sh`, then `~/projects/*/os/clavain/hooks/lib-interspect.sh`. Uses path search (not `$CLAUDE_PLUGIN_ROOT`) to enable sourcing from Clavain hooks.

## Canary Monitoring

When a routing override is applied, it enters a canary period:
- **Window:** 20 uses over 14 days
- **Alert threshold:** 20% regression
- SessionStart hook injects `additionalContext` warning if canary alerts exist

## Data Storage

- **Evidence DB:** `.clavain/interspect/interspect.db` (SQLite, per-project). Tables include `evidence` (with `source_kind` ∈ `agent|tool|pattern|skill`), `canary`/`canary_samples` (agent/tool canaries), and the skill-calibration tables `skill_goals`, `skill_signals`, `skill_canary_samples`.
- **Routing overrides:** `.claude/routing-overrides.json` (cross-repo contract). Carries agent `exclude`/`propose` entries plus `kind:"skill_tune"` entries.
- **Skill overlays:** `~/.claude/skill-overlays/<plugin>:<skill>.md` (USER HOME — read by the Claude Code skill loader, not repo-tracked).
- **Skill autonomy policy:** `.clavain/interspect/skill-autonomy-policy.json` (per-action safe-list; defaults baked into `lib-interspect.sh`).
- **Protected paths:** `.clavain/interspect/protected-paths.json`

## Integration Points

| Tool | Relationship |
|------|-------------|
| Clavain | Primary integration — Clavain discovers interspect via `_discover_interspect_plugin()`, sources `lib-interspect.sh` |
| Intercore | Dual-read from `interspect_events` kernel table via `ic interspect query`; kernel events consumed at session start |
| flux-drive agents | The subjects being profiled; routing overrides target flux-drive agent IDs |
| interpulse | interpulse monitors context pressure; interspect monitors routing quality (complementary) |
| tool-time | **Boundary:** tool-time continues to own `~/.claude/tool-time/events.jsonl` for its community-comparison flows. Interspect reads only `~/.claude/audit.log` (the `tool:"Skill"` rows) for skill calibration — it never reads or writes tool-time's event log. The two share no storage. |
| skills (Skill loader) | The subjects of skill calibration; skill overlays at `~/.claude/skill-overlays/` are merged over source SKILL.md by the loader |

## Testing

```bash
cd tests && uv run pytest -q
```

## Decay Policy

Evidence and calibration data (C2) follow intermem's decay model:

| Data type | Grace period | Decay rate | Hysteresis | Action |
|-----------|-------------|------------|------------|--------|
| Evidence records | 90 days | Excluded from analysis after 90d | N/A | Old evidence not counted in pattern detection |
| Canary windows | 14 days | Window expires after 14d or 20 uses | N/A | Auto-evaluated at expiry |
| Routing overrides | None | Permanent until reverted | N/A | Manual revert via `/interspect:revert` |
| Session records | 90 days | Excluded from baseline after 90d | N/A | Old sessions not counted in canary baselines |

**Standard pattern:** Grace period → linear exclusion → no hysteresis needed (evidence is append-only, not demotable). Interspect uses a 90-day rolling window rather than per-entry decay because evidence is statistical — individual records don't go "stale," but old aggregate patterns lose relevance.

## Signed Evidence (moat play — sylveste-ewy3.5.4)

Interspect can emit **HMAC-signed action receipts** for routing-calibration events, turning its evidence trail into portable, third-party-verifiable proof. This is the "every action produces evidence" principle made cryptographic. See `docs/canon/signed-receipts-v1.md` for the receipt schema, canonicalization, and trust model.

- **Opt-in**: set `INTERSPECT_SIGNED_RECEIPTS=1`. Off by default (zero overhead on the evidence hot path).
- **Agent identity**: receipts are signed as `sylveste://agent/interspect#<rotation_epoch>`. The signing key self-provisions on first use under `.clavain/keys/receipts/` (canon §Key handling).
- **Substrate**: `ic receipt emit` (sign + store), `ic receipt verify <id> | --since=<dur>` (verify, exit 0=valid/1=not-found/2=bad-sig/3=bad-schema/4=unknown-key), `ic receipt keygen` (rotate). Receipts live in the **intercore** DB (`action_receipts`, schema v35), not the interspect DB.
- **Wired now**: routing-**override** applications (via `_interspect_emit_receipt` in `_interspect_insert_evidence`, fail-open). The emission is guarded so a signing failure never breaks evidence recording.
- **Not yet wired** (follow-up): proposal and canary-evaluation paths are distinct code paths and are tracked separately. `/interspect:status` surfaces the current interspect signed-receipt count.
- **Prerequisite**: the intercore DB must be migrated to schema v35 (`ic init` or normal migration). Without it, emission fails open (no receipt, no error).

## Known Constraints

- `lib-interspect.sh` is 114KB — monolithic by necessity (all commands need the same DB/routing/canary functions)
- plugin.json has no `commands` or `hooks` keys — they load via convention (commands/ dir, hooks/hooks.json)
- Missing README.md (interspect is the only repo without one)
- Discovery uses path search rather than `$CLAUDE_PLUGIN_ROOT` — required for cross-plugin sourcing from Clavain
