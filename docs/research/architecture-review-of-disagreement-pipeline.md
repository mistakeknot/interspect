# Architecture Review: Disagreement → Resolution → Routing Signal Pipeline

**Reviewer:** fd-architecture (Flux-drive Architecture & Design Reviewer)
**Date:** 2026-02-28
**Diff:** `/tmp/qg-diff-1772294818.txt`
**Verdict:** safe

---

## Scope

Reviewed the complete T/T+1/T+2 learning loop implementation:

- `core/intercore`: `internal/event/event.go`, `store.go`, `store_test.go`, `replay_capture.go`, `internal/db/db.go`, `schema.sql`, `migrations/020_baseline.sql`, `migrations/024_review_events.sql`, `cmd/ic/events.go`, `internal/observation/observation.go`, `test-integration.sh`
- `os/clavain`: `commands/resolve.md` (step 5b addition)
- `interverse/interspect`: `hooks/lib-interspect.sh` (`_interspect_process_disagreement_event`, `_interspect_consume_review_events`)

---

## 1. Boundaries & Coupling

### Layer integrity

The pipeline respects the three-layer architecture. `review_events` is a kernel (L1) concept. Shell scripts in L2 (clavain) and L2.5 (interspect) access it only via `ic` CLI calls — there is no direct SQLite access to the kernel DB from shell. The L3 direction of dependency is clean throughout.

### Data flow contract

The full flow is:

1. `commands/resolve.md` step 5b: Shell script reads `findings.json`, applies an impact gate, and calls `ic events emit --source=review --type=disagreement_resolved --context=<json>`.
2. `cmdEventsEmit` in `cmd/ic/events.go`: Validates the JSON context payload, calls `evStore.AddReviewEvent(...)`.
3. `AddReviewEvent` in `internal/event/store.go`: Inserts into `review_events`, optionally creates a replay input if `runID` is set.
4. `_interspect_consume_review_events` in `hooks/lib-interspect.sh`: Reads cursor from `ic state get`, calls `ic events list-review --since=<cursor>`, processes each event through `_interspect_process_disagreement_event`, advances cursor via `ic state set`.
5. `_interspect_process_disagreement_event`: Converts each agent entry in the `agents_json` map to an evidence row in the Interspect SQLite DB (for agents whose severity differed from `chosen_severity`).

End-to-end contract is sound. No layer boundary is crossed in the wrong direction.

### Dual query path rationale

The consumer uses `ic events list-review` (backed by `ListReviewEvents`) rather than `ic events tail` (backed by `ListAllEvents`). This is the correct choice: the UNION ALL representation squashes `ReviewEvent`-specific fields (finding_id, chosen_severity, impact, agents_json, dismissal_reason) into the generic 9-column `Event` struct, losing field fidelity that the consumer needs. The architecture context documents this explicitly, and it matches the `interspect_events` precedent.

The UNION ALL inclusion of `review_events` for the `--all` timeline view preserves observability without compromising consumer correctness.

### New dependencies

- `interverse/interspect` → `ic events list-review` (new CLI surface, same binary)
- `interverse/interspect` → `ic state get/set` for cursor (pre-existing pattern)
- `os/clavain/commands/resolve.md` → `ic events emit --source=review` (new surface)

All dependencies are kernel-inward. No new cross-module coupling is introduced.

---

## 2. Pattern Analysis

### Schema triple consistency

`review_events` DDL appears in all three required locations:
- `internal/db/schema.sql` — for fresh installs via `CREATE TABLE IF NOT EXISTS`
- `internal/db/migrations/020_baseline.sql` — for the Migrator baseline (plain `CREATE TABLE`, consistent with baseline convention)
- `internal/db/migrations/024_review_events.sql` — for the additive migrator path

Version constants `currentSchemaVersion` and `maxSchemaVersion` both set to 24. Migration guard in `db.go` applies the DDL correctly for existing databases. The 14 db_test.go assertions updated from 23 → 24 are mechanical and correct.

### Replay input capture

`AddReviewEvent` follows the `AddDispatchEvent` pattern: capture the insert ID, then call `insertReplayInput` with `SourceReview` and the event payload. The guard `if runID != ""` matches the existing pattern (dispatch also skips replay input when runID is empty). `reviewReplayPayload` follows the `dispatchReplayPayload` structure.

### Impact gate

Step 5b's impact gate filters to events with real routing signal value:
- Discarded findings where at least one agent rated P0 or P1 (high-stakes overrule)
- Accepted findings where the chosen severity differs from the agents' ratings (severity override signal)

This gate prevents routing signal pollution from routine agreement resolutions. It is coherent with the downstream evidence usage: only agents whose severity was overridden (`agent_severity != chosen_severity`) get evidence records.

### Dismissal-reason → override-reason mapping

`_interspect_process_disagreement_event` maps `dismissal_reason` values to the existing `override_reason` vocabulary:
- `agent_wrong` → `agent_wrong`
- `deprioritized` → `deprioritized`
- `already_fixed` → `stale_finding`
- `not_applicable` → `agent_wrong`
- `""` with `accepted + severity_overridden` → `severity_miscalibrated`

`severity_miscalibrated` is a new reason value. It should be verified against the existing evidence query and classification paths in `_interspect_get_classified_patterns` and `_interspect_is_routing_eligible` to confirm it does not disrupt the routing eligibility logic (which filters specifically for `agent_wrong` as the override reason). If `severity_miscalibrated` events should also feed routing proposals, the eligibility predicates will need updating.

---

## 3. Simplicity & YAGNI

The `--type` parameter in `ic events emit` is required at the CLI level but unused by `AddReviewEvent`, which hard-codes `disagreement_resolved` as the only event type. The flag creates mandatory caller overhead with no current functional value. If a second event type is needed in the future, the `review_events` table schema would need a `event_type` column anyway. The flag is premature extensibility.

