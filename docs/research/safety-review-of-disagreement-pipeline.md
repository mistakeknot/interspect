# Safety Review: Disagreement Resolution Pipeline

**Date:** 2026-02-28
**Reviewer:** fd-safety (Flux-drive Safety Reviewer)
**Diff:** `/tmp/qg-diff-1772294818.txt`
**Risk Classification:** Medium — new event pipeline with shell-to-DB trust boundary; no auth/credential changes; internal-only (local SQLite, local hooks)

---

## Threat Model

**Deployment context:** Local-only tool. The `ic` binary and interspect hooks run on the developer's workstation. There is no network exposure of the event bus. The primary untrusted surface is the content of `.clavain/quality-gates/findings.json`, which is written by AI agents (interflux review agents). Agent-controlled strings flow through shell variables into `ic events emit --context=<JSON>` and then via the interspect hook into a SQLite evidence table.

**Untrusted inputs:**
- `findings.json` content: `.findings[].id`, `.findings[].severity`, `.findings[].severity_conflict` (agent-populated map), `.findings[].resolution`, `.findings[].dismissal_reason`
- `agents_json` field (stored verbatim in DB; re-parsed in hook as agent names and severity values)
- `session_id` (from `CLAUDE_SESSION_ID` env var or `--session` flag)
- `project_dir` (from `--project` flag or `git rev-parse --show-toplevel`)

**Trust boundaries:**
1. Shell (resolve.md Step 5b) reads findings.json and calls `ic events emit`
2. `ic events emit` parses `--context=<JSON>`, validates, stores in SQLite (parameterized)
3. Interspect hook reads `review_events` via `ic events list-review`, re-parses JSON fields, calls `_interspect_insert_evidence` which does SQLite string interpolation

**Data path:** findings.json -> jq-built JSON -> ic emit -> review_events table (parameterized) -> ic list-review (JSON output) -> bash jq parsing -> _interspect_insert_evidence (string-interpolated SQLite)

---

## Findings

### Finding 1: hook_id "interspect-disagreement" Not in Allowlist — Evidence Silently Dropped

**File:** `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`
**Lines:** 2115-2118 (call site), 2278-2288 (allowlist)

`_interspect_process_disagreement_event` calls `_interspect_insert_evidence` with `hook_id="interspect-disagreement"`. The allowlist in `_interspect_validate_hook_id` contains exactly:

```
interspect-evidence | interspect-session-start | interspect-session-end | interspect-correction | interspect-consumer
```

`interspect-disagreement` is not on this list. `_interspect_insert_evidence` returns 1 on invalid hook_id, and the call site uses `2>/dev/null || true`, so the failure is completely silent. Every disagreement event processed by the hook produces zero evidence rows. The entire pipeline delivers no routing signal.

**Impact:** High operational impact; the feature does not function at all. Low security risk (fail-closed is safe), but the hook_id validation is the first gate and it blocks unconditionally.

**Mitigation:** Add `interspect-disagreement` to the allowlist in `_interspect_validate_hook_id`, or change the call to pass a valid hook_id such as `interspect-consumer`.

---

### Finding 2: session_id From Kernel Events Not Sanitized Before Evidence Insertion

**File:** `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`
**Lines:** 2074, 2115-2118, 2330-2338

`session_id` is extracted from the kernel event JSON via `jq -r '.session_id // "unknown"'`. It is passed directly as the first argument to `_interspect_insert_evidence`. Inside that function, `session_id` is SQL-escaped with `${session_id//\'/\'\'}` (single-quote doubling) but is **not** passed through `_interspect_sanitize` first.

The sanitize pipeline (line 2244-2273) strips control characters, truncates, redacts secrets, and rejects prompt-injection patterns. None of those steps apply to `session_id`. The SQL escape uses only single-quote doubling, which is sufficient for basic SQLi but does not guard against null bytes or control characters that could corrupt the DB or cause unexpected query behavior.

Separately, `session_id` is used as a subquery parameter in rolling-window analytics (line 1717): `WHERE session_id IN (SELECT session_id FROM sessions ...)`. A crafted session_id that contains SQL metacharacters beyond a single quote (e.g., `--`, `;`, or Unicode look-alikes) could potentially affect query semantics depending on SQLite version behavior.

In contrast, all other fields (`source`, `event`, `override_reason`, `context_json`) are sanitized via `_interspect_sanitize` before the SQL escape step.

**Impact:** Low to medium. session_id originates from `CLAUDE_SESSION_ID` env var set by Claude Code. In a local-only deployment, this is not directly attacker-controlled. However, if a crafted `session_id` were stored in the kernel DB (e.g., via the `--session` flag at emit time), it would flow through to the evidence DB unsanitized.

