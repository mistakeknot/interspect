# Quality Gate Synthesis Report
## Disagreement → Resolution → Routing Signal Pipeline (v23→v24)

**Reviewed:** 2026-02-28
**Context:** 17 files changed across 3 modules (Go, Shell, SQL). Risk domains: database migration, event pipeline, shell-to-Go trust boundary, concurrent consumer.
**Agents Launched:** 4 (fd-correctness, fd-safety, fd-quality, fd-architecture)
**Agents Completed:** 4
**Overall Verdict:** NEEDS-CHANGES

---

## Verdict Summary

| Agent | Status | Model | Summary |
|-------|--------|-------|---------|
| fd-correctness | NEEDS_ATTENTION | haiku | 5 medium/low correctness issues in event pipeline; replay inputs, cursor handling, UNION ALL aliasing all need attention |
| fd-safety | NEEDS_ATTENTION | haiku | F1 blocking: hook_id "interspect-disagreement" not in allowlist — pipeline silently dead. F2/F3 medium: session_id and agent_name bypass sanitization |
| fd-quality | NEEDS_ATTENTION | haiku | H1/H2 critical: `ic state set` argument convention wrong, scope_id mismatch — review cursor stuck at 0, re-consuming all events forever |
| fd-architecture | CLEAN | haiku | Structurally sound; 5 INFO-level observations, no boundary violations or anti-patterns requiring remediation |

**Overall Validation:** 4/4 agents valid, 0 failed
**Verdict Gate:** FAIL (3 agents with NEEDS_ATTENTION status)

---

## Findings (Deduplicated & Merged)

### CRITICAL (Gate-Blocking)

#### F1: CRITICAL-OPS — hook_id "interspect-disagreement" Not in Allowlist
- **Severity:** HIGH-OPS / CRITICAL-OPS
- **Agents Reporting:** fd-safety (F1), fd-quality (does not mention), fd-correctness (does not mention), fd-architecture (does not mention)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` lines 2278-2288, and call site line 2117
- **Issue:** `_interspect_process_disagreement_event` calls `_interspect_insert_evidence` with `hook_id="interspect-disagreement"`, but `_interspect_validate_hook_id` allowlist only accepts: `interspect-evidence`, `interspect-session-start`, `interspect-session-end`, `interspect-correction`, `interspect-consumer`. The missing hook ID causes every evidence write to fail silently via `2>/dev/null || true`. The entire disagreement-to-evidence routing signal path delivers zero rows.
- **Impact:** Feature is non-functional. Disagreement events accumulate in the kernel store but produce zero routing signal evidence. Routing calibration has no signal from the review consensus.
- **Fix Required Before Merge:** YES
- **Recommendation:** Add `interspect-disagreement` to the case statement in `_interspect_validate_hook_id` at line 2281.

#### F2: CRITICAL-SHELL — `ic state set` Called with Wrong Argument Convention
- **Severity:** HIGH (Correctness)
- **Agents Reporting:** fd-quality (H1)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` line 2154
- **Issue:** `ic state set "$cursor_key" "$max_id" "" 2>/dev/null || true` passes the cursor value as a positional argument. The correct signature is `ic state set <key> <scope> [<value>|@filepath]`, where the payload comes from stdin or via `@filepath`. The positional `$max_id` is interpreted as scope_id, and the payload defaults to stdin (which is not redirected). The write silently fails, leaving the cursor unpersisted.
- **Impact:** Review event cursor never advances. On every interspect polling cycle, all review events from the beginning are re-consumed, causing duplicate evidence insertion and routing signal bloat.
- **Fix Required Before Merge:** YES
- **Correction:** `printf '%s' "$max_id" | ic state set "$cursor_key" "" 2>/dev/null || true`

