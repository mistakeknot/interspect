#!/usr/bin/env python3
"""Weighted per-skill scoring for the Interspect skill calibrator (sylveste-7aj8.5).

The join point of the skill-calibration pipeline: turns the per-skill SIGNALS
(written by ``scripts/signals/collect_*.py``) and the per-skill GOAL WEIGHTS
(written by ``scripts/infer-skill-goals.py``) into a single composite score per
skill, then writes a ``skills`` block into ``routing-calibration.json``
sibling to the agent ``agents`` block.

Why a sibling script (not an extension of calibrate-audit.py)
─────────────────────────────────────────────────────────────
``scripts/calibrate-audit.py`` is a *drift-report* tool: it diffs the current
``routing-calibration.json`` against an old snapshot and emits a markdown audit.
It never computes or writes scores — agent score-writing lives in
``hooks/lib-interspect.sh::_interspect_write_routing_calibration``. So the
natural sibling for skill *scoring* is the skill pipeline itself
(``ingest-skill-audit.py`` → ``signals/collect_*`` → ``infer-skill-goals.py`` →
this). This script mirrors those siblings' conventions verbatim: the same
``find_repo_root`` / ``default_db_path`` discovery, sqlite3 ``?`` placeholders,
``--db``/``--dry-run``/``--repo-root`` CLI surface, and the SAME
calibration-history snapshot machinery the agent writer uses (so skill scores get
the same drift tracking calibrate-audit can later read).

Algorithm
─────────
1. Qualifying skills: >= ``--min-invocations`` (default 10) distinct skill
   invocations in the trailing ``--window-days`` (default 30) — counted as
   distinct ``evidence.source_event_id`` where ``source_kind='skill'`` within the
   window.
2. Per-signal aggregate: recency-weighted mean of each signal_kind's
   ``skill_signals.value`` over the window (see RECENCY DECAY below). Each
   aggregate is already in [0,1] because collectors normalize to [0,1].
3. Signal → goal mapping (matches infer-skill-goals.SIGNAL_GOAL_MAP):
       tokens      → speed         (structural weight 1.0)
       error       → precision     (structural weight 1.0)
       no_redirect → precision     (structural weight 0.5, so it doesn't double-count error)
       bead_close  → completeness  (structural weight 1.0)
   VARIANCE-AWARE WEIGHTING (sylveste-ysny, default on): each signal_kind's
   structural weight is multiplied by a cohort INFORMATION WEIGHT in [0,1] derived
   from how much that signal VARIES across the scored skills (population stddev →
   clamp(sigma/DISPERSION_REF)). A saturated signal (e.g. the structurally-1.0
   `error`) gets info weight ~0 and contributes ~nothing to its goal, so the
   varying signal (`no_redirect`) dominates precision instead of being drowned.
   The per-signal EFFECTIVE weight is structural_weight * info_weight; no_redirect's
   0.5x structural de-weight is kept as a SEPARATE factor. Disable with
   ``--static-weights`` to recover the legacy static composite. See
   ``compute_signal_info_weights`` for the formula + fallbacks (single-skill cohort
   and all-saturated-goal both fall back to static weights).
   Per-goal value = (effective-)weighted mean of its mapped signals that are PRESENT
   (missing signals drop out — the goal is scored on what it has).
4. Composite: score = Σ_k goal_weights[k] * goal_value[k], renormalized over the
   goals that actually have a value (so a skill missing one whole goal still
   scores on the goals it has). goal_weights come from ``skill_goals``; absent →
   uniform {1/3,1/3,1/3} with goal_source='uniform'.
5. Write a ``skills`` block (schema_version bumped to 3, additively) and a
   calibration-history snapshot.

RECENCY DECAY
─────────────
No decay helper exists in calibrate-audit.py (it is a diff tool, not an
aggregator), so we define one here: an EXPONENTIAL half-life weight on signal
age. A signal observed ``age`` days before ``now`` gets weight
``0.5 ** (age / half_life)`` with ``half_life = --half-life-days`` (default 14).
The recency-weighted mean is ``Σ w_i v_i / Σ w_i``. Half-life 14d means a signal
two weeks old counts half as much as a fresh one; at the 30-day window edge a
signal counts ~0.23x. This is documented and ``--half-life-days``-overridable;
pass a very large half-life to recover a plain unweighted mean.

CLI
───
  score-skills.py [--db PATH] [--window-days 30] [--min-invocations 10]
                  [--half-life-days 14] [--dry-run] [--json] [--repo-root .]

Exit 0 always on success (this is a calibration writer, not a gate).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


# ─── Repo-root / DB discovery (mirrors ingest-skill-audit.py) ────────────────


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".clavain").exists() or (p / ".git").exists():
            return p
        p = p.parent
    return start.resolve()


def default_db_path(repo_root: Path) -> Path:
    return repo_root / ".clavain" / "interspect" / "interspect.db"


# ─── Signal → goal mapping ───────────────────────────────────────────────────
#
# (signal_kind, goal, weight). Mirrors infer-skill-goals.SIGNAL_GOAL_MAP but
# carries the explicit 0.5x de-weight for no_redirect so it does not double-count
# with error inside the precision goal.
SIGNAL_GOAL: dict[str, tuple[str, float]] = {
    "tokens": ("speed", 1.0),
    "error": ("precision", 1.0),
    "no_redirect": ("precision", 0.5),
    "bead_close": ("completeness", 1.0),
}

GOAL_KEYS = ("speed", "precision", "completeness")
UNIFORM_WEIGHTS = {k: 1.0 / 3.0 for k in GOAL_KEYS}

# ─── Variance-aware signal weighting (sylveste-ysny) ─────────────────────────
#
# PROBLEM. A signal that is STRUCTURALLY SATURATED (identical for ~every skill)
# carries no information yet still pulls its mapped goal toward a constant. The
# `error` signal is the canonical case: the PostToolUse telemetry has no real
# failure data (exit_code is always 0), so `error` aggregates to ~1.0 for every
# skill. At full weight it pins `precision` near 1.0 for everyone and flattens
# the composite, drowning out `no_redirect` — the one precision signal with real
# behavioral variance.
#
# FIX. Scale each signal_kind's contribution by how much it actually VARIES
# across the scored cohort. We measure dispersion as the POPULATION standard
# deviation of a signal's per-skill aggregate values, then map it to an
# "information weight" in [0,1]:
#
#     info_weight(sigma) = clamp(sigma / DISPERSION_REF, 0, 1)
#
# A linear ramp (not exp) was chosen for auditability — the weight reads off as
# "this signal's spread relative to a reference spread". DISPERSION_REF is the
# stddev at which a signal earns FULL weight; below DISPERSION_EPS the signal is
# treated as fully saturated and gets weight 0 (the eps guards float noise so a
# signal that is *exactly* constant, or constant to ~1e-6, scores 0 rather than a
# tiny non-zero ramp value).
#
# REFERENCE CHOICE. Signal aggregates live in [0,1]. A signal split evenly
# between 0 and 1 across the cohort has population stddev 0.5 (the maximum for a
# two-point [0,1] distribution); a signal uniformly spread over [0,1] has stddev
# ~0.29. DISPERSION_REF = 0.15 means a signal needs a stddev of ~0.15 (a
# moderate, clearly-non-saturated spread — e.g. values ranging ~0.3 wide) to earn
# full weight, and partial weight ramps linearly below that. This keeps a
# genuinely-varying signal at or near full weight while crushing a saturated one
# to ~0.
#
# The information weight is a SEPARATE multiplicative factor folded on top of the
# existing structural SIGNAL_GOAL weights (e.g. no_redirect keeps its 0.5x
# structural de-weight; its EFFECTIVE weight inside precision becomes
# 0.5 * info_weight(no_redirect)). See effective_signal_weight().
DISPERSION_REF = 0.15  # stddev at which a signal earns full information weight
DISPERSION_EPS = 1e-6  # below this stddev a signal is "saturated" → weight 0


def population_stddev(values: list[float]) -> float:
    """Population standard deviation of a non-empty value list (0.0 if <2)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