**Mitigation:** Apply `_interspect_sanitize` to `session_id` inside `_interspect_insert_evidence` before the SQL-escape step, consistent with how other fields are handled.

---

### Finding 3: agent_name From agents_json Not Validated Before Evidence Insertion

**File:** `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`
**Lines:** 2094-2119

`agents_json` is extracted from the kernel event with `jq -r '.agents_json // "{}"'`. The field value is the re-serialized `agents` map stored verbatim from the emit call. In `_interspect_process_disagreement_event`, agent name keys are extracted via `jq -r '.key'` and passed as the `$2` (`source`) argument to `_interspect_insert_evidence`.

The function validates agent names with `_interspect_validate_agent_name` in routing contexts (lines 501, 566, 605, 814), but this validation is **not applied** in the disagreement path at line 2116. Agent names from the `agents_json` field are passed directly as `source`, which flows through `_interspect_sanitize` (good — it will truncate and redact), but the format constraint `^fd-[a-z][a-z0-9-]*$` is never enforced.

This means an agent name like `fd-architecture; DROP TABLE evidence;--` would be sanitized (truncated, single-quote escaped) before SQLite insertion, but a long or unexpected name would be stored without format validation. More concretely, the routing override logic that reads `source` values from evidence may behave unexpectedly if non-standard agent names accumulate.

**Impact:** Low. `_interspect_sanitize` provides the critical defense layer. The gap is format integrity, not injection. The operational risk is routing overrides being skewed by evidence rows with malformed agent names.

**Mitigation:** Call `_interspect_validate_agent_name "$agent_name"` in `_interspect_process_disagreement_event` before passing to `_interspect_insert_evidence`, and skip evidence for agents that fail validation (consistent with lines 566, 605).

---

### Finding 4: No Allowlist Validation on resolution, chosen_severity, or impact Values

**File:** `/home/mk/projects/Demarch/core/intercore/cmd/ic/events.go`
**Lines:** 373-397

The Go emit handler validates that `finding_id`, `resolution`, `chosen_severity`, and `impact` are non-empty, but does not validate their values against an allowlist. Accepted values in the rest of the system are:

- `resolution`: `"accepted"` | `"discarded"`
- `chosen_severity`: `"P0"` | `"P1"` | `"P2"` | `"P3"`
- `impact`: `"decision_changed"` | `"severity_overridden"`

Any string can be stored. In the hook's `case` statement for `dismissal_reason` (lines 2080-2089), unrecognized values fall through to the empty-string case, leaving `override_reason` unset. The routing logic downstream reads `resolution` and `impact` values from the DB. Unexpected values would cause routing logic branches to be silently skipped rather than causing errors, but the lack of input constraints weakens the system's ability to detect tampered or malformed events.

**Impact:** Low. In the local threat model, the emit caller is the resolve skill which constructs values from a controlled `case` statement. However, `ic events emit` is a general CLI with no external caller restriction.

**Mitigation:** Add enum validation in `cmdEventsEmit` for `Resolution` (`accepted`, `discarded`), `ChosenSeverity` (`P0`–`P3`), and `Impact` (`decision_changed`, `severity_overridden`). Return exit code 3 on unknown values.

---

### Finding 5: Shell-to-Go Trust Boundary — jq Escaping Is Correct But Depends on jq Availability

**File:** `/home/mk/projects/Demarch/os/clavain/commands/resolve.md`
**Lines:** 146-160

The CONTEXT variable is constructed via `jq -n --arg ... --argjson ...`. Using `--arg` for all string fields and `--argjson` for the pre-validated `AGENTS_MAP` is the correct pattern; jq handles all quoting internally and does not interpolate shell variables into JSON in an unsafe way. This is not a vulnerability.