The cursor management in `_interspect_consume_review_events` uses `ic state get/set` rather than the established durable cursor system. This works correctly, but introduces a second mental model for watermark persistence within the same consumer function. The durable cursor JSON payload now includes a `review` field (added by this diff), so the machinery for a unified cursor exists. This is a mild structural divergence, not a defect.

---

## Issues Found

**A-01. INFO: `--type` flag in `ic events emit` is mandatory but silently discarded**

`cmdEventsEmit` requires `--type` via `if eventType == ""` guard (returns exit 3), but the value is explicitly discarded with `_ = eventType` before `AddReviewEvent` is called. The comment acknowledges this. The flag creates a required-but-meaningless caller contract.

File: `/home/mk/projects/Demarch/core/intercore/cmd/ic/events.go`, `cmdEventsEmit`, line `_ = eventType`.

Smallest fix: remove the `--type` requirement check (make it optional and ignored), or drop the flag entirely. Do not add a `event_type` column to the table until there is a second concrete event type.

---

**A-02. INFO: `coordination_events` uses `id > 0` instead of a since-cursor in run-scoped `ListEvents`**

Pre-existing issue made more visible by this diff. The `review_events` leg correctly uses `id > sinceReviewID`, but the `coordination_events` leg uses `id > 0` — a hardcoded sentinel that bypasses cursor pagination. This means the coordination leg is always fully re-fetched from the beginning regardless of cursor position.

File: `/home/mk/projects/Demarch/core/intercore/internal/event/store.go` line 91.

This diff does not introduce the bug, but could be the right moment to fix it since the function signature already carries the unused `sinceCoordination` value in cursor state.

---

**A-03. INFO: Two cursor mechanisms coexist within the same consumer**

`_interspect_consume_kernel_events` uses the durable cursor system (`ic events cursor`). Its new callee `_interspect_consume_review_events` uses `ic state get/set` with a private key `interspect-disagreement-review-cursor`. Both are correct, but they establish two different mental models for the same concern in adjacent code paths.

The durable cursor JSON now has a `review` field. A future unification pass could read the review watermark from the shared cursor state instead of a private state key, consolidating to one mechanism.

File: `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`, `_interspect_consume_review_events` function.

---

**A-04. INFO: `reviewReplayPayload` double-encodes `agentsJSON`**

`agentsJSON` is already a JSON string (e.g., `{"fd-architecture":"P1"}`). Placing it as a `string` field in the `out` map then calling `json.Marshal` produces `{"agents_json":"{\"fd-architecture\":\"P1\"}"}` — a string containing JSON — rather than a nested object. This differs from what is stored in `review_events.agents_json` (the raw JSON) and from what the `list-review` output returns to the consumer.

The payload is only used for deterministic replay (PRD F1) and is not currently exercised in production. If replay is ever invoked on review events, the consumer would need to double-decode.

Fix: `out["agents_json"] = json.RawMessage(agentsJSON)` — one line change in `reviewReplayPayload`.

File: `/home/mk/projects/Demarch/core/intercore/internal/event/replay_capture.go` line 65.

---

**A-05. INFO: Migration guard lower bound `>= 20` may miss schemas at v14–v19**

The v23→v24 block uses `currentVersion >= 20`. The adjacent v22→v23 block uses `currentVersion >= 15`. A database at v14–v19 that somehow reaches this block (e.g., if intermediate guards also fail) would not create `review_events` but would still have `user_version` bumped to 24. The terminal `schemaDDL` application handles this via `CREATE TABLE IF NOT EXISTS`, so this is a soft gap.

Safer guard: `currentVersion >= 15 && currentVersion < 24`, consistent with the v22→v23 pattern.

File: `/home/mk/projects/Demarch/core/intercore/internal/db/db.go` line 357.

---

**A-06. INFO: `severity_miscalibrated` override reason is not handled by routing eligibility predicates**

`_interspect_process_disagreement_event` emits evidence rows with `override_reason="severity_miscalibrated"` for the accepted-with-severity-mismatch case. The routing eligibility check (`_interspect_is_routing_eligible`) and the classified patterns query (`_interspect_get_classified_patterns`) filter for `override_reason = 'agent_wrong'` to identify routing-eligible agents. Events with `severity_miscalibrated` will be stored as evidence but will not currently influence routing proposals or overlay eligibility.

This may be intentional — severity miscalibration might be handled as a separate signal path in the future. If it should contribute to routing proposals today, the eligibility predicate needs updating.

File: `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`, `_interspect_process_disagreement_event` case block, and `_interspect_is_routing_eligible` SQL query.

---

## Improvements

**I-01. Drop or make `--type` optional in `ic events emit`**
Remove the mandatory check. The source already determines the route. Eliminates dead code and simplifies caller contract.

**I-02. Fix double-encoding in `reviewReplayPayload`**
`out["agents_json"] = json.RawMessage(agentsJSON)` — one-line fix that makes the replay payload a proper nested object.

**I-03. Tighten migration guard to `>= 15` for v23→v24**
Consistent with adjacent migrations, closes the theoretical gap for v14–v19 databases.

**I-04. Document `severity_miscalibrated` routing intent**
Either add a comment in the evidence insertion stating this reason is intentionally excluded from routing eligibility for now, or add it to the eligibility predicate. Ambiguity here will surface as a surprise when routing proposals are eventually investigated.

**I-05. Fix `coordination_events` cursor (pre-existing)**
Change `id > 0` to `id > sinceCoordinationID` in `ListEvents` and thread the cursor value through `loadCursor`/`saveCursor`. Low risk, the machinery is already in place.
