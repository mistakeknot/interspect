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

Gate operations require Python 3.12+ and SQLite JSON1, including the persistent
triggers. Initialization checks JSON1 even on the migration fast path and stops
gate writes with a clear error if it is unavailable. Identity is asserted by
callers, not authenticated: two invented session names can still corroborate.
Direct database write access is trusted; triggers enforce row invariants, not
provenance. Merge requires operator acceptance of that boundary.

Raw JSON is validated before shell extraction. Sessions are ASCII lowercase;
review/finding IDs preserve ASCII case and match exactly, including `P0-1` and
`basename@2026-02-14T08:45:32Z`. Whitespace, controls, NUL and non-ASCII remain
invalid. Canonical
`gate_*` columns carry identities separately from diagnostic fields. Both lift
modes use one parameterized UPDATE; automatic insertion and lift share one
transaction. Missing run IDs use explicit `session` scope, with the original
session as scope ID. `unscoped:` is ordinary review text, never a magic prefix.
Derived `basename@timestamp` review IDs reject basenames containing `@`; the
first `@` is therefore the unique boundary. Colons remain valid in either
component, and explicit review/finding IDs still allow `:` and `@`.

Migration is transactional, audits prior low-confidence rows, re-quarantines
legacy lifts and never lifts by itself. Invalid identities or mismatched legacy
keys remain version zero with NULL keys. Review affected overrides listed in
`interspect_gate_override_review`; migration does not revert them. Failed
migration stops gate writes. Old plugin copies cannot lift migrated rows.
Gate statistics include flagged, corroborated, held and invalid/legacy-held counts.
Base-era rows whose legacy `finding_key` is NULL stay permanently at version
zero; neither lift path can corroborate them. Migration also catches agent rows
whose column says false but whose context says true, or whose context cannot be
strictly parsed. It prints the held-row count and a REVIEW REQUIRED override
count. Audit records and override-review entries are retained on repeat repair.
The v2 marker's `guard_revision` upgrades pre-fix guards transactionally; such
an upgrade re-quarantines evidence and preserves valid canonical v2 identities.
Revision 2 also repairs override reporting from retained audit snapshots. It
prints every previously exposed evidence ID now held, including context-only
flags whose quarantine expired without a corroboration timestamp. Override
evidence references are matched using SQLite INTEGER primary-key affinity, so
`1`, `"1"`, `1.0`, `"01"` and equivalent numeric strings refer to the same row.
Non-reference arrays/objects in an evidence-ID array do not abort reconciliation.
These reports never automatically reverse an override.

Revision 3 permanently holds pre-existing review identities with multiple `@`
characters, including canonical v2 rows and legacy concatenated keys. Earlier
generators could join `(a@b, c)` and `(a, b@c)` into the same ID. Stored rows do
not retain enough provenance to distinguish explicit IDs from generated IDs, so
repair conservatively clears their gate identities and usable keys while keeping
diagnostics and audits. They cannot corroborate a new correction. Repeated repair
also holds this shape. New generation still rejects `@` in basenames.

Revision 4 retires agent canaries whose sample sessions or baseline/monitoring
windows overlap exposed evidence re-quarantined by migration. Matching uses
canonical session aliases and agent cohorts; missing historical window metadata
is handled conservatively. It sets `needs_review`, deletes affected canary
samples, resets usage and clears numeric baselines. Migration lists affected
canary IDs and preserves their previous records/samples and evidence IDs as
`kind: "canary"` entries in `interspect_gate_override_review`. Evaluation returns
`needs_review` without a pass/alert; status ignores even samples retained under
that invalid state. Reconcile the override and establish a fresh canary after
review; never reactivate the old aggregate. Overrides are not automatically
reverted. Path-aware initialization reconciles override reports from retained
audits even if an earlier bridge migrated without the override path. Ordinary
initialization at the current revision remains read-only when no report is due.
`interspect_gate_exposed` retains exposed IDs across later lifts and repairs,
including when the original immutable audit snapshot captured a held row.