However, the pattern falls back silently if `jq` is not available (the `command -v ic` guard does not check for `jq`). If jq is absent, `CONTEXT` would be empty, `--context=` would be rejected by the emit handler as empty, and the `|| true` ensures silence. This is safe (fail-closed from the kernel's perspective) but produces a confusing debugging experience.

**Impact:** Operational only. No security risk.

**Mitigation:** Add `command -v jq &>/dev/null || { ... continue; }` guard before the `jq -n` call.

---

### Finding 6: eventType Flag Accepted and Required But Silently Discarded

**File:** `/home/mk/projects/Demarch/core/intercore/cmd/ic/events.go`
**Lines:** 336-339, 395

`--type` is required and validated (non-empty), but then discarded with `_ = eventType`. The type is hardcoded to `disagreement_resolved` in `AddReviewEvent`. This is a minor API contract issue: callers must pass `--type=disagreement_resolved` exactly, but any non-empty string is silently accepted and ignored. A future caller using a different type string would receive a successful exit code but store the wrong semantic event.

**Impact:** Low. No security risk. Risk of silent API misuse.

**Mitigation:** Either validate `eventType == "disagreement_resolved"` and reject others, or change the flag to optional with a default. Document the constraint in the flag's error message.

---

### Finding 7: project_dir Stored Without Path Normalization or Validation

**File:** `/home/mk/projects/Demarch/core/intercore/cmd/ic/events.go`
**Lines:** 325, 360-361, 397

`projectDir` is accepted from `--project=<value>` or defaults to `os.Getwd()`. It is stored verbatim in `review_events.project_dir` with `NULLIF(?, '')`. No path normalization (e.g., `filepath.Clean`, `filepath.Abs`) is applied before storage. The hook reads `project_dir` from kernel events (line 2074 reads `session_id`, not `project_dir`, so `project_dir` is stored but not currently used in the hook). In the local threat model this is not exploitable, but if `project_dir` is later used to construct file paths for evidence lookup, an unsanitized value containing `..` components could allow path traversal.

**Impact:** Low. Not currently consumed by path-sensitive code. Residual risk if consumed downstream without sanitization.

**Mitigation:** Apply `filepath.Clean(filepath.Abs(projectDir))` before storage, consistent with the `--db` flag validation noted in the CLAUDE.md (`no ..`).

---

### Finding 8: Cursor State Key Is Hardcoded — No Isolation Across Projects

**File:** `/home/mk/projects/Demarch/interverse/interspect/hooks/lib-interspect.sh`
**Lines:** 2128-2154

The cursor key `"interspect-disagreement-review-cursor"` is a global key in `ic state`. It is not scoped to project or run. If the same `ic` DB is shared across multiple projects (which the `--db` flag allows), a single cursor would advance across all projects' review events interleaved. This is an operational isolation gap, not a security issue, but it could cause evidence from project A to be attributed to project B's interspect context.

**Impact:** Low. Operational correctness risk in multi-project setups.

**Mitigation:** Scope the cursor key with a project-derived suffix, e.g., `"interspect-disagreement-review-cursor:$(git rev-parse --show-toplevel 2>/dev/null | sha1sum | cut -c1-8)"`.

---

## Deployment & Migration Review

### Schema Migration (v23 to v24)

The migration is additive: new `review_events` table, no columns dropped or modified on existing tables. Migration is idempotent (`CREATE TABLE IF NOT EXISTS` in the Go block, `CREATE INDEX IF NOT EXISTS`). The baseline `020_baseline.sql` was updated to include the new table for fresh installs.

**Rollback:** The table can be dropped without affecting any other table. Rollback is: `DROP TABLE IF EXISTS review_events; PRAGMA user_version = 23;`. This is reversible.

**Pre-deploy check:** `ic --db=<path> events list-review --limit=1` should return 0 (no events) and exit 0 after migration. `ic --db=<path> events tail --all --limit=1` should continue to work.

**Risk:** Low. No data loss possible. The migration adds a new independent table.

### Event Bus UNION ALL Change

`review_events` is added to the `ListEvents` and `ListAllEvents` UNION ALL queries. The column projection uses `COALESCE(agents_json, '{}') AS reason` — mapping the agents map into the generic `reason` field. Consumers of the UNION ALL stream that process `Source == "review"` events will now see this field. The `scanEvents` function in `store.go` reads all 9 columns, so no schema mismatch risk.

**Risk:** Low. Existing consumers that don't check `Source == "review"` will receive new events in their stream but the `|| true` / cursor pattern means they will advance past them without errors.

### Cursor Backward Compatibility

The cursor JSON format changes from `{"phase":0,"dispatch":0,"interspect":0,"discovery":0}` to add `"review":0`. Existing cursors stored in `ic state` without the `"review"` field will deserialize to `review = 0` (zero value for int64 in Go), which is the correct default (read all review events from the beginning). This is backward-compatible.

---

## Summary

The disagreement pipeline adds a new event table and a shell-to-DB trust boundary. The Go layer is well-implemented: JSON is parsed via `json.Unmarshal` into a typed struct, all DB writes use parameterized queries (`ExecContext` with `?` placeholders), and the emit endpoint enforces presence of required fields. The primary risk area is in the interspect hook, which re-parses DB-sourced JSON in bash and uses string-interpolated SQLite. The existing `_interspect_sanitize` + single-quote escaping defense chain is applied to most fields, but `session_id` bypasses sanitization and `agent_name` bypasses format validation. The most severe operational issue is Finding 1 (invalid hook_id breaks the entire pipeline silently). No high-severity exploitable security issues are present given the local-only deployment context.

**Verdict:** needs-changes (Finding 1 breaks functionality; Findings 2-3 are medium-priority hardening)
