"""Review-loop core: freeze, sample, aggregate, backlog, render.

Pure functions over plain dicts so every step is replayable from the frozen
snapshot. Standard library only. The statistical design follows the reviewed
weekly-job plan (v6): PR is the sampling and analysis unit, a Hajek ratio
estimator gives the lower and upper bounds on the share of BLOCK receipts with
a confirmed warranted defect, and a Rao-Wu rescaled bootstrap gives the
sampling interval.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict

RUBRIC_VERSION = "v1"
HORIZON_DAYS = 14
BUDGET = 30
OPUS_CAP = 12
MIN_PRS_FOR_INTERVAL = 8
BOOTSTRAP_REPLICATES = 2000
OUTCOMES = ("defect", "defect_minor", "policy_gate", "process_slip", "over_strict", "unclear")
CATEGORIES = (
    "security-access", "money-spend", "concurrency-state", "data-contract",
    "error-handling", "api-misuse", "spec-drift", "docs-claim", "missing-tests",
    "logic-bug", "ci-gate", "scope-hygiene",
)
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------- helpers

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$")


def parse_time(value: str) -> dt.datetime:
    """Parse a UTC timestamp with an optional fractional part of any length."""
    m = _TIME.match(value)
    if not m:
        raise ValueError(f"unsupported timestamp {value!r}")
    micro = int((m.group(2) or "0")[:6].ljust(6, "0"))
    return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        microsecond=micro, tzinfo=dt.timezone.utc)


def normalize_time(value: str) -> str:
    """Canonical UTC string with a fixed nine-digit fraction, so string order is
    time order at full precision."""
    m = _TIME.match(value)
    if not m:
        raise ValueError(f"unsupported timestamp {value!r}")
    return f"{m.group(1)}.{(m.group(2) or '0')[:9].ljust(9, '0')}Z"


def iso_week(t: dt.datetime) -> str:
    year, week, _ = t.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week: str) -> tuple[dt.datetime, dt.datetime]:
    """Monday 00:00 UTC (inclusive) and the following Monday (exclusive)."""
    year, w = week.split("-W")
    monday = dt.datetime.fromisocalendar(int(year), int(w), 1).replace(tzinfo=dt.timezone.utc)
    return monday, monday + dt.timedelta(days=7)


def normalize_identity(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.split("/")[-1].split(":")[-1]


def lab_of(identity: str) -> str:
    low = identity.lower()
    if any(k in low for k in ("claude", "opus", "sonnet", "fable", "haiku")):
        return "anthropic"
    if any(k in low for k in ("gpt", "sol", "astra", "codex")):
        return "openai"
    return "other"


def is_opus(identity: str) -> bool:
    return "opus" in identity.lower()


# ------------------------------------------------------------------ freeze

def load_receipts(rows: list[dict]) -> list[dict]:
    """Reduce raw receipt JSON to the fields the job reads. Drops rows that
    cannot be attributed to a PR; callers count the drops."""
    out = []
    for d in rows:
        if not d.get("repo") or d.get("pr") is None or not d.get("created_at"):
            continue
        try:
            t = normalize_time(d["created_at"])
        except (ValueError, TypeError):
            continue
        out.append({
            "pr": f"{d['repo']}#{d['pr']}",
            "head": d.get("head_sha"),
            "base": d.get("base_sha"),
            "reviewer": normalize_identity(d.get("reviewer_identity")),
            "producer": normalize_identity(d.get("producer_identity")),
            "verdict": d.get("verdict"),
            "t": t,
            "dispatch_id": d.get("dispatch_id"),
        })
    out.sort(key=lambda r: (r["pr"], r["t"], r["head"] or "", r["dispatch_id"] or ""))
    # round = position within the PR timeline
    seen: Counter = Counter()
    for r in out:
        seen[r["pr"]] += 1
        r["round"] = seen[r["pr"]]
    return out


def freeze(receipts: list[dict], week: str, run_at: dt.datetime, *, code_version: str,
           rubric_version: str = RUBRIC_VERSION, judge_prompt_version: str = "j1") -> dict:
    """Build the cohort population for one ISO week.

    Cohort: PRs whose first review of any verdict falls inside the week.
    Frame: cohort PRs with at least one BLOCK dated before week close + horizon.
    N counts the frame PRs' BLOCK receipts inside the horizon. Raises if the
    horizon has not elapsed.
    """
    start, end = week_bounds(week)
    horizon_end = end + dt.timedelta(days=HORIZON_DAYS)
    if run_at < horizon_end:
        raise ValueError(f"horizon not elapsed: week {week} can run at {horizon_end:%Y-%m-%dT%H:%M:%SZ}")
    by_pr: dict[str, list[dict]] = defaultdict(list)
    for r in receipts:
        by_pr[r["pr"]].append(r)
    cohort, frame, late = [], [], 0
    for pr in sorted(by_pr):
        rows = sorted(by_pr[pr], key=lambda r: (r["t"], r["round"]))
        first = parse_time(rows[0]["t"])
        if not (start <= first < end):
            continue
        cohort.append(pr)
        blocks = [r for r in rows if r["verdict"] == "BLOCK" and parse_time(r["t"]) < horizon_end]
        late += sum(1 for r in rows if r["verdict"] == "BLOCK" and parse_time(r["t"]) >= horizon_end)
        if not blocks:
            continue
        first_block = blocks[0]
        frame.append({
            "pr": pr,
            "reviewer": first_block["reviewer"],
            "producer": first_block["producer"],
            "blocks": [{k: b[k] for k in ("round", "head", "base", "t", "reviewer", "producer")} for b in blocks],
        })
    population = {
        "week": week,
        "cohort_prs": cohort,
        "frame": frame,
        "n_block_receipts": sum(len(f["blocks"]) for f in frame),
        "late_rounds": late,
    }
    population_sha = sha256_text(canonical_json(population))
    seed = int(sha256_text(f"{week}:{population_sha}")[:16], 16)
    manifest = {
        "week": week,
        "population_sha256": population_sha,
        "bootstrap_seed": seed,
        "code_version": code_version,
        "rubric_version": rubric_version,
        "judge_prompt_version": judge_prompt_version,
        "horizon_days": HORIZON_DAYS,
        "budget": BUDGET,
        "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return {"manifest": manifest, "population": population}


# ------------------------------------------------------------------ sample

def _stratum_key(pr: dict) -> tuple[str, str]:
    return (pr["reviewer"], pr["producer"])


def _largest_remainder(shares: dict, total: int, caps: dict) -> dict:
    """Allocate `total` extra units proportional to `shares`, capped, deterministic."""
    alloc = {k: 0 for k in shares}
    remaining = total
    while remaining > 0:
        open_keys = [k for k in sorted(shares) if alloc[k] < caps[k]]
        if not open_keys:
            break
        weight = sum(shares[k] for k in open_keys)
        quotas = {k: remaining * shares[k] / weight for k in open_keys}
        floors = {k: min(int(math.floor(quotas[k])), caps[k] - alloc[k]) for k in open_keys}
        given = sum(floors.values())
        for k in open_keys:
            alloc[k] += floors[k]
        remaining -= given
        if remaining <= 0:
            break
        order = sorted(open_keys, key=lambda k: (-(quotas[k] - math.floor(quotas[k])), k))
        progressed = False
        for k in order:
            if remaining > 0 and alloc[k] < caps[k]:
                alloc[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return alloc


def allocate(frame: list[dict]) -> dict:
    """Stratum allocation per the plan's precedence (a)-(e). Returns
    {stratum_label: {"members": [pr...], "n": int, "pooled": bool, "certainty": bool}}
    plus the budget actually used. Deterministic."""
    if len(frame) <= BUDGET:
        strata = defaultdict(list)
        for pr in frame:
            strata["|".join(_stratum_key(pr))].append(pr["pr"])
        return {"budget_used": len(frame), "budget_raised": False, "take_all": True,
                "strata": {k: {"members": sorted(v), "n": len(v), "pooled": False, "certainty": True}
                           for k, v in sorted(strata.items())}}
    groups: dict[str, dict] = {}
    for pr in frame:
        label = "|".join(_stratum_key(pr))
        g = groups.setdefault(label, {"members": [], "reviewer": pr["reviewer"], "pooled": False})
        g["members"].append(pr["pr"])
    for g in groups.values():
        g["members"].sort()
    opus = {k: g for k, g in groups.items() if is_opus(g["reviewer"])}
    other = {k: g for k, g in groups.items() if k not in opus}
    opus_total = sum(len(g["members"]) for g in opus.values())
    opus_certainty = opus_total <= OPUS_CAP
    opus_take = opus_total if opus_certainty else max(OPUS_CAP, len(opus))

    def mandatory(m: int, strata: dict) -> int:
        return sum(min(len(g["members"]), m) for g in strata.values())

    def pick_minimum(strata: dict) -> int | None:
        for m in (3, 2, 1):
            if opus_take + mandatory(m, strata) <= BUDGET:
                return m
        return None

    m = pick_minimum(other)
    if m is None:
        # (c) pool strata with fewer than 3 PRs into one stratum per reviewer lab
        pooled_other: dict[str, dict] = {}
        for k, g in sorted(other.items()):
            if len(g["members"]) < 3:
                lab = f"other|{lab_of(g['reviewer'])}"
                p = pooled_other.setdefault(lab, {"members": [], "reviewer": g["reviewer"], "pooled": True})
                p["members"].extend(g["members"])
            else:
                pooled_other[k] = g
        for g in pooled_other.values():
            g["members"].sort()
        other = pooled_other
        m = pick_minimum(other)
    budget = BUDGET
    raised = False
    if m is None:
        m = 1
        budget = opus_take + mandatory(1, other)
        raised = True
    alloc: dict[str, int] = {}
    caps: dict[str, int] = {}
    for k, g in opus.items():
        if opus_certainty:
            alloc[k] = len(g["members"])
        else:
            alloc[k] = min(len(g["members"]), 1)
        caps[k] = len(g["members"]) if opus_certainty else len(g["members"])
    for k, g in other.items():
        alloc[k] = min(len(g["members"]), m)
        caps[k] = len(g["members"])
    used = sum(alloc.values())
    if not opus_certainty:
        # opus extra draws share the OPUS_CAP; the rest share the remaining budget
        opus_extra = max(0, opus_take - sum(alloc[k] for k in opus))
        extra = _largest_remainder({k: len(opus[k]["members"]) for k in opus}, opus_extra,
                                   {k: caps[k] - alloc[k] for k in opus})
        for k, v in extra.items():
            alloc[k] += v
        used = sum(alloc.values())
    remaining = max(0, budget - used)
    free = {k: g for k, g in other.items() if alloc[k] < caps[k]}
    if remaining and free:
        extra = _largest_remainder({k: len(free[k]["members"]) for k in free}, remaining,
                                   {k: caps[k] - alloc[k] for k in free})
        for k, v in extra.items():
            alloc[k] += v
    strata_out = {}
    for k, g in sorted({**opus, **other}.items()):
        strata_out[k] = {
            "members": g["members"], "n": alloc[k], "pooled": bool(g.get("pooled")),
            "certainty": alloc[k] >= len(g["members"]),
        }
    return {"budget_used": sum(alloc.values()), "budget_raised": raised, "take_all": False,
            "strata": strata_out}


def draw(frame: list[dict], seed: int) -> dict:
    """Draw the sample. Returns sample rows with weights w = N_h / n_h."""
    plan = allocate(frame)
    rng = random.Random(seed)
    rounds = {f["pr"]: [b["round"] for b in f["blocks"]] for f in frame}
    rows = []
    for label, s in plan["strata"].items():
        members = list(s["members"])
        chosen = members if s["n"] >= len(members) else sorted(rng.sample(members, s["n"]))
        weight = len(members) / len(chosen) if chosen else 0.0
        for pr in chosen:
            rows.append({"pr": pr, "stratum": label, "weight": weight,
                         "n_blocks": len(rounds.get(pr, [])), "rounds": rounds.get(pr, []),
                         "stratum_size": len(members), "stratum_n": len(chosen),
                         "pooled": s["pooled"]})
    rows.sort(key=lambda r: r["pr"])
    return {"allocation": plan, "sample": rows}


# --------------------------------------------------------------- aggregate

def receipt_outcome(findings: list[dict], partial: bool) -> str:
    """Receipt outcome from atomic finding outcomes, per plan step 4."""
    outs = [f["outcome"] for f in findings]
    if "defect" in outs:
        return "defect"
    if partial:
        return "unresolved"
    if "defect_minor" in outs:
        return "defect_minor"
    if outs and all(o == "policy_gate" for o in outs):
        return "policy_gate"
    for o in ("process_slip", "over_strict", "unclear"):
        if o in outs:
            return o
    return "unclear"


def _stats(units: list[dict]) -> tuple[float, float, float, float]:
    den = sum(u["w"] * u["total"] for u in units)
    conf = sum(u["w"] * u["confirmed"] for u in units)
    unres = sum(u["w"] * u["unresolved"] for u in units)
    return den, conf, unres, 0.0


def _bounds(units: list[dict]) -> tuple[float, float]:
    den, conf, unres, _ = _stats(units)
    if den <= 0:
        return 0.0, 0.0
    return conf / den, (conf + unres) / den


def check_judgments(sample: list[dict], judgments: dict) -> None:
    """Reject judgments that do not match the frozen sample: a PR that was not
    sampled, a round that is not one of that PR's BLOCK receipts, or a round
    judged twice. Keys starting with an underscore are metadata."""
    expected = {}
    for s in sample:
        if "rounds" not in s:
            raise ValueError(f"sample row for {s['pr']} lacks its BLOCK rounds")
        expected[s["pr"]] = list(s["rounds"])
    for pr in judgments:
        if pr.startswith("_"):
            continue
        if pr not in expected:
            raise ValueError(f"judgment for {pr}, which was not sampled")
        seen: set = set()
        for r in judgments[pr].get("receipts", []):
            rd = r.get("round")
            if rd in seen:
                raise ValueError(f"{pr}: round {rd} judged more than once")
            seen.add(rd)
            if rd not in expected[pr]:
                raise ValueError(f"{pr}: round {rd} is not a BLOCK receipt of the frozen sample")


def build_units(sample: list[dict], judgments: dict) -> list[dict]:
    """One unit per sampled PR: its weight, BLOCK-receipt count, confirmed-defect
    receipts and unresolved receipts without a confirmed defect. Expected rounds
    with no judgment count as unresolved."""
    check_judgments(sample, judgments)
    units = []
    for s in sample:
        j = judgments.get(s["pr"])
        receipts = j["receipts"] if j else []
        total = len(s["rounds"])
        confirmed = unresolved = 0
        for r in receipts:
            outcome = receipt_outcome(r.get("findings", []), bool(r.get("partial")))
            if outcome == "defect":
                confirmed += 1
            elif outcome == "unresolved":
                unresolved += 1
        unresolved += total - len(receipts)
        if confirmed + unresolved > total:
            raise ValueError(f"{s['pr']}: {confirmed} confirmed + {unresolved} unresolved exceeds {total} receipts")
        units.append({"pr": s["pr"], "stratum": s["stratum"], "w": s["weight"], "total": total,
                      "confirmed": confirmed, "unresolved": unresolved, "pooled": s.get("pooled", False),
                      "stratum_size": s["stratum_size"], "stratum_n": s["stratum_n"]})
    return units


def _analysis_strata(units: list[dict]) -> dict:
    """Analysis-only pooling (weights preserved): a stratum with one sampled PR out
    of several joins its reviewer lab's pool; a lab pool with one PR joins a
    global pool. Population singletons and certainty strata keep zero variance.
    Keys are namespaced ("stratum:", "lab-pool:", "global") so an analysis pool
    can never replace an allocation stratum, and every unit appears once."""
    strata: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        strata[u["stratum"]].append(u)
    out: dict[str, list[dict]] = {}
    lab_pool: dict[str, list[dict]] = defaultdict(list)
    for k in sorted(strata):
        members = strata[k]
        n, size = len(members), members[0]["stratum_size"]
        if n == 1 and size > 1:
            lab_pool[lab_of(k.split("|")[0])].extend(members)
        else:
            out[f"stratum:{k}"] = members
    glob: list[dict] = []
    for lab in sorted(lab_pool):
        if len(lab_pool[lab]) >= 2:
            out[f"lab-pool:{lab}"] = lab_pool[lab]
        else:
            glob.extend(lab_pool[lab])
    if glob:
        out["global"] = glob
    return out


def bootstrap_interval(units: list[dict], seed: int, replicates: int = BOOTSTRAP_REPLICATES) -> dict:
    """Rao-Wu rescaled bootstrap with finite-population correction. Both L and U
    are recomputed in every replicate."""
    strata = _analysis_strata(units)
    rng = random.Random(seed)
    ls, us = [], []
    for _ in range(replicates):
        scaled = []
        for k in sorted(strata):
            members = strata[k]
            n = len(members)
            pop = members[0]["stratum_size"] if len(set(m["stratum"] for m in members)) == 1 else \
                sum({m["stratum"]: m["stratum_size"] for m in members}.values())
            f = min(1.0, n / pop) if pop else 1.0
            if n < 2 or f >= 1.0:
                scaled.extend(dict(m) for m in members)
                continue
            m_h = n - 1
            lam = math.sqrt(m_h * (1.0 - f) / (n - 1))
            counts = Counter(rng.randrange(n) for _ in range(m_h))
            for i, m in enumerate(members):
                factor = 1.0 - lam + lam * (n / m_h) * counts.get(i, 0)
                mm = dict(m)
                mm["w"] = m["w"] * factor
                scaled.extend([mm])
        lo, up = _bounds(scaled)
        ls.append(lo)
        us.append(up)
    ls.sort()
    us.sort()

    def pct(vs, q):
        idx = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
        return vs[idx]

    return {"L": [pct(ls, 0.025), pct(ls, 0.975)], "U": [pct(us, 0.025), pct(us, 0.975)],
            "replicates": replicates, "strata": {k: len(v) for k, v in sorted(strata.items())}}


def aggregate(sample: list[dict], judgments: dict, manifest: dict, population: dict) -> dict:
    units = build_units(sample, judgments)
    lower, upper = _bounds(units)
    den = sum(u["w"] * u["total"] for u in units)
    unres = sum(u["w"] * u["unresolved"] for u in units)
    resolved_den = den - unres - 0.0
    # judged-only (conditional): confirmed defects on resolved receipts only
    conf_resolved = 0.0
    partial_confirmed = 0
    reasons: Counter = Counter()
    outcomes: Counter = Counter()
    for s in sample:
        j = judgments.get(s["pr"])
        for r in (j["receipts"] if j else []):
            out = receipt_outcome(r.get("findings", []), bool(r.get("partial")))
            outcomes[out] += 1
            if r.get("partial"):
                if out == "defect":
                    partial_confirmed += 1
                    reasons["partial_with_confirmed_defect"] += 1
                else:
                    reasons[r.get("unresolved_reason") or "unspecified"] += 1
            elif out == "defect":
                conf_resolved += s["weight"]
    judged_only = conf_resolved / resolved_den if resolved_den > 0 else None
    result = {
        "week": manifest["week"],
        "n_block_receipts_population": population["n_block_receipts"],
        "weighted_denominator": den,
        "denominator_gap_vs_N": den - population["n_block_receipts"],
        "sampled_prs": len(sample),
        "L": lower,
        "U": upper,
        "judged_only_conditional": judged_only,
        "confirmed_on_partial_receipts": partial_confirmed,
        "unresolved_reasons": dict(sorted(reasons.items())),
        "receipt_outcomes": dict(sorted(outcomes.items())),
        "interval": None,
    }
    if len(sample) >= MIN_PRS_FOR_INTERVAL:
        result["interval"] = bootstrap_interval(units, manifest["bootstrap_seed"])
    return result


# ----------------------------------------------------------------- backlog

def mechanism_counts(sample: list[dict], judgments: dict) -> dict:
    """Atomic findings with outcome defect/defect_minor, once per (PR, mechanism)."""
    seen: dict[tuple, dict] = {}
    for s in sample:
        j = judgments.get(s["pr"])
        for r in (j["receipts"] if j else []):
            for f in r.get("findings", []):
                if f["outcome"] not in ("defect", "defect_minor"):
                    continue
                key = (s["pr"], f["category"], f["mechanism"])
                prev = seen.get(key)
                if prev is None or SEVERITY_WEIGHT[f["severity"]] > SEVERITY_WEIGHT[prev["severity"]]:
                    seen[key] = {"pr": s["pr"], "category": f["category"], "mechanism": f["mechanism"],
                                 "severity": f["severity"], "prevent_with": f.get("prevent_with", "none")}
    by_mech: dict[tuple, list[dict]] = defaultdict(list)
    for v in seen.values():
        by_mech[(v["category"], v["mechanism"])].append(v)
    out = {}
    for (cat, mech), items in sorted(by_mech.items()):
        prs = sorted({i["pr"] for i in items})
        out[f"{cat}: {mech}"] = {
            "category": cat, "mechanism": mech, "defects": len(items), "prs": prs,
            "weight": sum(SEVERITY_WEIGHT[i["severity"]] for i in items),
            "high": sum(1 for i in items if i["severity"] == "high"),
            "prevent_with": Counter(i["prevent_with"] for i in items).most_common(1)[0][0],
        }
    return out


RELAXATION_PHRASES = (
    "return notes", "pass when", "skip the check", "skip verification", "relax", "downgrade",
    "allow without review", "ignore the failing", "waive",
)


def backlog_proposals(mechs: dict, changes: dict | None = None) -> list[dict]:
    """A mechanism needs 3+ defects across 2+ PRs, or any severity-high defect.
    A proposal whose stated change would relax a check is rejected, not filed."""
    out = []
    for key, m in mechs.items():
        if not ((m["defects"] >= 3 and len(m["prs"]) >= 2) or m["high"] >= 1):
            continue
        text = (changes or {}).get(key, "")
        rejected = [p for p in RELAXATION_PHRASES if p in text.lower()]
        out.append({"mechanism": key, "defects": m["defects"], "prs": m["prs"], "weight": m["weight"],
                    "high": m["high"], "prevent_with": m["prevent_with"],
                    "rejected_by_no_relaxation_rule": rejected})
    out.sort(key=lambda p: (-p["weight"], -len(p["prs"]), p["mechanism"]))
    return out


# ------------------------------------------------------------------ render

def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_report(manifest: dict, population: dict, plan: dict, agg: dict, mechs: dict,
                  proposals: list[dict], recoverable: dict | None = None) -> str:
    lines = [
        f"# Review-loop report {manifest['week']}",
        "",
        f"snapshot {manifest['population_sha256']} | rubric {manifest['rubric_version']} | "
        f"judge prompt {manifest['judge_prompt_version']} | code {manifest['code_version']} | "
        f"run {manifest['run_at']}",
        "",
        "## Headline (Stage A labels; nominal caveats apply)",
        f"- BLOCK receipts in the cohort frame: {agg['n_block_receipts_population']} "
        f"(late rounds not estimated: {population['late_rounds']}); sampled PRs: {agg['sampled_prs']} "
        f"of {len(population['frame'])}.",
        f"- Share with a confirmed warranted defect: lower bound {_pct(agg['L'])}, upper bound {_pct(agg['U'])} "
        "(Hajek ratio, same denominator; upper adds unresolved receipts).",
    ]
    if agg["interval"]:
        i = agg["interval"]
        lines.append(f"- Sampling interval (Rao-Wu bootstrap, {i['replicates']} replicates): "
                     f"lower bound {_pct(i['L'][0])} to {_pct(i['L'][1])}; upper bound "
                     f"{_pct(i['U'][0])} to {_pct(i['U'][1])}.")
    else:
        lines.append(f"- Fewer than {MIN_PRS_FOR_INTERVAL} sampled PRs: no interval is printed, counts and bounds only.")
    lines += [
        f"- Conditional on judged receipts only (not the headline): {_pct(agg['judged_only_conditional'])}; "
        f"confirmed defects on partial receipts disclosed separately: {agg['confirmed_on_partial_receipts']}.",
        f"- Weighted denominator gap versus N: {agg['denominator_gap_vs_N']:.2f}.",
        f"- Unresolved reasons: {json.dumps(agg['unresolved_reasons'], sort_keys=True)}",
        f"- Receipt outcomes: {json.dumps(agg['receipt_outcomes'], sort_keys=True)}",
    ]
    if recoverable is not None:
        lines.append(f"- Finding text recovered: {recoverable['recovered']} of {recoverable['total']} sampled receipts.")
    lines += ["", "## Sampling", f"- Budget used {plan['budget_used']}" +
              (" (raised above 30 to the mandatory allocation)" if plan["budget_raised"] else "") +
              (" (frame at most 30: all PRs taken)" if plan["take_all"] else "")]
    for label, s in plan["strata"].items():
        flags = (" pooled" if s["pooled"] else "") + (" certainty" if s["certainty"] else "")
        lines.append(f"- {label}: {s['n']} of {len(s['members'])}{flags}")
    lines += ["", "## Mechanisms (defects once per PR and mechanism)"]
    if not mechs:
        lines.append("- none")
    for key, m in mechs.items():
        lines.append(f"- {key}: {m['defects']} defects, {len(m['prs'])} PRs, weight {m['weight']}, "
                     f"high {m['high']}, prevent with {m['prevent_with']}")
    lines += ["", "## Backlog proposals (the job files nothing; the steward files beads)"]
    if not proposals:
        lines.append("- none meet the threshold")
    for p in proposals:
        rej = f" REJECTED by the no-relaxation rule ({', '.join(p['rejected_by_no_relaxation_rule'])})" \
            if p["rejected_by_no_relaxation_rule"] else ""
        lines.append(f"- {p['mechanism']}: {p['defects']} defects across {len(p['prs'])} PRs, "
                     f"weight {p['weight']}{rej}")
    lines += [
        "", "## Limits",
        "- A PR is the sampling unit; rounds on one PR are clustered. Bounds cover unresolved receipts; "
        "they do not cover judge error or missing specifications.",
        "- Nothing here ranks reviewer or author models.",
        "- Finding-text capture recovers the evidence going forward, not historical evidence.",
        "",
    ]
    return "\n".join(lines)


def digest_block(manifest: dict, agg: dict, proposals: list[dict], recoverable: dict | None) -> str:
    top = [p["mechanism"] for p in proposals if not p["rejected_by_no_relaxation_rule"]][:3]
    rec = f"{recoverable['recovered']}/{recoverable['total']}" if recoverable else "n/a"
    lines = [
        f"review-loop {manifest['week']} (rubric {manifest['rubric_version']})",
        f"BLOCK receipts {agg['n_block_receipts_population']}, sampled PRs {agg['sampled_prs']}",
        f"confirmed-defect share: lower {_pct(agg['L'])}, upper {_pct(agg['U'])}",
        f"unresolved: {sum(agg['unresolved_reasons'].values())} receipts; text recovered {rec}",
        "top mechanisms: " + ("; ".join(top) if top else "none over threshold"),
    ]
    return "\n".join(lines[:10]) + "\n"