Canary matching resolves the backing agent separately from the modification
identity: an overlay canary's compound `agent/overlay` group_id invalidates by the
part before `/` (its baseline is computed from that agent's evidence), and a row
matches by any source recorded for it, including the `gate_source`/`source` kept in
its first audit, because a revision-3 repair clears canonical identities of
ambiguous review IDs. Revision 3 kept no exposure table, so a row its audit shows
as held (never corroborated) is treated as possibly exposed: a later lift followed
by another repair would otherwise leave audit and row both merely held. This
over-invalidates conservatively; operators review the listed canaries. Reverting
an override or overlay also closes its `needs_review` canaries, and
`/interspect:interspect-status` reports them as invalidated (not "collecting").
`calibrate-audit.py` `defect_escape_rate` uses `skill_signal_eligible_sql`, so
signals tied to ineligible, quarantined or missing invocations never count.
`interspect-status`, `-propose`, `-approve` and `-tune` stop with an error when
`_interspect_ensure_db` (including the gate migration) fails, before any decision
read or canary evaluation. Migration reconciles the validated
`FLUX_ROUTING_OVERRIDES_PATH` (default `.claude/routing-overrides.json`) and fails
on an unsafe path. Tool-mode tuning validates `$TARGET` and SQL-escapes it.

Disagreements use `event_confidence()` to honor top-level or nested true flags
regardless of dismissal reason, and reject malformed contexts/non-boolean flags.
Missing top-level classification remains conservatively held. Execution
defects and kernel events also pass raw JSON through the Python bridge, honoring
top-level and nested confidence flags before any shell extraction. Ordinary
execution/kernel events may omit the flag. Flagged rows without a valid fd-agent,
reason and finding tuple remain held at v0 (execution defects have no routing
override reason; kernel telemetry has a `kernel-*` source). No alternate lift
path exists. Non-agent shell, skill audit and harness writes do not use the
agent flag/context guard. The harness writer currently emits `source_kind=memory`,
which the production evidence CHECK already excludes; these changes do not
alter that independent schema mismatch.

Routing, overlay drafts, tune preconditions, delegation outcomes, tool canaries,
review/decomposition/plan calibration, agent scores and effectiveness readers
share `_interspect_eligible_sql`. Python skill readers use its byte-equivalent
`eligible_sql` counterpart, tested for parity. Skill scoring, tuning means/counts,
overlay baselines and observed goal refinement use the shared
`skill_signal_eligible_sql` / `_interspect_skill_signal_eligible_sql` predicate.
It requires an eligible invocation matching the signal's invocation ID, skill
and session. Zero eligible signals cannot generate an overlay body or refine
goal weights. Canary samples have no session column, so their writer and readers
join through the matching collected signal (invocation, skill, signal kind) to
recover the full tuple. Samples without that relationship are ineligible.
Evaluation and status progress recheck retained samples, including after a
migration re-quarantines evidence; both counts and aggregates exclude them.
Held rows remain visible in raw diagnostics and gate-control scans, never as
eligible evidence for learned decisions. Prior derived weights/overlays still
require operator reconciliation; filtering does not reconstruct their history.

Finished-session agent canary sampling is the time-quarantine exception: it
records normal telemetry immediately under the default 48-hour quarantine, but
never counts a low-confidence finding unless it is validly corroborated. This
prevents ordinary sessions from silently producing no samples. Autonomy and
agent scores use `gate_session` (lowercase raw session fallback) and `gate_source`
for history thresholds; canary sampling aggregates and deduplicates canonical
session aliases. Consensus audit agreement, output diversity and evidence counts
use the full evidence eligibility predicate. Agent tune targets are regex-checked
and SQL-escaped before querying evidence.

Before rollout, pause writers and rehearse on a consistent database copy outside
git. Disable corroboration for rollback; with writers still paused, drop only
the `interspect_gate_insert`/`interspect_gate_update` triggers and
`interspect_gate_schema` marker, re-quarantine every low-confidence row and
clear its corroboration timestamp/actor/mode in one transaction. Retain audit
tables and do not resume old gate code or restore legacy lifts. After reviewed
reconciliation, re-running the current migration is idempotent with retained
audit tables: it re-quarantines rows, refreshes guards and writes the marker last.
No audit-table cleanup or automatic restoration of old lifted states is needed.

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