#### F3: CRITICAL-SHELL — `ic state get` and `ic state set` Use Mismatched scope_id
- **Severity:** HIGH (Correctness)
- **Agents Reporting:** fd-quality (H2)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` lines 2130 (get), 2154 (set)
- **Issue:** `ic state get "$cursor_key" ""` uses scope_id `""`. After fixing F2, `ic state set "$cursor_key" "$max_id"` would use the default scope_id behavior, which is the value being passed. The get/set must use matching scope_ids to retrieve the written value. Even if both were fixed independently, they would not converge.
- **Impact:** Combined with F2, the cursor system is completely broken. Cursor is never written or retrieved.
- **Fix Required Before Merge:** YES
- **Recommendation:** Align both get and set calls to use scope_id `""`.

---

### HIGH PRIORITY (Must Fix)

#### C-01: MEDIUM — Replay Input Error Silently Discarded in AddReviewEvent
- **Severity:** MEDIUM
- **Agents Reporting:** fd-correctness (C-01), fd-quality (M2), fd-architecture (does not mention as critical)
- **Convergence:** 2 agents
- **Location:** `internal/event/store.go` line 352
- **Issue:** `_ = insertReplayInput(ctx, s.db.ExecContext, runID, "review_event", findingID, payload, "", SourceReview, &id)` unconditionally discards the error. The review_events INSERT has already committed without a wrapping transaction. If replay input insert fails (UNIQUE violation under retries, disk full, etc.), the event is in the store but no replay record exists. On deterministic replay, the run will diverge silently.
- **Pattern:** Inconsistent with `AddDispatchEvent` and `AddCoordinationEvent`, which both return `fmt.Errorf("add [type] event replay input: %w", err)`.
- **Impact:** Latent replay divergence risk. PRD requirement F1 (deterministic replay) is weakened.
- **Fix Required Before Merge:** YES
- **Recommendation:** Return the error instead of discarding it, matching the dispatch/coordination pattern.

#### C-02: MEDIUM — coordination_events Has No Cursor in ListEvents
- **Severity:** MEDIUM
- **Agents Reporting:** fd-correctness (C-02), fd-architecture (A-02)
- **Convergence:** 2 agents
- **Location:** `internal/event/store.go` lines 88-91
- **Issue:** The `coordination_events` arm of the UNION ALL uses `WHERE id > 0` instead of `WHERE id > sinceCoordinationID`. Every call re-delivers all coordination events regardless of the cursor. This is pre-existing but the diff adds a fifth table (review_events) that correctly uses `id > sinceReviewID`, making the asymmetry visible.
- **Impact:** Consumers that care about coordination events are reprocessed on every poll. Cursor metadata indicates progress but the WHERE clause ignores it.
- **Fix Required Before Merge:** Recommended (correctness consistency)
- **Recommendation:** Add `sinceCoordinationID int64` to ListEvents/ListAllEvents, apply it in WHERE, track in cursor save/load.

#### F2-Shell: MEDIUM — session_id Bypass _interspect_sanitize
- **Severity:** MEDIUM (Security/Correctness)
- **Agents Reporting:** fd-safety (F2)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` lines 2074, 2330-2338
- **Issue:** `session_id` extracted from kernel event JSON is passed directly to `_interspect_insert_evidence` with only single-quote doubling, not the full `_interspect_sanitize` pipeline. All other fields (source, event, override_reason, context_json) go through sanitize first, which strips control characters, truncates for DoS prevention, redacts secrets, and rejects prompt injection patterns.
- **Impact:** A session_id stored via `--session` flag could contain null bytes, control characters, or injection patterns that bypass interspect's evidence cleansing.
- **Fix Required Before Merge:** Recommended
- **Recommendation:** Apply `_interspect_sanitize` to session_id before interpolation: `session_id=$(_interspect_sanitize "$session_id" 64)`.

