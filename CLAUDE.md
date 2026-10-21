# interspect

> See `AGENTS.md` for full development guide.

## Overview

Agent performance profiler and routing optimizer — 0 skills, 18 commands, 0 agents, 3 hooks, 0 MCP servers. Companion plugin for Clavain. Collects evidence about flux-drive agent accuracy, proposes routing overrides, and monitors canary periods.

## Quick Commands

```bash
# Test locally
claude --plugin-dir /home/mk/projects/Sylveste/interverse/interspect

# Validate structure
ls commands/*.md | wc -l              # Should be 18
bash -n hooks/lib-interspect.sh       # Syntax check
python3 -c "import json; json.load(open('.claude-plugin/plugin.json'))"  # Manifest check
python3 -c "import json; json.load(open('hooks/hooks.json'))"           # Hooks JSON check
```

## Design Decisions (Do Not Re-Ask)

### Low-confidence evidence gate

Gate operations require Python 3.12+ and SQLite JSON1. Identity is asserted by
callers, not authenticated: two invented session names can still corroborate.
Direct database write access is trusted; triggers enforce row invariants, not
provenance. Merge requires operator acceptance of that boundary.

Raw JSON is validated before shell extraction. Sessions are ASCII lowercase;
review/finding IDs must already be lowercase and match exactly. Canonical
`gate_*` columns carry identities separately from diagnostic fields. Both lift
modes use one parameterized UPDATE; automatic insertion and lift share one
transaction. Missing run IDs use explicit `session` scope, with the original
session as scope ID. `unscoped:` is ordinary review text, never a magic prefix.

Migration is transactional, audits prior low-confidence rows, re-quarantines
legacy lifts and never lifts by itself. Invalid identities or mismatched legacy
keys remain version zero with NULL keys. Review affected overrides listed in
`interspect_gate_override_review`; migration does not revert them. Failed
migration stops gate writes. Old plugin copies cannot lift migrated rows.
Gate statistics include flagged, corroborated, held and invalid/legacy-held counts.

Before rollout, pause writers and rehearse on a consistent database copy outside
git. Disable corroboration for rollback; with writers still paused, drop only
the `interspect_gate_insert`/`interspect_gate_update` triggers and
`interspect_gate_schema` marker, re-quarantine every low-confidence row and
clear its corroboration timestamp/actor/mode in one transaction. Retain audit
tables and do not resume old gate code or restore legacy lifts. A subsequent
forward repair requires reviewed reconciliation, not automatic rerun.

Verification: `bash tests/shell/test_low_confidence_gate.sh` and
`cd tests && uv run pytest -q structural/test_gate_*.py`, followed by the full
structural and shell suites. The shared structural fixture package must be on
`PYTHONPATH` when testing a standalone clone.

- Namespace: `interspect:` (companion to Clavain)
- Evidence stored in `.clavain/interspect/interspect.db` (SQLite)
- Routing overrides written to `.claude/routing-overrides.json` (cross-repo contract)
- Canary monitoring: 20-use window over 14 days, 20% alert threshold
- Protected paths enforced via `.clavain/interspect/protected-paths.json`
- Discovery via clavain lib.sh `_discover_interspect_plugin()` looking for `hooks/lib-interspect.sh`
- **Skill calibration (sylveste-7aj8):** the routing loop also covers skills via `source_kind='skill'`. Evidence drains from `~/.claude/audit.log` only (tool-time keeps its own `events.jsonl` — no shared storage). Skill overlays live in `~/.claude/skill-overlays/<plugin>:<skill>.md`; per-action autonomy safe-list in `.clavain/interspect/skill-autonomy-policy.json` (`tighten_description`/`when_to_use_add` auto-apply; body rewrites/availability propose-only). Skill canaries use the `skill_canary_samples` table, NOT the agent-shaped `canary_samples`.
- **Intermesh boundary:** optional `intermesh.route.v1` receipts are decision-only skill evidence. Never infer success from selection; retain `route_id` for outcome attachment, and keep all learned weights, overlays, and canaries owned by Interspect.
