# Correctness Review: Disagreement Resolution Pipeline (v23→v24)

**Reviewer:** Julik (fd-correctness)
**Date:** 2026-02-28
**Diff:** `/tmp/qg-diff-1772294818.txt`
**Scope:** `internal/db/db.go`, `internal/db/schema.sql`, `internal/db/migrations/024_review_events.sql`, `internal/db/migrations/020_baseline.sql`, `internal/event/store.go`, `internal/event/replay_capture.go`, `cmd/ic/events.go`, `hooks/lib-interspect.sh`, `os/clavain/commands/resolve.md`

---

## Invariants Established Before Review

These must remain true across the pipeline:

1. **Event durability:** Every `AddReviewEvent` call that returns a non-error ID has a corresponding row in `review_events`.
2. **Replay completeness:** Every review event associated with a run has a corresponding `run_replay_inputs` row. Violation causes replay divergence.
3. **Cursor monotonicity:** Consumer cursors only advance. Re-delivering an event is acceptable (at-least-once). Skipping an event permanently is not.
4. **Migration idempotency:** Running `Migrate()` twice on the same database reaches the same final state.
5. **Schema consistency:** Fresh installs (schema.sql) and migrations (db.go + 024_review_events.sql) produce identical table structures.
6. **UNION ALL field alignment:** All arms of the UNION ALL query produce the same column count in the same semantic position.
7. **Shell injection safety:** All shell-constructed JSON payloads use `jq --arg`/`--argjson` rather than direct interpolation.

---

## Verdict: needs-changes

Three issues require fixes before trusting this in production:

- **C-01 (MEDIUM):** Replay input errors are silently discarded, breaking replay invariant.
- **C-02 (MEDIUM):** `coordination_events` has never had a working cursor in `ListEvents`; this diff adds a fifth correctly-cursored table without fixing the existing broken one.
- **C-03 (MEDIUM):** Shell consumer cursor treats DB errors and missing-key identically, causing full re-scan on infrastructure failure.

---

## Full Findings

Full findings with evidence, failure narratives, and fixes are at:

```
/home/mk/projects/Demarch/.clavain/quality-gates/fd-correctness-output.md
```

### Finding Summary

| ID | SEVERITY | Title |
|----|----------|-------|
| C-01 | MEDIUM | Replay input silently lost on insertReplayInput failure — error discarded |
| C-02 | MEDIUM | coordination_events lacks sinceCoordinationID cursor — always returns all rows |
| C-03 | MEDIUM | `ic state get` exits 1 on missing key — shell cursor fallback collapses infra errors |
| C-04 | LOW | Consumer cursor advances past events even when evidence insertion fails |
| C-05 | LOW | UNION ALL maps agents_json to `reason` column — silent semantic aliasing |
| C-06 | LOW | Shell cursor key in different namespace than unified event cursor |
| C-07 | LOW | Migration guard lower bound is >= 20 not >= 23 — wider than necessary |
| I-01 | INFO | Shell JSON construction is injection-safe (jq --arg/--argjson pattern) |
| I-02 | INFO | eventType validated but discarded — misleading to callers |

---

## What Is Correct

- **Migration idempotency:** `CREATE TABLE IF NOT EXISTS` in `db.go` and `024_review_events.sql` is safe for re-runs. `020_baseline.sql` (plain `CREATE TABLE`) is for fresh installs only and is correct.
- **Schema consistency:** Fresh install via `schema.sql` and upgrade via `db.go`+`024_review_events.sql` produce the same `review_events` DDL.
- **Version guard:** The `currentVersion >= 20 && currentVersion < 24` guard is safe (IF NOT EXISTS handles idempotency). The lower bound is wider than necessary (see C-07) but not incorrect.
- **AddReviewEvent insert:** The `NULLIF(?, '')` pattern for nullable columns is consistent with the rest of the codebase.
- **ListReviewEvents:** Correct cursor-based pagination via `id > ?`. Scan column count matches SELECT column count.
- **Cursor save guard:** `ic events tail` only saves cursor after a non-empty batch with no encode error — correct.
- **Cursor register initialization:** The `"review":0` field in the initial cursor JSON payload is correctly added.
- **UNION ALL parameter binding:** The parameter count in `ListEvents` (runID×6 + sincePhaseID + sinceDispatchID + sinceReviewID + limit = 10) matches the SQL placeholder count.
- **Shell JSON construction:** All shell code uses `jq -n --arg/--argjson` — no injection risk.
- **Impact gate logic:** The `HAS_HIGH_SEVERITY` and `SEVERITY_MISMATCH` conditions correctly implement the PRD gate.
- **Consumer idempotency:** Evidence insertions in interspect.db have no unique constraint on finding_id+agent, so duplicate delivery produces duplicate evidence rows, not a crash. Acceptable for at-least-once semantics.