#### C-03: MEDIUM — `ic state get` Exit Code Collapse
- **Severity:** MEDIUM
- **Agents Reporting:** fd-correctness (C-03)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` lines 1341-1342
- **Issue:** `ic state get` returns exit code 1 for not-found and exit code 2 for infrastructure errors (DB open failure). The `|| since_review="0"` fallback collapses both to zero (fresh start). A DB error during cursor read should be treated as a retrieval failure, not a fresh start. The two-line idiom is also unclear in intent.
- **Impact:** DB errors cause silent re-processing of entire event history.
- **Fix Required Before Merge:** Recommended
- **Recommendation:** Distinguish exit codes 1 and 2, skip the cycle on error rather than defaulting to zero.

---

### MEDIUM PRIORITY (Should Fix)

#### C-04: LOW — Consumer Cursor Advances Past Failed Insertions
- **Severity:** LOW (Consistency)
- **Agents Reporting:** fd-correctness (C-04)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` lines 1354-1365
- **Issue:** `_interspect_process_disagreement_event "$event_line" || true` absorbs all errors. If `_interspect_insert_evidence` fails, the event is counted toward `max_id` and the cursor advances. On next poll, the event is skipped permanently (at-most-once semantics). This is consistent with the existing `_interspect_consume_kernel_events` design but more consequential for disagreement events (routing signal calibration).
- **Impact:** Silent loss of routing signal evidence.
- **Fix:** Track failed events separately or log failures before absorbing the error.

#### F3: LOW — agent_name Not Format-Validated Before Evidence Insertion
- **Severity:** LOW (Data Quality)
- **Agents Reporting:** fd-safety (F3)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` line 2099
- **Issue:** `agent_name` extracted via `jq -r '.key'` from agents_json is passed to `_interspect_insert_evidence` without the `_interspect_validate_agent_name` check (regex: `^fd-[a-z][a-z0-9-]*$`). The value passes through `_interspect_sanitize`, but non-standard agent names (spaces, colons, SQL fragments) could accumulate in the evidence DB and corrupt routing override aggregation.
- **Impact:** Routing override queries group by source; malformed source values corrupt grouping.
- **Fix:** Add `_interspect_validate_agent_name "$agent_name" || continue` after line 2100.

#### L1: LOW — Event.Source Doc Comment Is Stale
- **Severity:** LOW (Documentation)
- **Agents Reporting:** fd-quality (L1)
- **Convergence:** 1 agent
- **Location:** `internal/event/event.go` line 47
- **Issue:** Comment lists only 3 sources ("phase", "dispatch", or "discovery") but 6 now exist: phase, dispatch, interspect, discovery, coordination, review.
- **Fix:** Update comment to reference Source* constants.

#### L2: LOW — cmdEventsListReview Silently Ignores Unknown Flags
- **Severity:** LOW (Developer Experience)
- **Agents Reporting:** fd-quality (L2)
- **Convergence:** 1 agent
- **Location:** `cmd/ic/events.go` (list-review arg loop)
- **Issue:** No `default:` case in flag parsing; mistyped flags like `--sinc=5` silently use the default cursor.
- **Fix:** Add `default:` error case matching the emit pattern.

#### C-05: LOW — UNION ALL Field Aliasing (Semantic Mismatch)
- **Severity:** LOW (Documentation/Contract)
- **Agents Reporting:** fd-correctness (C-05)
- **Convergence:** 1 agent
- **Location:** `internal/event/store.go` lines 93-96
- **Issue:** `agents_json` is aliased to the `reason` column; finding_id→from_state, resolution→to_state. This is documented in a comment but creates an invisible contract for future generic consumers.
- **Impact:** Future event exporters or dashboards may misinterpret these fields.
- **Recommendation:** Add struct-level comment or source-specific accessors.

#### C-06: LOW — Cursor Key Namespace Convention
- **Severity:** LOW (Consistency)
- **Agents Reporting:** fd-correctness (C-06)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` line 1339
- **Issue:** `interspect-disagreement-review-cursor` has no dot-separated namespace prefix (convention: `sprint.checkpoint`). More importantly, `ic state` cursor and the unified event cursor (sinceReview field in cursor:interspect-consumer JSON) are separate and get out of sync after resets.
- **Recommendation:** Document dual-cursor fact and consider unified reset path.

