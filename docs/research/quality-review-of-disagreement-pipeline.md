# Quality Review: Disagreement -> Resolution -> Routing Signal Pipeline

**Reviewed diff:** `/tmp/qg-diff-1772294818.txt`
**Date:** 2026-02-28
**Languages in scope:** Go (intercore), Shell (bash), SQL

---

## Full Findings

### Findings Index

| SEVERITY | ID | Section | Title |
|---|---|---|---|
| HIGH | H1 | Shell / lib-interspect.sh | `ic state set` called with wrong argument convention — cursor value silently lost |
| HIGH | H2 | Shell / lib-interspect.sh | `ic state get` and `ic state set` use mismatched scope_id — cursor never advances |
| MEDIUM | M1 | Go / cmd/ic/events.go | `--type` flag accepted and validated but silently discarded with `_ = eventType` |
| MEDIUM | M2 | Go / internal/event/store.go | Replay input error silently swallowed in `AddReviewEvent` |
| LOW | L1 | Go / internal/event/event.go | Comment on `Event.Source` field is stale — still lists only 3 sources |
| LOW | L2 | Go / cmd/ic/events.go | `cmdEventsListReview` does not reject unknown flags — silently ignores them |
| IMPROVEMENT | I1 | Shell / commands/resolve.md | Shell block uses uppercase variable names — style mismatch with lib-interspect.sh |
| IMPROVEMENT | I2 | Go / internal/event/replay_capture.go | `reviewReplayPayload` double-encodes `agents_json` |

Verdict: needs-changes

---

### Summary

The disagreement-to-resolution pipeline is architecturally sound and follows established patterns (table shape, UNION ALL inclusion, replay capture, cursor-based pagination). The Go layer — schema, store methods, CLI subcommands, tests, integration tests — is correct and consistent with the rest of the codebase. Two shell bugs in `lib-interspect.sh` are the critical issues: `ic state set` accepts its payload on stdin, not as a positional argument, so the cursor integer value is silently discarded; and the get/set calls use different scope_ids (`""` vs `$max_id`), meaning even a corrected write would not be found by the subsequent read. Together these bugs make the review-event cursor permanently stuck at 0, causing every interspect polling cycle to re-consume all review events from the beginning.

---

### Issues Found

**H1. HIGH: `ic state set` called with payload as positional argument — value silently lost**

`ic state set` reads its payload from stdin (or a `@filepath` third positional). The shell call at `lib-interspect.sh:2154`:
```bash
ic state set "$cursor_key" "$max_id" "" 2>/dev/null || true
```
passes three positionals: `[0]=cursor_key`, `[1]=$max_id` (treated as scope_id), `[2]=""` (not `@`-prefixed, so the code falls through to `io.ReadAll(os.Stdin)`). Since stdin is not redirected here, this either blocks until stdin closes or reads an empty payload, which `ValidatePayload` rejects as invalid JSON. The `2>/dev/null || true` swallows the failure silently. The cursor is never persisted.

Fix:
```bash
printf '%s' "$max_id" | ic state set "$cursor_key" "" 2>/dev/null || true
```

Reference: `core/intercore/cmd/ic/main.go:628-631` (stdin branch), `core/intercore/internal/state/state.go:193-203` (ValidatePayload rejects non-JSON).

**H2. HIGH: `ic state get` and `ic state set` use mismatched scope_id — cursor read always misses**

Even if H1 were fixed, the get and set calls use different scope_ids. The current code:

- Get at `lib-interspect.sh:2130`: `ic state get "$cursor_key" ""`  (scope_id = `""`)
- Set at `lib-interspect.sh:2154`: `ic state set "$cursor_key" "$max_id" ""` (scope_id = `$max_id`, value = stdin)

`ic state` stores and retrieves by `(key, scope_id)` tuple. With mismatched scope_ids, the get never finds what the set wrote. Both calls must use the same scope_id. The correct convention for a single-instance cursor is `""` (empty scope). Fix: use scope_id `""` on both, and pipe the value via stdin as described in H1.

**M1. MEDIUM: `--type` flag accepted, validated as required, then silently dropped**

In `cmdEventsEmit` (`cmd/ic/events.go`):
```go
if eventType == "" {
    slog.Error("events emit: --type is required")
    return 3
}
// ...
_ = eventType // eventType validated but not used in AddReviewEvent (hardcoded as disagreement_resolved)
```
The CLI requires `--type`, validates it is non-empty, then discards it. `AddReviewEvent` hardcodes `disagreement_resolved` as the event type in the UNION ALL query. The flag appears functional but has no effect. Options: (a) remove the `--type` requirement (make it optional or reserved for future sources), or (b) persist `eventType` in `review_events` so it is actually used. At minimum, the error guard `if eventType == ""` should be removed since it falsely implies the value matters.

