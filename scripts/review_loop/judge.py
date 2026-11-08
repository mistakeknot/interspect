"""Judge prompt construction and judgment validation.

The judge is a separate model call dispatched through the packaged role
dispatcher; this module only builds the prompt and validates the reply, so both
stay testable without a network or a model.
"""

from __future__ import annotations

import json
import re

from .core import CATEGORIES, OUTCOMES, SEVERITY_WEIGHT

CLASSES = ("operational_slip", "semantic_drift", "unknown")
PREVENT = ("test", "lint", "type", "ci", "brief", "none")
_BLOCK = re.compile(r"JUDGMENT_JSON:\s*(\{.*\})\s*$", re.S)

RUBRIC = """Taxonomy rubric v1. For each finding in the review text choose one category by
precedence: security-access, money-spend, concurrency-state, data-contract,
error-handling, api-misuse, spec-drift, docs-claim, missing-tests, logic-bug,
ci-gate, scope-hygiene. Outcome is one of: defect, defect_minor, policy_gate,
process_slip, over_strict, unclear. Class is operational_slip, semantic_drift or
unknown and must rest on evidence in the text. Severity follows impact: high,
medium, low. State the mechanism in one line and what would have prevented it
(test, lint, type, ci, brief, none)."""


def build_prompt(pr: str, receipts: list[dict], *, outcome_aware: bool) -> str:
    lines = [RUBRIC, "", f"Pull request: {pr}",
             "Stage B (outcome-aware)." if outcome_aware else "Stage A (blind to later outcome).",
             "For every BLOCK receipt below, reply with exactly one line starting JUDGMENT_JSON: "
             "followed by a JSON object {\"receipts\": [{\"round\": n, \"partial\": bool, "
             "\"unresolved_reason\": str|null, \"findings\": [{\"category\":..., \"outcome\":..., "
             "\"class\":..., \"severity\":..., \"mechanism\":..., \"prevent_with\":...}]}]}.", ""]
    for r in receipts:
        lines.append(f"--- receipt round {r['round']} head {r.get('head')} ---")
        lines.append(r.get("text") or "(finding text not retained)")
    return "\n".join(lines)


def parse_judgment(output: str) -> dict:
    m = _BLOCK.search(output.strip())
    if not m:
        raise ValueError("no JUDGMENT_JSON line")
    data = json.loads(m.group(1))
    validate(data)
    return data


def validate(data: dict) -> None:
    """Reject any reply shape the aggregation cannot trust. Every container and
    field is type-checked and a failure is always a ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("receipts"), list):
        raise ValueError("receipts must be a list")
    for r in data["receipts"]:
        if not isinstance(r, dict):
            raise ValueError("each receipt must be an object")
        if not isinstance(r.get("round"), int) or isinstance(r.get("round"), bool):
            raise ValueError("receipt round must be an int")
        if "partial" in r and not isinstance(r["partial"], bool):
            raise ValueError("partial must be a boolean")
        if not isinstance(r.get("findings", []), list):
            raise ValueError("findings must be a list")
        if r.get("unresolved_reason") is not None and not isinstance(r["unresolved_reason"], str):
            raise ValueError("unresolved_reason must be a string or null")
    rounds = [r["round"] for r in data["receipts"]]
    if len(set(rounds)) != len(rounds):
        raise ValueError("a round is judged more than once")
    for r in data["receipts"]:
        for f in r.get("findings", []):
            if not isinstance(f, dict):
                raise ValueError("each finding must be an object")
            if f.get("category") not in CATEGORIES:
                raise ValueError(f"bad category {f.get('category')!r}")
            if f.get("outcome") not in OUTCOMES:
                raise ValueError(f"bad outcome {f.get('outcome')!r}")
            if f.get("severity") not in SEVERITY_WEIGHT:
                raise ValueError(f"bad severity {f.get('severity')!r}")
            if f.get("class", "unknown") not in CLASSES:
                raise ValueError(f"bad class {f.get('class')!r}")
            if f.get("prevent_with", "none") not in PREVENT:
                raise ValueError(f"bad prevent_with {f.get('prevent_with')!r}")
            if not isinstance(f.get("mechanism"), str) or not f["mechanism"]:
                raise ValueError("finding needs a mechanism")


def agreement(a: dict, b: dict) -> float:
    """Share of receipts where two judges give the same receipt-level outcome."""
    from .core import receipt_outcome
    ra = {r["round"]: receipt_outcome(r.get("findings", []), bool(r.get("partial"))) for r in a["receipts"]}
    rb = {r["round"]: receipt_outcome(r.get("findings", []), bool(r.get("partial"))) for r in b["receipts"]}
    keys = set(ra) | set(rb)
    if not keys:
        return 1.0
    return sum(1 for k in keys if ra.get(k) == rb.get(k)) / len(keys)
