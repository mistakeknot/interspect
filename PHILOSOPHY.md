# interspect Philosophy

## Purpose
Agent performance profiler and routing optimizer. Collects evidence about flux-drive agent accuracy, proposes routing overrides for underperforming agents, and monitors canary periods. Companion plugin for Clavain.

## North Star
Maximize routing accuracy — the right agent fires on the right task at the right cost.

## Working Priorities
- Evidence collection fidelity
- Routing override precision
- Canary monitoring reliability

## Brainstorming Doctrine
1. Start from outcomes and failure modes, not implementation details.
2. Generate at least three options: conservative, balanced, and aggressive.
3. Explicitly call out assumptions, unknowns, and dependency risk across modules.
4. Prefer ideas that improve clarity, reversibility, and operational visibility.

## Planning Doctrine
1. Convert selected direction into small, testable, reversible slices.
2. Define acceptance criteria, verification steps, and rollback path for each slice.
3. Sequence dependencies explicitly and keep integration contracts narrow.
4. Reserve optimization work until correctness and reliability are proven.

## Decision Filters
- Does this improve agent selection accuracy?
- Does this reduce wasted tokens on wrong-fit agents?
- Is the evidence durable and replayable?
- Can a bad override be reverted before damage compounds?