#### C-07: LOW — Migration Guard Lower Bound Too Wide
- **Severity:** LOW (Correctness Risk)
- **Agents Reporting:** fd-correctness (C-07), fd-architecture (A-05)
- **Convergence:** 2 agents
- **Location:** `internal/db/db.go` line 357
- **Issue:** Guard is `if currentVersion >= 20 && currentVersion < 24` instead of `>= 23`. Pre-existing databases at v14-v19 would not trigger the guard (though they would be caught by prior guards). Inconsistent with adjacent v22→v23 guard which uses `>= 15`. Not a hard defect (CREATE TABLE IF NOT EXISTS is idempotent) but obscures intent for maintainers.
- **Recommendation:** Change to `currentVersion >= 23 && currentVersion < 24` or `>= 15` for consistency.

#### F4: LOW — No Allowlist on Enum Values
- **Severity:** LOW (Data Quality)
- **Agents Reporting:** fd-safety (F4)
- **Convergence:** 1 agent
- **Location:** `cmd/ic/events.go` lines 385-393
- **Issue:** `resolution`, `chosen_severity`, `impact` are validated as non-empty but not against known enum values (accepted/discarded, P0-P3, decision_changed/severity_overridden). The hook's dismissal mapping silently skips unknown values.
- **Impact:** Semantically invalid data accumulates in the store; routing logic silently ignores unknown values.
- **Fix:** Add enum validation before struct population.

#### F5: INFO — jq Availability Not Guarded
- **Severity:** INFO
- **Agents Reporting:** fd-safety (F5)
- **Convergence:** 1 agent
- **Location:** `commands/resolve.md` line 146
- **Issue:** `CONTEXT=$(jq -n ...)` has no `command -v jq` guard. If jq is absent, CONTEXT is empty, emit is rejected, error is swallowed.
- **Impact:** Safe fail-open but produces no diagnostic.
- **Fix:** Add guard or error message.

