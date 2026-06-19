#!/usr/bin/env python3
"""Infer per-skill goal weights for the Interspect skill calibrator (sylveste-7aj8.4).

Classifies each registered skill into a goal-weight vector
`{speed, precision, completeness}` that sums to 1.0, persisted in the
`skill_goals` table:

  skill_goals(skill_name PRIMARY KEY, goal_weights TEXT, classified_from TEXT,
              classifier_version TEXT, classified_at TEXT, skill_md_hash TEXT)

Two passes:

  1. Classification (one Haiku call per skill that needs it). Reads each
     SKILL.md's frontmatter + a body slice, asks Haiku for a strict-JSON
     weight vector, parses defensively, renormalizes, and writes a row with
     `classified_from='skill_md'`. A content hash short-circuits skills whose
     SKILL.md is unchanged since the last classification (override with --force).

  2. Observed refinement (--refine; runs by default after classification, see
     RUN_REFINE_BY_DEFAULT). For skills with >= MIN_REFINE_SIGNALS skill_signals
     rows AND an existing skill_goals row, compute the observed signal mix,
     EMA-blend it (alpha=REFINE_ALPHA) with the classifier weights, and rewrite
     the row with `classified_from='observed'`. No API call.

Skill enumeration mirrors how skills appear in `evidence.source`: the canonical
name is the namespaced `<plugin>:<skill>` form, derived from the plugin dir +
skill dir name (the SKILL.md frontmatter `name:` is the bare skill name only).
Plugin SKILL.md files live under the cache layout
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md`;
user skills live at `~/.claude/skills/<skill>/SKILL.md` (namespace = bare name);
project skills at `<repo>/.claude/skills/<skill>/SKILL.md`.

COMMAND enumeration (sylveste-7aj8.8) covers the highest-traffic surfaces. Commands
fire as `tool:"Skill"` events too (that's why `clavain:sprint`, `interflux:flux-drive`
appear in the scoring leaderboard), but they are SINGLE `.md` files directly in a
`commands/` dir — not a dir containing SKILL.md. They are enumerated identically and
land in the SAME `skill_goals` table keyed by the namespaced name:
  - Plugin commands: `<cache>/<marketplace>/<plugin>/<version>/commands/<cmd>.md`
    → name `<plugin>:<cmd>` (e.g. `.../clavain/0.6.252/commands/sprint.md` → `clavain:sprint`).
  - User commands: `~/.claude/commands/<cmd>.md` → bare name `<cmd>`.
  - Project commands: `<repo>/.claude/commands/<cmd>.md` → bare name `<cmd>`.
Classification is identical to skills (same Haiku prompt over frontmatter + body slice).

entity_kind discriminator: the `skill_goals` table has no dedicated column and this
task does NOT migrate schema, so we record the entity kind in the existing
`classified_from` column — `skill_md` for skills, `command_md` for commands (the
refine pass still rewrites it to `observed`). This is queryable
(`WHERE classified_from='command_md'`) without a schema change, and scoring treats
`classified_from` as an opaque audit string (it only switches goal_source to
'skill_goals' when any row exists), so the new value is safe.

De-dup / tie-break: enumeration walks skills first, then commands, and dedups by
canonical name (first writer wins). If the same `<plugin>:<name>` exists as BOTH a
skill and a command (not observed in practice), the skill wins — skills are the
richer SKILL.md surface and were the original classified entity; the command would
only be a thin orchestration shim under the same name.

Modes:
  --mock      Deterministic stub classifier (no API call) — for tests/CI.
  --dry-run   Print would-be weights; never writes.
  --force     Re-classify even when skill_md_hash is unchanged.
  --refine    Force the observed-refinement pass (it also runs by default).
  --skill N   Classify just one skill OR command by canonical name (on-demand).

Usage:
  infer-skill-goals.py [--db PATH] [--skills-root PATH ...]
                       [--skill NAME] [--mock] [--dry-run] [--force]
                       [--refine] [--no-refine] [--repo-root .] [--model ID]

# ─── Cron (do NOT install here; documented for the operator) ─────────────────
# Intended weekly cadence — classify new/changed skills + refine from signals:
#
#   # m h dom mon dow   command
#   0 4 * * 1  cd /home/mk/projects/Sylveste/interverse/interspect && \
#              python3 scripts/infer-skill-goals.py >> \
#              ~/.claude/interspect/skill-goals-cron.log 2>&1
#
# Runs Monday 04:00. The hash short-circuit makes re-runs cheap: only skills
# whose SKILL.md changed since the last run incur a Haiku call. The refine pass
# is API-free and re-blends observed signals every run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple


# ─── Tunables ────────────────────────────────────────────────────────────────

# Version stamp for classifier_version. Bumping this (or the prompt below)
# should be reflected here so re-runs with --force are auditable.
CLASSIFIER_VERSION = "skill-goals-v1"

# Haiku model for the non-interactive classifier call. Alias form resolves to
# the current Haiku 4.5 snapshot; verified against the claude-api skill catalog.
DEFAULT_MODEL = "claude-haiku-4-5"

# Body slice fed to the classifier (after frontmatter). ~2KB keeps the prompt
# cheap while capturing the skill's intent.
BODY_SLICE_BYTES = 2048

# Observed-refinement gates.
MIN_REFINE_SIGNALS = 20
REFINE_ALPHA = 0.3  # EMA weight on the observed mix; (1-alpha) on the classifier.

# Run the observed-refinement pass by default after classification. The --refine
# flag forces it; --no-refine suppresses it. Default-on is the documented choice:
# refinement is API-free and idempotent, so folding it into every run keeps
# skill_goals current without a second invocation.
RUN_REFINE_BY_DEFAULT = True

GOAL_KEYS = ("speed", "precision", "completeness")

# classified_from values that double as the entity_kind discriminator (no schema
# migration — these live in the existing classified_from column). The refine pass
# overwrites either with 'observed'.
CLASSIFIED_FROM_SKILL = "skill_md"
CLASSIFIED_FROM_COMMAND = "command_md"


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


def default_skills_roots() -> list[Path]:
    """Real skills roots, in scan order. Each is a glob *parent* — the globber
    below walks them with the appropriate pattern for each layout."""
    home = Path(os.path.expanduser("~"))
    return [
        home / ".claude" / "plugins" / "cache",  # namespaced plugin skills
        home / ".claude" / "skills",  # user skills (bare-name namespace)
    ]


def default_commands_roots() -> list[Path]:
    """Real commands roots, in scan order.

    The plugin cache root is shared with skills — the command globber pulls the
    `<marketplace>/<plugin>/<version>/commands/<cmd>.md` layout out of it. The user
    commands dir is flat (`~/.claude/commands/<cmd>.md`, bare-name namespace).
    """
    home = Path(os.path.expanduser("~"))
    return [
        home / ".claude" / "plugins" / "cache",  # namespaced plugin commands
        home / ".claude" / "commands",  # user commands (bare-name namespace)
    ]


# ─── Skill enumeration ───────────────────────────────────────────────────────


class SkillEntry(NamedTuple):
    name: str  # canonical namespaced name, e.g. "intersearch:session-search"
    path: Path  # absolute path to SKILL.md (or a command .md)
    kind: str = "skill"  # "skill" or "command" — drives the classified_from value


def _plugin_namespaced_name(skill_md: Path, cache_root: Path) -> str | None:
    """Derive `<plugin>:<skill>` from a cache-layout SKILL.md path.

    Layout: <cache_root>/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    The plugin segment is the dir two levels under the marketplace; the skill
    segment is the parent dir of SKILL.md. This matches how skills appear in
    evidence.source (e.g. 'intersearch:session-search').
    """
    try:
        rel = skill_md.relative_to(cache_root)
    except ValueError:
        return None
    parts = rel.parts
    # marketplace / plugin / version / skills / skill / SKILL.md  → 6 parts
    if len(parts) < 6 or parts[-1] != "SKILL.md" or parts[-3] != "skills":
        return None
    plugin = parts[1]
    skill = parts[-2]
    if not plugin or not skill:
        return None
    return f"{plugin}:{skill}"


def enumerate_skills(roots: list[Path]) -> list[SkillEntry]:
    """Find SKILL.md files under each root and assign canonical names.

    - A root that contains a 'cache'-style plugin tree (or is one) is walked
      for the `<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md` layout
      and named `<plugin>:<skill>`.
    - Any other root is treated as a flat skills dir
      (`<root>/<skill>/SKILL.md`) and named with the bare `<skill>` (matching
      how un-namespaced user/project skills surface in evidence.source).

    Dedup by canonical name; first root wins (plugin cache before user dir).
    """
    seen: dict[str, SkillEntry] = {}
    for root in roots:
        if not root.is_dir():
            continue
        is_cache_layout = root.name == "cache" or _looks_like_cache_root(root)
        for skill_md in sorted(root.rglob("SKILL.md")):
            if is_cache_layout:
                name = _plugin_namespaced_name(skill_md, root)
                if name is None:
                    # Not the expected depth — fall back to the skill dir name.
                    name = skill_md.parent.name
            else:
                # Flat layout: <root>/<skill>/SKILL.md → bare skill name.
                name = skill_md.parent.name
            if not name or name in seen:
                continue
            seen[name] = SkillEntry(name=name, path=skill_md)
    return list(seen.values())


def _looks_like_cache_root(root: Path) -> bool:
    """Heuristic: does this root hold the deep cache layout? True if at least
    one SKILL.md sits at the `.../skills/<skill>/SKILL.md` depth with a
    `skills` grandparent and >= 5 path segments under root."""
    for skill_md in root.rglob("SKILL.md"):
        try:
            depth = len(skill_md.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= 6 and skill_md.parent.parent.name == "skills":
            return True
        return False  # first hit decides
    return False


def _plugin_namespaced_command_name(cmd_md: Path, cache_root: Path) -> str | None:
    """Derive `<plugin>:<cmd-stem>` from a cache-layout command .md path.

    Layout: <cache_root>/<marketplace>/<plugin>/<version>/commands/<cmd>.md
    The plugin segment is the dir two levels under the marketplace; the command
    segment is the .md filename stem. This matches how commands appear as Skill
    events (e.g. 'clavain:sprint').
    """
    try:
        rel = cmd_md.relative_to(cache_root)
    except ValueError:
        return None
    parts = rel.parts
    # marketplace / plugin / version / commands / <cmd>.md  → 5 parts
    if len(parts) < 5 or parts[-2] != "commands" or not parts[-1].endswith(".md"):
        return None
    plugin = parts[1]
    cmd = cmd_md.stem
    if not plugin or not cmd:
        return None
    return f"{plugin}:{cmd}"


def _is_cache_command_root(root: Path) -> bool:
    """Heuristic mirror of _looks_like_cache_root for the command layout: a deep
    `.../commands/<cmd>.md` (>= 5 segments, `commands` parent dir) decides."""
    if root.name == "cache":
        return True
    for cmd_md in root.rglob("*.md"):
        try:
            depth = len(cmd_md.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= 5 and cmd_md.parent.name == "commands":
            return True
        return False  # first hit decides
    return False


def enumerate_commands(roots: list[Path]) -> list[SkillEntry]:
    """Find command .md files under each root and assign canonical names.

    Commands are SINGLE `.md` files directly in a `commands/` dir (NOT a dir with
    a SKILL.md inside). Two layouts:
    - Cache root: `<marketplace>/<plugin>/<version>/commands/<cmd>.md` → `<plugin>:<cmd>`.
    - Flat root (`~/.claude/commands`, `<repo>/.claude/commands`): `<cmd>.md` → bare `<cmd>`.

    Only files whose parent dir is named `commands` are taken (so sibling
    non-command files like `degraded-modes.yaml` — already excluded by the *.md
    glob — and nested `commands/<sub>/x.md` are filtered). Dedup by canonical
    name within commands; first root wins.
    """
    seen: dict[str, SkillEntry] = {}
    for root in roots:
        if not root.is_dir():
            continue
        is_cache_layout = _is_cache_command_root(root)
        for cmd_md in sorted(root.rglob("*.md")):
            # Only direct children of a `commands/` dir are commands.
            if cmd_md.parent.name != "commands":
                continue
            if is_cache_layout:
                name = _plugin_namespaced_command_name(cmd_md, root)
                if name is None:
                    # Not the expected depth — fall back to the bare stem.
                    name = cmd_md.stem
            else:
                # Flat layout: <root>/commands/<cmd>.md — but a flat root IS the
                # commands dir, so its direct children are bare-named commands.
                name = cmd_md.stem
            if not name or name in seen:
                continue
            seen[name] = SkillEntry(name=name, path=cmd_md, kind="command")
    return list(seen.values())


# ─── SKILL.md parsing + hashing ──────────────────────────────────────────────


def read_skill_md(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def skill_md_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_prompt_input(text: str) -> str:
    """Frontmatter (name/description/when-to-use) + first ~BODY_SLICE_BYTES of body.

    Frontmatter is the leading `---`-delimited YAML block, if present. We forward
    it verbatim (it carries name + description + user_invocable / when-to-use)
    plus a body slice so the classifier sees the skill's actual workflow.
    """
    frontmatter = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            # Include both fences for clarity to the model.
            close = text.find("\n", end + 1)
            frontmatter = text[: close + 1] if close != -1 else text[: end + 4]
            body = text[close + 1:] if close != -1 else text[end + 4:]
    body_slice = body[:BODY_SLICE_BYTES]
    return f"{frontmatter}\n{body_slice}".strip()


# ─── Classifier ──────────────────────────────────────────────────────────────


_PROMPT_TEMPLATE = """\
You classify a Claude Code "skill" or "command" into three goal weights that \
describe what it optimizes for. Output STRICT JSON ONLY — no prose, no code \
fences, no explanation — an object with exactly these float keys summing to 1.0:

  {{"speed": <float>, "precision": <float>, "completeness": <float>}}