def info_weight_from_dispersion(sigma: float) -> float:
    """Map a per-signal dispersion (stddev) to an information weight in [0,1].

    ``clamp(sigma / DISPERSION_REF, 0, 1)`` with a hard 0 below DISPERSION_EPS
    (so an exactly/near-constant signal contributes nothing). A signal with
    dispersion >= DISPERSION_REF earns full weight 1.0.
    """
    if sigma < DISPERSION_EPS:
        return 0.0
    w = sigma / DISPERSION_REF
    if w < 0.0:
        return 0.0
    if w > 1.0:
        return 1.0
    return w


def compute_signal_info_weights(
    per_skill_aggs: list[dict[str, float]],
) -> dict[str, float]:
    """Cohort-level information weight per signal_kind, in [0,1].

    ``per_skill_aggs`` is one signal-aggregate dict per scored skill. For each
    signal_kind we collect its value across the skills THAT HAVE IT (a signal
    present for only some skills disperses over the skills that have it), compute
    the population stddev, and map it through info_weight_from_dispersion.

    FALLBACK (single scored skill / <2 values for a kind): population_stddev
    returns 0.0 → info weight 0.0, which would zero out every signal. The CALLER
    detects the "no dispersion computable" case (a cohort with fewer than 2
    skills) and uses static weights instead; this function never divides by zero.
    """
    by_kind: dict[str, list[float]] = {}
    for aggs in per_skill_aggs:
        for kind, value in aggs.items():
            by_kind.setdefault(kind, []).append(value)
    return {
        kind: info_weight_from_dispersion(population_stddev(vals))
        for kind, vals in by_kind.items()
    }