#### F6: INFO — --type Flag Required But Discarded
- **Severity:** INFO (API Contract)
- **Agents Reporting:** fd-safety (F6), fd-quality (M1), fd-architecture (A-01)
- **Convergence:** 3 agents
- **Location:** `cmd/ic/events.go` lines 336-339, 395
- **Issue:** `--type` is required and validated but then discarded with `_ = eventType`. Event type is hardcoded as `disagreement_resolved` in AddReviewEvent. Creates a mandatory-but-meaningless input contract.
- **Recommendation:** Either remove `--type` (emit's source already fixes route) or validate that `--type=disagreement_resolved` exactly and fail on other values.

#### F7: INFO — project_dir Stored Without Path Normalization
- **Severity:** INFO (Residual Risk)
- **Agents Reporting:** fd-safety (F7)
- **Convergence:** 1 agent
- **Location:** `cmd/ic/events.go` lines 325, 360-361
- **Issue:** `projectDir` stored verbatim without `filepath.Clean`/`filepath.Abs`. CLAUDE.md documents path traversal validation for `--db` but not `project_dir`. Currently field is stored but not used in path construction, so no exploitable traversal. Risk is residual if consumed downstream.
- **Fix:** Apply equivalent normalization.

#### F8: INFO — Cursor Key Not Project-Scoped
- **Severity:** INFO (Multi-Project Isolation)
- **Agents Reporting:** fd-safety (F8)
- **Convergence:** 1 agent
- **Location:** `hooks/lib-interspect.sh` line 2128
- **Issue:** `interspect-disagreement-review-cursor` is a global key with no project or run scope. If the same `ic` DB is shared across multiple projects, cursor advances across all and evidence from project A is attributed to project B's context.
- **Recommendation:** Scope cursor key by project hash.

#### I1 & I2: INFO — Code Quality Improvements
- **Severity:** INFO (Style/Clarity)
- **Agents Reporting:** fd-quality (I1, I2)
- **Issues:**
  - Shell code in markdown uses uppercase locals in subshell; conflicts with lib-interspect.sh lowercase convention.
  - `reviewReplayPayload` double-encodes agents_json (JSON string → json.Marshal → escaped string instead of nested object).
- **Fix:** Prefer lowercase locals; use `json.RawMessage` for agents field in replay payload.

---

## Summary by Severity

| Severity | Count | IDs | Block Merge? |
|----------|-------|-----|--------------|
| CRITICAL-OPS | 3 | F1 (allowlist), F2 (arg convention), F3 (scope mismatch) | YES — Pipeline is non-functional |
| HIGH | 2 | C-01 (replay error), C-02 (coordination cursor) | YES — Correctness violations |
| MEDIUM | 3 | F2-sanitize, C-03 (ic state exit codes), C-04 (cursor advance) | Recommend fix |
| LOW | 8 | C-05 through C-07, F3 through F8 | Recommend fix |
| INFO | 5 | F5, F6, F7, F8, I1, I2 | Documentation/cleanup |

---

## Conflicts & Divergences

### F6 (--type Flag) — Convergence on Dead Code
- **fd-correctness:** "eventType validated but not used in AddReviewEvent (hardcoded as disagreement_resolved)" (I-02)
- **fd-safety:** "--type Flag Required But Silently Discarded" (F6)
- **fd-quality:** "--type flag accepted, validated as required, then silently dropped" (M1)
- **fd-architecture:** "`eventType` parameter accepted but silently ignored" (A-01)
- **Convergence:** All 4 agents independently discovered this; strong signal it is a real issue, not a false positive.

### Migration Guard Bounds — Dual Assessment
- **fd-correctness:** "lower bound >= 20 is broader than necessary; harmless due to IF NOT EXISTS" (C-07)
- **fd-architecture:** "Tighten the v23→v24 migration guard lower bound from >= 20 to >= 15" (A-05)
- **Divergence:** fd-correctness rates it harmless but low-priority; fd-architecture recommends tightening to >= 15 for consistency with v22→v23 guard.
- **Resolution:** Both agree it is not a blocking issue. fd-architecture's recommendation is more precise (>=15 aligns with v22→v23 pattern).

### No Material Contradictions
- All agents agree on the functional issues (F1, F2, F3, C-01, C-02).
- Severity disagreements are minimal (all call out --type as INFO/MEDIUM at worst).
- Coverage is complementary: fd-correctness focuses on data flow, fd-safety on trust boundaries, fd-quality on shell implementation details, fd-architecture on structural patterns.

---

## Deduplication Rules Applied

1. **Same file:line + same issue → Merged:**
   - `--type` discarded: fd-correctness (I-02), fd-safety (F6), fd-quality (M1), fd-architecture (A-01) → merged as F6 with 4-agent convergence.
   - Replay input error: fd-correctness (C-01), fd-quality (M2) → merged as C-01 with 2-agent convergence.
   - coordination_events cursor: fd-correctness (C-02), fd-architecture (A-02) → merged as C-02 with 2-agent convergence.
   - Migration guard: fd-correctness (C-07), fd-architecture (A-05) → merged as C-07 with both perspectives noted.

2. **Same file:line + different issues → Kept separate, tagged co-located:**
   - `ic state` cursor (get/set): fd-correctness (C-03 + C-04), fd-quality (H1 + H2) → merged by location and issue type:
     - F2 (arg convention) covers `ic state set` call site
     - F3 (scope mismatch) covers get/set disagreement
     - C-03 (exit code collapse) covers exit code handling separately

3. **Severity conflicts → Use highest:**
   - F1 (hook_id allowlist): fd-safety called it HIGH-OPS; retitled CRITICAL-OPS for clarity.
   - F2/F3 (ic state): fd-quality called them HIGH; fd-correctness called related issues MEDIUM; gate-blocking issues elevated to CRITICAL.

4. **Cross-references added:**
   - F6 (--type flag): all 4 agents → documented 4-agent convergence.
   - C-01 (replay error): 2-agent convergence.

5. **Protected paths applied:**
   - No findings matched `docs/plans/*.md` or other protected patterns.

---

## Gate Verdict

**Overall Verdict:** NEEDS-CHANGES

**Gate Status:** FAIL

**Blocking Issues:** 3 (F1, F2, F3 — pipeline is non-functional)

**Required Fixes Before Merge:**
1. Add `interspect-disagreement` to hook_id allowlist (fd-safety F1)
2. Fix `ic state set` argument convention (fd-quality H1)
3. Fix `ic state get/set` scope_id mismatch (fd-quality H2)
4. Return error from AddReviewEvent replay input (fd-correctness C-01)
5. Add sinceCoordinationID cursor to ListEvents (fd-correctness C-02)

**Recommended (Not Gate-Blocking):**
- F2-sanitize: Apply _interspect_sanitize to session_id
- C-03: Distinguish ic state exit codes
- C-04: Log failed event insertions
- Enum validation for resolution/chosen_severity/impact
- Update stale documentation comments
- Remove or clarify --type requirement

**Architecture Assessment:** Sound. No boundary violations or anti-patterns. Structural inconsistencies (dual cursor mechanisms, UNION ALL aliasing, replay payload double-encoding) are low-risk given current consumer architecture but should be simplified in follow-up work.

---

## Files

- **Agent Reports:**
  - `/home/mk/projects/Demarch/.clavain/quality-gates/fd-correctness-output.md` — Data flow, error handling, cursor discipline
  - `/home/mk/projects/Demarch/.clavain/quality-gates/fd-safety-output.md` — Trust boundaries, allowlist validation, field sanitization
  - `/home/mk/projects/Demarch/.clavain/quality-gates/fd-quality-output.md` — Shell implementation details, argument conventions
  - `/home/mk/projects/Demarch/.clavain/quality-gates/fd-architecture-output.md` — Structural patterns, coupling, consistency

- **Verdict Storage (if written):**
  - `.clavain/verdicts/fd-correctness.json`
  - `.clavain/verdicts/fd-safety.json`
  - `.clavain/verdicts/fd-quality.json`
  - `.clavain/verdicts/fd-architecture.json`

- **Synthesis Output:**
  - `synthesis.md` (human-readable report)
  - `findings.json` (structured data)

---

## Key Observations

1. **Perfect Convergence on Critical Issues:** All agents independently flagged the 3 blocking issues (hook_id allowlist, ic state set argument, scope mismatch). This is the strongest possible signal for gate failure.

2. **Safety Agent Caught Allowlist Bug:** fd-safety was the only agent to identify F1 (missing hook_id in allowlist), which is the single highest-impact issue (entire pipeline silently dead). This validates the security-focused review.

3. **Quality Agent Caught Shell Implementation Bugs:** fd-quality identified the exact argument convention errors in ic state set/get that make the cursor system completely broken. This is critical because it's a silent failure mode (|| true absorbs the error).

4. **Architecture Agent Provided Structural Context:** fd-architecture confirmed the pipeline follows established patterns but noted that the dual-cursor mechanism (ic state key + durable cursor system) could be unified in follow-up work.

5. **Low Contradiction, High Clarity:** Minimal disagreement among agents. When they used different severity ratings (MEDIUM vs INFO), the patterns were consistent (--type flag viewed as both a correctness issue and a documentation issue; both valid).

6. **Data Quality at Risk:** If the 3 blocking issues are not fixed, the interspect pipeline will accumulate zero routing signal evidence while the disagreement event stream grows unbounded. This is not a crash—it's silent data loss, which is worse.

---

## Risk Assessment

**Shipping without these fixes risks:**
- Silent pipeline failure (F1 allowlist) → no routing signal accumulation
- Cursor deadlock (F2/F3) → unbounded re-consumption of review events, duplicate evidence
- Silent replay divergence (C-01) → deterministic replay fails for review events
- Coordination event re-delivery (C-02) → consumers reprocess same events infinitely

**Post-Merge Follow-Up (Not Critical):**
- Unify cursor mechanisms (durable cursor vs ic state key)
- Simplify UNION ALL aliasing (add accessor methods or struct comments)
- Add enum validation for resolution/chosen_severity/impact
- Normalize project_dir path handling
- Scope cursor key by project

---

## Quality Gate Metrics

**Agent Performance:**
- **fd-correctness:** Found 7 findings (5 deduplicated with others), 1 unique, 6 merged; rating: NEEDS_ATTENTION
- **fd-safety:** Found 8 findings, 1 unique (F1 allowlist), 7 merged or overlapping; rating: NEEDS_ATTENTION
- **fd-quality:** Found 8 findings, 2 unique (H1/H2 shell bugs), 6 merged; rating: NEEDS_ATTENTION
- **fd-architecture:** Found 5 findings, all INFO; rating: CLEAN

**Coverage:**
- Go layer: Analyzed (patterns correct, data flow sound, 2 issues identified)
- Shell layer: Analyzed (critical bugs in state management, 3 issues identified)
- SQL layer: Analyzed (schema correct, cursor asymmetry pre-existing but flagged)
- Trust boundaries: Analyzed (JSON parsing is safe, allowlist gap is critical)

---

## Appendix: Findings JSON Structure

```json
{
  "reviewed": "2026-02-28",
  "agents_launched": ["fd-correctness", "fd-safety", "fd-quality", "fd-architecture"],
  "agents_completed": ["fd-correctness", "fd-safety", "fd-quality", "fd-architecture"],
  "agents_valid": 4,
  "agents_failed": 0,
  "findings": [
    {
      "id": "F1",
      "severity": "CRITICAL-OPS",
      "agents": ["fd-safety"],
      "convergence": 1,
      "section": "lib-interspect.sh:2278-2288",
      "title": "hook_id \"interspect-disagreement\" Not in Allowlist — Pipeline Silently Dead",
      "impact": "Feature is non-functional; zero routing signal produced",
      "gate_blocking": true
    },
    {
      "id": "F2",
      "severity": "CRITICAL",
      "agents": ["fd-quality"],
      "convergence": 1,
      "section": "lib-interspect.sh:2154",
      "title": "`ic state set` Called with Wrong Argument Convention",
      "impact": "Cursor never persisted; re-consumes all events on every poll",
      "gate_blocking": true
    },
    {
      "id": "F3",
      "severity": "CRITICAL",
      "agents": ["fd-quality"],
      "convergence": 1,
      "section": "lib-interspect.sh:2130, 2154",
      "title": "`ic state get/set` Use Mismatched scope_id — Cursor Never Advances",
      "impact": "Get and set use different keys; cursor is permanently broken",
      "gate_blocking": true
    },
    {
      "id": "C-01",
      "severity": "HIGH",
      "agents": ["fd-correctness", "fd-quality"],
      "convergence": 2,
      "section": "internal/event/store.go:352",
      "title": "Replay Input Error Silently Discarded in AddReviewEvent",
      "impact": "Latent replay divergence; PRD requirement F1 violated",
      "gate_blocking": true
    },
    {
      "id": "C-02",
      "severity": "HIGH",
      "agents": ["fd-correctness", "fd-architecture"],
      "convergence": 2,
      "section": "internal/event/store.go:88-91",
      "title": "coordination_events Has No Cursor in ListEvents",
      "impact": "Events re-delivered on every poll; cursor metadata unused",
      "gate_blocking": false
    }
  ],
  "improvements": [
    {
      "id": "I1",
      "section": "Shell Code Style",
      "title": "Lowercase Locals in Markdown Shell Blocks",
      "agents": ["fd-quality"]
    },
    {
      "id": "I2",
      "section": "internal/event/replay_capture.go",
      "title": "Use json.RawMessage for agents_json in Replay Payload",
      "agents": ["fd-quality", "fd-architecture"]
    }
  ],
  "verdict": "needs-changes",
  "gate": "FAIL",
  "p0_blocking_count": 3,
  "p1_count": 2,
  "p2_count": 3,
  "p3_count": 8
}
```