Definitions:
  - speed:        retrieval/search/lookup; fast answers; low latency matters most.
  - precision:    reasoning/implementation/correctness; getting one thing right.
  - completeness: audit/review/coverage; not missing anything; thoroughness.

Few-shot examples (description -> weights):
  retrieval/search skill (e.g. intersearch, recall):
      {{"speed": 0.7, "precision": 0.2, "completeness": 0.1}}
  reasoning/implementation skill (e.g. clavain:work):
      {{"speed": 0.2, "precision": 0.6, "completeness": 0.2}}
  audit/review skill (e.g. quality-gates, interwatch:audit):
      {{"speed": 0.1, "precision": 0.3, "completeness": 0.6}}
  workflow/orchestration command (e.g. clavain:sprint — sequences brainstorm,
  plan, execute, review, ship; correctness of each step and full coverage of the
  lifecycle both matter, speed least):
      {{"speed": 0.15, "precision": 0.45, "completeness": 0.4}}

Judge from the description — do NOT assume a command is always orchestration.
Output the JSON object only.

ENTITY:
{skill_text}
"""


def build_prompt(skill_text: str) -> str:
    return _PROMPT_TEMPLATE.format(skill_text=skill_text)


class ClassifyError(Exception):
    pass


def _extract_json_object(raw: str) -> dict:
    """Pull the first balanced JSON object out of model output, tolerating prose
    or code fences around it. Raises ClassifyError if none parses."""
    # Strip common code-fence wrappers first.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidates: list[str] = []
    if fenced:
        candidates.append(fenced.group(1))
    # Greedy outermost-brace scan as a fallback.
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(raw)):
            c = raw[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start: i + 1])
                    break
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ClassifyError(f"no JSON object in model output: {raw[:200]!r}")


def parse_and_normalize(raw: str) -> dict[str, float]:
    """Parse model output into a normalized {speed, precision, completeness} dict.

    Renormalizes to sum 1.0. Raises ClassifyError on missing/invalid keys or a
    non-positive total (which can't be renormalized).
    """
    obj = _extract_json_object(raw)
    weights: dict[str, float] = {}
    for key in GOAL_KEYS:
        val = obj.get(key)
        if val is None:
            raise ClassifyError(f"missing key '{key}' in {obj!r}")
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ClassifyError(f"non-numeric '{key}'={val!r}")
        if f < 0:
            raise ClassifyError(f"negative weight '{key}'={f}")
        weights[key] = f
    return _renormalize(weights)


def _renormalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights[k] for k in GOAL_KEYS)
    if total <= 0:
        raise ClassifyError(f"weights sum to {total} (cannot renormalize)")
    return {k: weights[k] / total for k in GOAL_KEYS}


def mock_classify(skill_text: str) -> str:
    """Deterministic stub classifier — no API call.

    Keyword-buckets the skill text into one of the three few-shot archetypes so
    tests are reproducible and CI can run without spend. Always returns strict
    JSON (a string, mirroring the real claude -p stdout contract).
    """
    t = skill_text.lower()
    audit_kw = ("audit", "review", "verify", "coverage", "gate", "lint", "check")
    search_kw = ("search", "retriev", "lookup", "recall", "find", "query", "index")
    if any(k in t for k in audit_kw):
        w = {"speed": 0.1, "precision": 0.3, "completeness": 0.6}
    elif any(k in t for k in search_kw):
        w = {"speed": 0.7, "precision": 0.2, "completeness": 0.1}
    else:  # default: reasoning/implementation
        w = {"speed": 0.2, "precision": 0.6, "completeness": 0.2}
    return json.dumps(w)


def _run_claude(argv: list[str], prompt: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv + [prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise ClassifyError(f"claude -p timed out: {e}")
    except OSError as e:
        raise ClassifyError(f"claude -p failed to launch: {e}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def claude_classify(skill_text: str, model: str) -> str:
    """Invoke `claude -p` non-interactively and return its stdout text.

    Primary form (hook-free, the correct mode for a headless classifier):
        claude -p --bare --model <id> --output-format text "<prompt>"
    `--bare` skips hooks/LSP/plugins so a host SessionStart hook can't consume
    the turn — but it pins auth to ANTHROPIC_API_KEY/apiKeyHelper. When that
    fails (e.g. an OAuth/Max-login host with no API key, which prints
    "Please run /login"), fall back to the standard invocation, which honors
    OAuth auth at the cost of host hooks possibly polluting output (the
    defensive JSON extractor in parse_and_normalize tolerates surrounding prose).

    Raises ClassifyError on a missing binary, non-zero exit, or empty output.
    """
    if shutil.which("claude") is None:
        raise ClassifyError("claude binary not on PATH")
    prompt = build_prompt(skill_text)

    bare = ["claude", "-p", "--bare", "--model", model, "--output-format", "text"]
    rc, out, err = _run_claude(bare, prompt)
    # --bare with no API key prints a login notice to stdout and exits 0 — detect
    # that (and any non-zero/empty result) and retry without --bare.
    bare_unusable = (
        rc != 0
        or not out
        or "run /login" in out.lower()
        or "not logged in" in out.lower()
    )
    if bare_unusable:
        std = ["claude", "-p", "--model", model, "--output-format", "text"]
        rc, out, err = _run_claude(std, prompt)
        if rc != 0:
            raise ClassifyError(f"claude -p exited {rc}: {err[:200]}")
        if not out:
            raise ClassifyError("claude -p produced empty output")
    return out


# ─── DB helpers ──────────────────────────────────────────────────────────────


def existing_hash(conn: sqlite3.Connection, skill_name: str) -> str | None:
    row = conn.execute(
        "SELECT skill_md_hash FROM skill_goals WHERE skill_name = ?",
        (skill_name,),
    ).fetchone()
    return row[0] if row else None


def existing_row(conn: sqlite3.Connection, skill_name: str) -> dict | None:
    row = conn.execute(
        "SELECT goal_weights, classified_from, classifier_version, "
        "       classified_at, skill_md_hash "
        "FROM skill_goals WHERE skill_name = ?",
        (skill_name,),
    ).fetchone()
    if not row:
        return None
    return {
        "goal_weights": row[0],
        "classified_from": row[1],
        "classifier_version": row[2],
        "classified_at": row[3],
        "skill_md_hash": row[4],
    }


def upsert_goals(
    conn: sqlite3.Connection,
    *,
    skill_name: str,
    weights: dict[str, float],
    classified_from: str,
    classifier_version: str,
    classified_at: str,
    skill_md_hash_val: str | None,
) -> None:
    conn.execute(
        "INSERT INTO skill_goals "
        "(skill_name, goal_weights, classified_from, classifier_version, "
        " classified_at, skill_md_hash) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(skill_name) DO UPDATE SET "
        "  goal_weights=excluded.goal_weights, "
        "  classified_from=excluded.classified_from, "
        "  classifier_version=excluded.classifier_version, "
        "  classified_at=excluded.classified_at, "
        "  skill_md_hash=excluded.skill_md_hash",
        (
            skill_name,
            json.dumps(weights),
            classified_from,
            classifier_version,
            classified_at,
            skill_md_hash_val,
        ),
    )


# ─── Observed refinement ─────────────────────────────────────────────────────

# Map signal_kind → goal. Consistent with the scoring plan: token-efficiency
# signals load on speed; error/redirect-avoidance load on precision; bead
# closure loads on completeness.
SIGNAL_GOAL_MAP = {
    "tokens": "speed",
    "error": "precision",
    "no_redirect": "precision",
    "bead_close": "completeness",
}


def observed_mix(conn: sqlite3.Connection, skill_name: str) -> tuple[dict[str, float], int]:
    """Aggregate skill_signals into a normalized goal mix + the total signal count.

    Sums each signal's `value` into its mapped goal, then renormalizes across the
    three goals. Returns ({}, n) when no mapped signal mass exists.
    """
    rows = conn.execute(
        "SELECT signal_kind, value FROM skill_signals WHERE skill_name = ?",
        (skill_name,),
    ).fetchall()
    total_rows = len(rows)
    acc = {k: 0.0 for k in GOAL_KEYS}
    for signal_kind, value in rows:
        goal = SIGNAL_GOAL_MAP.get(signal_kind)
        if goal is None:
            continue
        try:
            acc[goal] += float(value)
        except (TypeError, ValueError):
            continue
    mass = sum(acc.values())
    if mass <= 0:
        return {}, total_rows
    return {k: acc[k] / mass for k in GOAL_KEYS}, total_rows


def ema_blend(
    classifier: dict[str, float], observed: dict[str, float], alpha: float
) -> dict[str, float]:
    """alpha*observed + (1-alpha)*classifier, renormalized."""
    blended = {
        k: alpha * observed[k] + (1.0 - alpha) * classifier[k] for k in GOAL_KEYS
    }
    return _renormalize(blended)


def refine_pass(
    conn: sqlite3.Connection,
    skills: list[SkillEntry] | None,
    *,
    dry_run: bool,
    now_iso: str,
    classifier_version: str,
) -> dict[str, int]:
    """Re-blend classifier weights with observed signal mix for eligible skills.

    Eligible = has an existing skill_goals row AND >= MIN_REFINE_SIGNALS signals.
    Operates over the skill names already in skill_goals (intersected with
    `skills` when a single-skill subset was requested).
    """
    stats = {"refine_eligible": 0, "refined": 0}

    # Determine candidate skill names: those present in skill_goals.
    name_filter: set[str] | None = None
    if skills is not None:
        name_filter = {s.name for s in skills}

    rows = conn.execute("SELECT skill_name, goal_weights FROM skill_goals").fetchall()
    for skill_name, goal_weights_json in rows:
        if name_filter is not None and skill_name not in name_filter:
            continue
        observed, n_signals = observed_mix(conn, skill_name)
        if n_signals < MIN_REFINE_SIGNALS or not observed:
            continue
        stats["refine_eligible"] += 1
        try:
            classifier_weights = json.loads(goal_weights_json)
            classifier_weights = {k: float(classifier_weights[k]) for k in GOAL_KEYS}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(
                f"infer-skill-goals: refine skip {skill_name}: bad stored weights ({e})",
                file=sys.stderr,
            )
            continue
        blended = ema_blend(classifier_weights, observed, REFINE_ALPHA)
        if dry_run:
            print(
                f"infer-skill-goals: [dry-run] refine {skill_name} "
                f"obs={_fmt(observed)} -> blended={_fmt(blended)}",
                file=sys.stderr,
            )
            stats["refined"] += 1
            continue
        upsert_goals(
            conn,
            skill_name=skill_name,
            weights=blended,
            classified_from="observed",
            classifier_version=classifier_version,
            classified_at=now_iso,
            skill_md_hash_val=existing_hash(conn, skill_name),
        )
        stats["refined"] += 1
    return stats


def _fmt(w: dict[str, float]) -> str:
    return "{" + ", ".join(f"{k}={w[k]:.2f}" for k in GOAL_KEYS) + "}"


# ─── Classification pass ─────────────────────────────────────────────────────


def classify_pass(
    conn: sqlite3.Connection,
    skills: list[SkillEntry],
    *,
    mock: bool,
    dry_run: bool,
    force: bool,
    model: str,
    now_iso: str,
) -> dict[str, int]:
    stats = {
        "found": len(skills),
        "classified": 0,
        "short_circuited": 0,
        "errors": 0,
    }
    for skill in skills:
        try:
            text = read_skill_md(skill.path)
        except OSError as e:
            print(f"infer-skill-goals: read error {skill.name}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        h = skill_md_hash(text)
        if not force and existing_hash(conn, skill.name) == h:
            stats["short_circuited"] += 1
            continue

        prompt_input = extract_prompt_input(text)
        try:
            raw = mock_classify(prompt_input) if mock else claude_classify(prompt_input, model)
            weights = parse_and_normalize(raw)
        except ClassifyError as e:
            print(
                f"infer-skill-goals: classify error {skill.name}: {e}",
                file=sys.stderr,
            )
            stats["errors"] += 1
            continue

        if dry_run:
            print(
                f"infer-skill-goals: [dry-run] {skill.name} -> {_fmt(weights)}",
                file=sys.stderr,
            )
            stats["classified"] += 1
            continue

        upsert_goals(
            conn,
            skill_name=skill.name,
            weights=weights,
            classified_from="skill_md",
            classifier_version=CLASSIFIER_VERSION,
            classified_at=now_iso,
            skill_md_hash_val=h,
        )
        stats["classified"] += 1
    return stats


# ─── Main ────────────────────────────────────────────────────────────────────


def resolve_skills(
    roots: list[Path], only: str | None
) -> list[SkillEntry]:
    skills = enumerate_skills(roots)
    if only:
        skills = [s for s in skills if s.name == only]
    return skills


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="Override interspect.db path")
    ap.add_argument(
        "--skills-root",
        action="append",
        default=None,
        help="Override skills root (repeatable). Default: real plugin cache + user skills.",
    )
    ap.add_argument("--skill", default=None, help="Classify only this canonical skill name")
    ap.add_argument("--mock", action="store_true", help="Deterministic stub classifier (no API)")
    ap.add_argument("--dry-run", action="store_true", help="Print would-be weights, no writes")
    ap.add_argument("--force", action="store_true", help="Re-classify even if hash unchanged")
    ap.add_argument("--refine", action="store_true", help="Force the observed-refinement pass")
    ap.add_argument(
        "--no-refine", action="store_true", help="Skip the observed-refinement pass"
    )
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Haiku model id (default {DEFAULT_MODEL})")
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args()

    repo_root = find_repo_root(Path(args.repo_root))
    db_path = Path(os.path.expanduser(args.db)) if args.db else default_db_path(repo_root)

    if not db_path.exists():
        print(
            f"infer-skill-goals: DB not found at {db_path} — "
            "run a hook or _interspect_ensure_db first",
            file=sys.stderr,
        )
        return 1

    if args.skills_root:
        roots = [Path(os.path.expanduser(r)) for r in args.skills_root]
    else:
        roots = default_skills_roots()
        # Include the project .claude/skills if present.
        proj = repo_root / ".claude" / "skills"
        if proj.is_dir():
            roots.append(proj)

    skills = resolve_skills(roots, args.skill)
    if args.skill and not skills:
        print(
            f"infer-skill-goals: skill '{args.skill}' not found under "
            f"{[str(r) for r in roots]}",
            file=sys.stderr,
        )
        return 1

    print(
        f"infer-skill-goals: roots={[str(r) for r in roots]} "
        f"found={len(skills)} mock={args.mock} dry_run={args.dry_run} force={args.force}",
        file=sys.stderr,
    )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    cstats: dict[str, int] = {}
    rstats: dict[str, int] = {"refine_eligible": 0, "refined": 0}
    try:
        cstats = classify_pass(
            conn,
            skills,
            mock=args.mock,
            dry_run=args.dry_run,
            force=args.force,
            model=args.model,
            now_iso=now_iso,
        )
        if not args.dry_run:
            conn.commit()

        # Refine pass: by default-on (RUN_REFINE_BY_DEFAULT), forced by --refine,
        # suppressed by --no-refine.
        do_refine = args.refine or (RUN_REFINE_BY_DEFAULT and not args.no_refine)
        if do_refine:
            # Scope to the requested subset only when --skill was passed.
            subset = skills if args.skill else None
            rstats = refine_pass(
                conn,
                subset,
                dry_run=args.dry_run,
                now_iso=now_iso,
                classifier_version=CLASSIFIER_VERSION,
            )
            if not args.dry_run:
                conn.commit()
    finally:
        conn.close()

    print(
        "infer-skill-goals: "
        f"found={cstats.get('found', 0)} "
        f"classified={cstats.get('classified', 0)} "
        f"short_circuited={cstats.get('short_circuited', 0)} "
        f"refine_eligible={rstats.get('refine_eligible', 0)} "
        f"refined={rstats.get('refined', 0)} "
        f"errors={cstats.get('errors', 0)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