# Schema version for routing-calibration.json. Agent writer
# (_interspect_write_routing_calibration) emits 2; this adds the `skills`
# block additively, so we bump to 3. Downstream consumers ignore unknown fields.
SKILLS_SCHEMA_VERSION = 3


# ─── Recency decay ───────────────────────────────────────────────────────────


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO8601 ``observed_at`` (trailing 'Z' or offset) to aware UTC."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def recency_weight(observed_at: str, now: datetime, half_life_days: float) -> float:
    """Exponential half-life weight on signal age in days. Older → smaller.

    ``0.5 ** (age_days / half_life_days)``. A missing/unparseable ts gets a
    minimal positive weight (treated as maximally old but not zero) so the row
    still contributes without dominating.
    """
    dt = _parse_ts(observed_at)
    if dt is None:
        return 0.5 ** (3650.0 / max(half_life_days, 1e-9))  # ~10y old fallback
    age_days = (now - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0  # clock skew: a future ts counts as fresh
    return 0.5 ** (age_days / max(half_life_days, 1e-9))


# ─── DB queries ──────────────────────────────────────────────────────────────


def window_lower_bound(now: datetime, window_days: int) -> str:
    """ISO8601 'Z' lower bound = now - window_days. Matches stored ts format."""
    cutoff = now.timestamp() - window_days * 86400
    dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def qualifying_skills(
    conn: sqlite3.Connection, lower_bound: str, min_invocations: int
) -> list[tuple[str, int]]:
    """Skills with >= min_invocations distinct skill invocations in the window.

    Counts distinct ``evidence.source_event_id`` (the invocation_id) for
    ``source_kind='skill'`` rows whose ``ts`` is within the window. Returns
    [(skill_name, invocations_30d)] sorted by name for determinism.
    """
    rows = conn.execute(
        "SELECT source AS skill_name, "
        "       COUNT(DISTINCT source_event_id) AS n "
        "FROM evidence "
        "WHERE source_kind = 'skill' "
        "  AND source_event_id IS NOT NULL "
        "  AND ts >= ? "
        "GROUP BY source "
        "HAVING n >= ? "
        "ORDER BY source ASC",
        (lower_bound, min_invocations),
    ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


class SignalRow(NamedTuple):
    signal_kind: str
    value: float
    observed_at: str


def signal_rows(
    conn: sqlite3.Connection, skill_name: str, lower_bound: str
) -> list[SignalRow]:
    """All in-window skill_signals rows for one skill."""
    rows = conn.execute(
        "SELECT signal_kind, value, observed_at "
        "FROM skill_signals "
        "WHERE skill_name = ? AND observed_at >= ?",
        (skill_name, lower_bound),
    ).fetchall()
    out: list[SignalRow] = []
    for k, v, oa in rows:
        try:
            out.append(SignalRow(signal_kind=k, value=float(v), observed_at=oa or ""))
        except (TypeError, ValueError):
            continue
    return out


def load_goal_weights(
    conn: sqlite3.Connection, skill_name: str
) -> tuple[dict[str, float], str, str]:
    """Return (weights, goal_source, classified_from).

    Reads the ``skill_goals`` row; renormalizes its weights defensively. When no
    row (or unusable weights) → uniform {1/3,1/3,1/3}, goal_source='uniform'.
    """
    row = conn.execute(
        "SELECT goal_weights, classified_from FROM skill_goals WHERE skill_name = ?",
        (skill_name,),
    ).fetchone()
    if not row:
        return dict(UNIFORM_WEIGHTS), "uniform", "uniform"
    try:
        raw = json.loads(row[0])
        w = {k: float(raw[k]) for k in GOAL_KEYS}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return dict(UNIFORM_WEIGHTS), "uniform", "uniform"
    total = sum(w[k] for k in GOAL_KEYS)
    if total <= 0:
        return dict(UNIFORM_WEIGHTS), "uniform", "uniform"
    w = {k: w[k] / total for k in GOAL_KEYS}
    return w, "skill_goals", (row[1] or "")


# ─── Aggregation ─────────────────────────────────────────────────────────────


def aggregate_signals(
    rows: list[SignalRow], now: datetime, half_life_days: float
) -> dict[str, float]:
    """Recency-weighted mean per signal_kind → [0,1]. Only present kinds appear."""
    acc: dict[str, list[tuple[float, float]]] = {}  # kind -> [(weight, value)]
    for r in rows:
        w = recency_weight(r.observed_at, now, half_life_days)
        acc.setdefault(r.signal_kind, []).append((w, r.value))
    agg: dict[str, float] = {}
    for kind, wv in acc.items():
        wsum = sum(w for w, _ in wv)
        if wsum <= 0:
            # All weights vanished (shouldn't happen — weights are positive);
            # fall back to a plain mean so the signal is not silently dropped.
            agg[kind] = sum(v for _, v in wv) / len(wv)
        else:
            agg[kind] = sum(w * v for w, v in wv) / wsum
    return agg


def effective_signal_weight(
    kind: str, structural_weight: float, info_weights: dict[str, float] | None
) -> float:
    """Per-signal effective weight = structural * info_weight.

    When ``info_weights`` is None (static mode, or single-skill fallback) the
    effective weight is just the structural SIGNAL_GOAL weight (old behavior). A
    signal_kind absent from ``info_weights`` (present for this skill but with no
    cohort dispersion computed) also falls back to its structural weight.
    """
    if info_weights is None:
        return structural_weight
    iw = info_weights.get(kind)
    if iw is None:
        return structural_weight
    return structural_weight * iw


def signals_to_goals(
    signal_aggs: dict[str, float],
    info_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Map per-signal aggregates → per-goal values via SIGNAL_GOAL.

    Each goal value = weighted mean of its mapped signals THAT ARE PRESENT
    (missing signals drop out). Goals with no present signal are omitted (the
    composite renormalizes over present goals).

    When ``info_weights`` is provided (variance-aware mode), each mapped signal's
    structural SIGNAL_GOAL weight is multiplied by that signal's cohort
    information weight, so a saturated signal (info weight ~0) contributes ~nothing
    to its goal and the surviving signal dominates.

    SATURATION FALLBACK. If EVERY signal mapped into a goal is saturated (their
    info-weighted weights all sum to ~0), the goal would be undefined under the
    variance-aware weights. We then fall back to the STATIC (structural-only)
    weighted mean for that goal so it stays defined — better a saturated value
    than a dropped goal.
    """
    by_goal: dict[str, list[tuple[float, float, float]]] = {}  # (eff_w, static_w, value)
    for kind, value in signal_aggs.items():
        mapping = SIGNAL_GOAL.get(kind)
        if mapping is None:
            continue
        goal, structural_weight = mapping
        eff_w = effective_signal_weight(kind, structural_weight, info_weights)
        by_goal.setdefault(goal, []).append((eff_w, structural_weight, value))
    goals: dict[str, float] = {}
    for goal, items in by_goal.items():
        eff_sum = sum(ew for ew, _, _ in items)
        if eff_sum > 0:
            goals[goal] = sum(ew * v for ew, _, v in items) / eff_sum
            continue
        # Every mapped signal is saturated (info weight ~0) → fall back to the
        # static structural-weighted mean so the goal does not go undefined.
        static_sum = sum(sw for _, sw, _ in items)
        if static_sum > 0:
            goals[goal] = sum(sw * v for _, sw, v in items) / static_sum
    return goals


def composite_score(
    goal_values: dict[str, float], goal_weights: dict[str, float]
) -> float:
    """Σ_k weight[k] * value[k], renormalized over goals that have a value.

    Renormalization means a skill missing one whole goal still scores fairly on
    the goals it has (its present goal_weights are rescaled to sum to 1).
    """
    present = [k for k in GOAL_KEYS if k in goal_values]
    if not present:
        return 0.0
    wsum = sum(goal_weights.get(k, 0.0) for k in present)
    if wsum <= 0:
        # No weight mass on present goals — fall back to uniform over present.
        return sum(goal_values[k] for k in present) / len(present)
    return sum(goal_weights[k] * goal_values[k] for k in present) / wsum


# ─── Scoring ─────────────────────────────────────────────────────────────────


class SkillScore(NamedTuple):
    skill: str
    invocations_30d: int
    score: float
    signals: dict[str, float]
    goals: dict[str, float]
    goal_weights: dict[str, float]
    goal_source: str
    classified_from: str


def score_skills(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    window_days: int,
    min_invocations: int,
    half_life_days: float,
    static_weights: bool = False,
) -> tuple[list[SkillScore], dict[str, float]]:
    """Score qualifying skills; return (scores, signal_info_weights).

    TWO-PASS (variance-aware). Pass 1 collects every qualifying skill's per-signal
    aggregates + goal weights. Pass 2 computes the cohort-level information weight
    per signal_kind (population stddev of its values across skills → [0,1]) and
    folds it into the signal→goal aggregation, so a saturated signal contributes
    ~nothing to its goal.

    FALLBACKS:
      * ``static_weights=True``  → skip variance adjustment entirely; reproduce the
        original static-weight composite (info_weights = {} reported, info_used=False).
      * fewer than 2 scored skills → no dispersion is computable (stddev needs >=2
        points), so fall back to static weights to avoid zeroing every signal.
    """
    lower_bound = window_lower_bound(now, window_days)

    # Pass 1: collect per-skill aggregates + metadata.
    collected: list[tuple[str, int, dict[str, float], dict[str, float], str, str]] = []
    for skill_name, n_inv in qualifying_skills(conn, lower_bound, min_invocations):
        rows = signal_rows(conn, skill_name, lower_bound)
        signal_aggs = aggregate_signals(rows, now, half_life_days)
        weights, goal_source, classified_from = load_goal_weights(conn, skill_name)
        collected.append(
            (skill_name, n_inv, signal_aggs, weights, goal_source, classified_from)
        )

    # Decide whether variance-aware weighting applies.
    per_skill_aggs = [c[2] for c in collected]
    use_info = (not static_weights) and len(collected) >= 2
    if use_info:
        info_weights = compute_signal_info_weights(per_skill_aggs)
    else:
        info_weights = {}
    info_arg = info_weights if use_info else None

    # Pass 2: score with (or without) the cohort info weights.
    out: list[SkillScore] = []
    for skill_name, n_inv, signal_aggs, weights, goal_source, classified_from in collected:
        goal_values = signals_to_goals(signal_aggs, info_arg)
        score = composite_score(goal_values, weights)
        out.append(
            SkillScore(
                skill=skill_name,
                invocations_30d=n_inv,
                score=score,
                signals={k: round(v, 4) for k, v in sorted(signal_aggs.items())},
                goals={k: round(goal_values[k], 4) for k in GOAL_KEYS if k in goal_values},
                goal_weights={k: round(weights[k], 4) for k in GOAL_KEYS},
                goal_source=goal_source,
                classified_from=classified_from,
            )
        )
    out.sort(key=lambda s: (-s.score, s.skill))
    return out, info_weights


# ─── Calibration JSON write (+ snapshot history) ─────────────────────────────


def _skills_block(scores: list[SkillScore]) -> dict:
    return {
        s.skill: {
            "skill": s.skill,
            "invocations_30d": s.invocations_30d,
            "score": round(s.score, 6),
            "signals": s.signals,
            "goals": s.goals,
            "goal_weights": s.goal_weights,
            "goal_source": s.goal_source,
            "classified_from": s.classified_from,
        }
        for s in scores
    }


def write_calibration(
    calibration_path: Path,
    scores: list[SkillScore],
    *,
    now: datetime,
    window_days: int,
    min_invocations: int,
    half_life_days: float,
    signal_info_weights: dict[str, float],
    static_weights: bool,
) -> None:
    """Merge a ``skills`` block into routing-calibration.json (preserving any
    existing ``agents`` block), bump schema_version additively, atomically
    write, then archive a calibration-history snapshot — mirroring
    ``_interspect_write_routing_calibration``.

    The cohort ``signal_info_weights`` (variance-aware weights, empty in static
    mode) are surfaced inside the ``skills`` block envelope so the weighting is
    auditable downstream.
    """
    # Load existing calibration (preserve agents + sibling fields).
    existing: dict = {}
    if calibration_path.exists():
        try:
            existing = json.loads(calibration_path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    existing["skills"] = _skills_block(scores)
    existing["signal_info_weights"] = {
        k: round(v, 4) for k, v in sorted(signal_info_weights.items())
    }
    existing["skills_calibrated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    existing["skills_calibration"] = {
        "window_days": window_days,
        "min_invocations": min_invocations,
        "half_life_days": half_life_days,
        "qualifying_skills": len(scores),
        "variance_aware": not static_weights,
        "dispersion_ref": DISPERSION_REF,
    }
    # Additive schema bump — never downgrade an existing higher version.
    prev = existing.get("schema_version")
    try:
        prev_n = int(prev) if prev is not None else 0
    except (TypeError, ValueError):
        prev_n = 0
    existing["schema_version"] = max(prev_n, SKILLS_SCHEMA_VERSION)

    # Atomic write: tmp + mv.
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = calibration_path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    # Validate it re-parses before swapping.
    json.loads(tmp.read_text())
    tmp.replace(calibration_path)

    # Archive snapshot (best-effort; never fail the calibration write).
    try:
        history_dir = calibration_path.parent / "calibration-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        snap_ts = now.strftime("%Y-%m-%dT%H-%M-%SZ")
        snap = history_dir / f"{snap_ts}.json"
        snap.write_text(calibration_path.read_text())
    except OSError:
        pass


# ─── Human leaderboard ───────────────────────────────────────────────────────


def print_leaderboard(
    scores: list[SkillScore],
    *,
    signal_info_weights: dict[str, float] | None = None,
    static_weights: bool = False,
    stream=sys.stdout,
) -> None:
    if not scores:
        print("(no qualifying skills)", file=stream)
        return
    sig_order = ("tokens", "error", "no_redirect", "bead_close")
    # Header: surface the cohort information weights so the down-weighting of a
    # saturated signal is auditable right next to the leaderboard.
    if static_weights:
        print("signal info weights: [static-weights] variance adjustment OFF", file=stream)
    elif signal_info_weights is not None:
        iw = "  ".join(
            f"{k}={signal_info_weights.get(k, 0.0):.3f}" for k in sig_order
        )
        print(f"signal info weights: {iw}", file=stream)
    print(
        f"{'#':>3}  {'score':>6}  {'inv':>4}  {'src':<10}  "
        f"{'tokens':>7} {'error':>7} {'no_red':>7} {'bead':>7}  skill",
        file=stream,
    )
    print("-" * 92, file=stream)
    for i, s in enumerate(scores, 1):
        def cell(k: str) -> str:
            return f"{s.signals[k]:.3f}" if k in s.signals else "  -  "
        print(
            f"{i:>3}  {s.score:>6.3f}  {s.invocations_30d:>4}  {s.goal_source:<10}  "
            f"{cell('tokens'):>7} {cell('error'):>7} {cell('no_redirect'):>7} "
            f"{cell('bead_close'):>7}  {s.skill}",
            file=stream,
        )


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="Override interspect.db path")
    ap.add_argument("--window-days", type=int, default=30, help="Trailing window (default 30)")
    ap.add_argument(
        "--min-invocations",
        type=int,
        default=10,
        help="Min distinct invocations in window to qualify (default 10)",
    )
    ap.add_argument(
        "--half-life-days",
        type=float,
        default=14.0,
        help="Recency half-life in days (default 14; large = ~unweighted mean)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Compute + print; do not write the json"
    )
    ap.add_argument("--json", action="store_true", help="Emit machine JSON to stdout")
    ap.add_argument(
        "--static-weights",
        action="store_true",
        help="Disable variance-aware signal weighting (recover legacy static-weight "
        "composite for comparison/regression).",
    )
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = (
        Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)
    )
    if not db_path.exists():
        print(
            f"score-skills: DB not found at {db_path} — "
            "run a hook or _interspect_ensure_db first",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        scores, signal_info_weights = score_skills(
            conn,
            now=now,
            window_days=args.window_days,
            min_invocations=args.min_invocations,
            half_life_days=args.half_life_days,
            static_weights=args.static_weights,
        )
    finally:
        conn.close()

    print(
        f"score-skills: db={db_path} window_days={args.window_days} "
        f"min_invocations={args.min_invocations} half_life_days={args.half_life_days} "
        f"qualifying={len(scores)}",
        file=sys.stderr,
    )

    calibration_path = db_path.parent / "routing-calibration.json"
    if not args.dry_run:
        write_calibration(
            calibration_path,
            scores,
            now=now,
            window_days=args.window_days,
            min_invocations=args.min_invocations,
            half_life_days=args.half_life_days,
            signal_info_weights=signal_info_weights,
            static_weights=args.static_weights,
        )
        print(f"score-skills: wrote skills block → {calibration_path}", file=sys.stderr)
    else:
        print("score-skills: [dry-run] not writing routing-calibration.json", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "calibrated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "schema_version": SKILLS_SCHEMA_VERSION,
                    "window_days": args.window_days,
                    "min_invocations": args.min_invocations,
                    "half_life_days": args.half_life_days,
                    "variance_aware": not args.static_weights,
                    "signal_info_weights": {
                        k: round(v, 4) for k, v in sorted(signal_info_weights.items())
                    },
                    "skills": _skills_block(scores),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(file=sys.stderr)
        print_leaderboard(
            scores,
            signal_info_weights=signal_info_weights,
            static_weights=args.static_weights,
            stream=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