**M2. MEDIUM: Replay input insertion error silently swallowed in `AddReviewEvent`**

`internal/event/store.go:349-353`:
```go
if runID != "" {
    payload := reviewReplayPayload(...)
    _ = insertReplayInput(ctx, s.db.ExecContext, runID, "review_event", findingID, payload, "", SourceReview, &id)
}
```
Compare to `AddDispatchEvent` (lines 48-60) and `AddCoordinationEvent` (lines 222-234), which both propagate replay errors:
```go
if err := insertReplayInput(...); err != nil {
    return fmt.Errorf("add dispatch event replay input: %w", err)
}
```
If the intent for review events is "best-effort replay only," document that explicitly. If the intent is parity with dispatch/coordination, propagate the error.

**L1. LOW: `Event.Source` doc comment is stale — lists 3 sources, 6 now exist**

`internal/event/event.go:47`:
```go
Source    string `json:"source"`     // "phase", "dispatch", or "discovery"
```
There are now 6 source constants (`SourcePhase`, `SourceDispatch`, `SourceInterspect`, `SourceDiscovery`, `SourceCoordination`, `SourceReview`). Update the comment to reference the `Source*` constants block or say "see Source* constants in this package."

**L2. LOW: `cmdEventsListReview` silently ignores unknown flags**

The arg-parsing loop in `cmdEventsListReview` has no `default:` case. A mistyped flag like `--sinc=5` is silently ignored and the default cursor is used. `cmdEventsEmit` correctly has:
```go
default:
    slog.Error("events emit: unknown flag", "value", args[i])
    return 3
```
Add the same `default:` error case to `cmdEventsListReview` for consistency and debuggability.

---

### Improvements

**I1. Shell in commands/resolve.md uses uppercase locals — style mismatch with lib-interspect.sh**

The bash block in `commands/resolve.md` (step 5b) uses uppercase names (`FINDING_ID`, `SEVERITY`, `AGENTS_MAP`, `OUTCOME`, etc.) as loop-local variables inside a `while IFS= read -r finding; do` subshell. The rest of the shell codebase (`lib-interspect.sh`, `lib-sprint.sh`) uses lowercase for locals. Since this executes in a subshell from the `jq ... | while` pipeline, there is no functional collision, but the style diverges. If this code is ever extracted into a library function, uppercase names could shadow inherited globals. Prefer lowercase to stay consistent.

**I2. `reviewReplayPayload` double-encodes `agents_json`**

`internal/event/replay_capture.go:62-78`:
```go
out := map[string]interface{}{
    "agents_json": agentsJSON,  // already a JSON string, e.g. `{"fd-arch":"P1"}`
    ...
}
b, _ := json.Marshal(out)
```
`agentsJSON` is a pre-serialized JSON string. `json.Marshal` escapes it as a string value, producing `"agents_json":"{\"fd-arch\":\"P1\"}"` — double-encoded JSON. Use `json.RawMessage` to embed it correctly:
```go
out := map[string]json.RawMessage{
    "agents_json": json.RawMessage(agentsJSON),
    ...
}
```
This does not affect the `review_events` table record (which stores `agents_json` correctly), but makes the replay input payload harder to read and diff during debugging.

---

### What Is Well Done

- Schema design matches the interspect_events pattern precisely (nullable run_id, NULLIF on write, COALESCE on read, integer timestamps, separate indexes on finding_id and created_at).
- UNION ALL integration in `ListEvents` and `ListAllEvents` correctly includes `sinceReviewID` as a per-table cursor, preserving the independent-ID-space invariant documented in the ListEvents comment.
- The `cmdEventsEmit` source restriction (`if source != event.SourceReview`) is good forward-thinking API gate — prevents future misuse before other sources are ready.
- Integration tests in `test-integration.sh` cover emit, tail roundtrip, list-review, and since-cursor — good coverage for a new CLI surface.
- Unit tests for `AddReviewEvent`, optional fields, since-cursor, and MaxReviewEventID follow the established test structure exactly.
- Migration scaffolding (db.go migration block + 024_review_events.sql + 020_baseline.sql) is complete and consistent with the rest of the migration system.
