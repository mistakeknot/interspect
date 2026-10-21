"""Round-6 regressions: canary invalidation history, audit signals, failed setup."""
import importlib.util
import json
import os
import sqlite3
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from test_gate_fuzz import ROOT, LIB, cli, db, row, rows, shell, template, explicit
from test_gate_normalization import gate_module
from test_gate_round4 import skill_signals


def audit_module():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('round6_audit', ROOT / 'scripts/calibrate-audit.py')
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    return audit


@pytest.mark.parametrize('state', ['quarantined', 'migrated', 'wrong_skill', 'wrong_session', 'orphan'])
def test_defect_escape_rate_ignores_ineligible_skill_signals(db, state):
    skill_signals(db, state, value=0.0)
    point = audit_module().compute_consensus_trend_point(db, datetime.now(timezone.utc), 90)
    assert point['defect_escape_rate'] is None


def test_defect_escape_rate_counts_eligible_skill_signals(db):
    skill_signals(db, 'eligible', n=2, value=0.5)
    point = audit_module().compute_consensus_trend_point(db, datetime.now(timezone.utc), 90)
    assert point['defect_escape_rate'] == 0.5


# ── Item 1: canary invalidation ─────────────────────────────────────────────

SENTINEL = 4102444800


def add_canary(db, group_id, window=None):
    with sqlite3.connect(db) as conn:
        cid = conn.execute(
            "INSERT INTO canary (file,commit_sha,group_id,applied_at,window_uses,"
            "window_expires_at,baseline_override_rate,baseline_fp_rate,"
            "baseline_finding_density,baseline_window,status) "
            "VALUES ('test','test',?,'2026-01-03',1,'2026-01-17',1,0.5,1,?,'active')",
            (group_id, window)).lastrowid
        conn.execute('INSERT INTO canary_samples(canary_id,session_id,ts,override_rate,fp_rate,finding_density) '
                     "VALUES (?,'session-a','2026-01-04',9,1,1)", (cid,))
        conn.execute('UPDATE canary SET uses_so_far=1 WHERE id=?', (cid,))
    return cid


def canary_state(db, cid):
    with sqlite3.connect(db) as conn:
        status, baseline = conn.execute('SELECT status,baseline_override_rate FROM canary WHERE id=?', (cid,)).fetchone()
        samples = conn.execute('SELECT COUNT(*) FROM canary_samples WHERE canary_id=?', (cid,)).fetchone()[0]
    return status, baseline, samples


def repair(db, revision=3):
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=?', (revision,))
    result = cli(db, 'migrate', {})
    assert result.returncode == 0, result.stderr


def lifted_row(db, **kwargs):
    assert cli(db, 'insert', row(**kwargs)).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    assert rows(db)[0]['corroborated_at'] > 0


def test_overlay_canary_is_invalidated_with_its_agent_cohort(db):
    lifted_row(db)
    agent, overlay, other = (add_canary(db, g) for g in ('fd-a', 'fd-a/tuning', 'fd-b/tuning'))
    repair(db)
    assert canary_state(db, agent) == ('needs_review', None, 0)
    assert canary_state(db, overlay) == ('needs_review', None, 0)
    assert canary_state(db, other) == ('active', 1, 1)
    with sqlite3.connect(db) as conn:
        reports = [json.loads(r[0]) for r in conn.execute('SELECT override_json FROM interspect_gate_override_review')]
    assert {r['canary_id'] for r in reports if r.get('kind') == 'canary'} == {agent, overlay}


def rehold_as_revision_three(db):
    """Revision 3 repaired the row again after a later lift: no exposure table,
    and both the first audit and the current row look merely held."""
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TRIGGER interspect_gate_update')
        conn.execute('DROP TABLE interspect_gate_exposed')
        conn.execute('UPDATE evidence SET quarantine_until=?, corroborated_at=0, '
                     'corroborated_by=NULL, corroborated_via=NULL', (SENTINEL,))


@pytest.mark.parametrize('group_id', ['fd-a', 'fd-a/tuning'])
def test_earlier_repair_cannot_erase_exposure_history(db, group_id):
    assert cli(db, 'insert', row()).returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM interspect_gate_schema')
    assert cli(db, 'migrate', {}).returncode == 0   # first audit sees a held row
    assert cli(db, 'corroborate', explicit()).returncode == 0   # later exposure
    rehold_as_revision_three(db)
    with sqlite3.connect(db) as conn:
        audit = json.loads(conn.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0])
    assert audit['corroborated_at'] == 0 and audit['quarantine_until'] == SENTINEL
    cid = add_canary(db, group_id)
    repair(db)
    assert canary_state(db, cid) == ('needs_review', None, 0)


def test_historical_audited_source_matches_canary_when_identities_were_cleared(db):
    review = 'a@b@c'
    assert cli(db, 'insert', row(context=json.dumps(dict(low_confidence=True, review_id=review, finding_id='p0-1')))).returncode == 0
    assert cli(db, 'corroborate', explicit(scope_id=review)).returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=2')
    assert cli(db, 'migrate', {}).returncode == 0     # revision-3 style repair clears identity
    evidence = rows(db)[0]
    assert evidence['gate_version'] == 0 and evidence['gate_source'] is None
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TRIGGER interspect_gate_update')   # direct writers are trusted
        conn.execute('UPDATE evidence SET source=?', ('legacy-diagnostic',))
    cid = add_canary(db, 'fd-a')
    other = add_canary(db, 'fd-b')
    repair(db)
    assert canary_state(db, cid) == ('needs_review', None, 0)
    assert canary_state(db, other) == ('active', 1, 1)


def git_project(tmp_path, db):
    """A real writable repo (the sandbox restriction applies only to this clone)."""
    project = db.parent
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')
    subprocess.run(['git', 'init', '-q', str(project)], check=True, env=env)
    (project / '.clavain/interspect').mkdir(parents=True, exist_ok=True)
    (project / '.clavain/interspect/protected-paths.json').write_text(json.dumps(
        dict(protected_paths=[], modification_allow_list=['.claude/*', '.clavain/interspect/overlays/**'])))
    (project / '.claude').mkdir(exist_ok=True)
    (project / '.claude/routing-overrides.json').write_text(json.dumps(
        dict(version=1, overrides=[dict(agent='fd-a', action='exclude', evidence_ids=[1])])))
    overlay = project / '.clavain/interspect/overlays/fd-a/tuning.md'
    overlay.parent.mkdir(parents=True)
    overlay.write_text('---\nactive: true\n---\nbody\n')
    subprocess.run(['git', '-C', str(project), 'add', '-A'], check=True, env=env)
    subprocess.run(['git', '-C', str(project), 'commit', '-qm', 'init', '--no-verify'], check=True, env=env)
    return project, env


@pytest.mark.parametrize('call,group_id', [
    ('_interspect_revert_routing_override fd-a', 'fd-a'),
    ('_interspect_disable_overlay fd-a tuning', 'fd-a/tuning')])
def test_revert_closes_needs_review_canaries(db, tmp_path, call, group_id):
    project, env = git_project(tmp_path, db)
    cid = add_canary(db, group_id)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE canary SET status='needs_review' WHERE id=?", (cid,))
    result = subprocess.run(['bash', '-c', 'source "$1"; _INTERSPECT_DB="$2"; cd "$3"; ' + call,
                             'revert', str(LIB), str(db), str(project)], text=True,
                            capture_output=True, env=dict(env, CLAUDE_PROJECT_DIR=str(project)), timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert canary_state(db, cid)[0] == 'reverted'


# ── Item 1 (status): invalidated canaries are not "collecting" ─────────────

def test_status_command_reports_needs_review_canaries():
    text = (ROOT / 'commands/interspect-status.md').read_text()
    assert "status == \"needs_review\"" in text
    assert "needs_review" in text.split('### Canaries:')[0]      # counted in the overview query
    assert "canary == \"needs_review\"" in text                  # overlay table
    assert 'insufficient historical data' in text
    # Invalidated canaries have NULL baselines on purpose; never call that "collecting".
    assert 'status != "needs_review"' in text or 'status == "needs_review"' in text


# ── Item 3: failed initialization is fatal in decision commands ────────────

COMMANDS = ('interspect-status', 'interspect-propose', 'interspect-approve', 'interspect-tune')


def bash_blocks(command):
    import re
    return re.findall(r'```bash\n(.*?)```', (ROOT / f'commands/{command}.md').read_text(), re.S)


def project_with_failing_migration(template, tmp_path):
    database = tmp_path / '.clavain/interspect/interspect.db'
    database.parent.mkdir(parents=True)
    shutil.copyfile(template, database)
    with sqlite3.connect(database) as conn:
        conn.execute('UPDATE interspect_gate_schema SET version=3')   # unsupported schema: migration must fail
    return database


def run_block(block, tmp_path, tail='echo REACHED-AFTER-SETUP'):
    return subprocess.run(['bash', '-c', block + '\n' + tail], text=True, capture_output=True, timeout=30,
                          env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(ROOT), CLAUDE_PROJECT_DIR=str(tmp_path),
                                   HOME=str(tmp_path)))


@pytest.mark.parametrize('command', COMMANDS)
def test_failed_initialization_stops_the_command_preamble(template, tmp_path, command):
    project_with_failing_migration(template, tmp_path)
    preamble = next(b for b in bash_blocks(command) if '_interspect_ensure_db' in b)
    result = run_block(preamble, tmp_path)
    assert result.returncode != 0
    assert 'REACHED-AFTER-SETUP' not in result.stdout
    assert 'initialization failed' in result.stderr.lower()


@pytest.mark.parametrize('command', COMMANDS)
def test_successful_initialization_continues_the_command_preamble(template, tmp_path, command):
    database = tmp_path / '.clavain/interspect/interspect.db'
    database.parent.mkdir(parents=True)
    shutil.copyfile(template, database)
    preamble = next(b for b in bash_blocks(command) if '_interspect_ensure_db' in b)
    result = run_block(preamble, tmp_path, 'echo "DB=$DB"')
    assert result.returncode == 0, result.stderr
    assert f'DB={database}' in result.stdout


@pytest.mark.parametrize('command', COMMANDS)
def test_command_documents_stopping_on_failed_initialization(command):
    text = (ROOT / f'commands/{command}.md').read_text()
    assert 'stop' in text.split('## Locate Library')[1].split('```')[2].lower()


# ── Item 4: configured overrides path, tool target escaping ────────────────

def overrides_report_count(db):
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0]


def test_migration_reports_overrides_from_the_configured_path(db, tmp_path):
    lifted_row(db)
    project = db.parent
    (project / '.claude').mkdir()
    (project / '.claude/custom-routing.json').write_text(json.dumps(
        dict(overrides=[dict(agent='fd-a', evidence_ids=[1])])))
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=3')
    result = subprocess.run(['bash', '-c', 'source "$1"; _INTERSPECT_DB="$2"; _interspect_migrate_gate',
                             'x', str(LIB), str(db)], text=True, capture_output=True, timeout=20,
                            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project),
                                     FLUX_ROUTING_OVERRIDES_PATH='.claude/custom-routing.json'))
    assert result.returncode == 0, result.stderr
    assert overrides_report_count(db) == 1


@pytest.mark.parametrize('bad', ['/etc/passwd', '../outside.json', 'a/../../b.json'])
def test_migration_rejects_unsafe_configured_overrides_path(db, bad):
    result = subprocess.run(['bash', '-c', 'source "$1"; _INTERSPECT_DB="$2"; _interspect_migrate_gate',
                             'x', str(LIB), str(db)], text=True, capture_output=True, timeout=20,
                            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(db.parent),
                                     FLUX_ROUTING_OVERRIDES_PATH=bad))
    assert result.returncode != 0
    assert 'FLUX_ROUTING_OVERRIDES_PATH' in result.stderr


def test_tool_mode_target_is_validated_and_escaped_before_sql(template, tmp_path):
    database = tmp_path / '.clavain/interspect/interspect.db'
    database.parent.mkdir(parents=True)
    shutil.copyfile(template, database)
    block = next(b for b in bash_blocks('interspect-tune') if 'PATTERN_COUNT' in b)
    preamble = next(b for b in bash_blocks('interspect-tune') if '_interspect_ensure_db' in b)
    hostile = "x' OR 1=1; DROP TABLE evidence; --"
    result = run_block(preamble + f"\nTARGET={shlex.quote(hostile)}\n" + block, tmp_path,
                       'echo "COUNT=${PATTERN_COUNT:-unset}"')
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='evidence'").fetchone()[0] == 1
    assert result.returncode != 0 and 'COUNT=' not in result.stdout
    assert 'Invalid tool source' in result.stderr
    ok = run_block(preamble + "\nTARGET=Bash\n" + block, tmp_path, 'echo "COUNT=$PATTERN_COUNT"')
    assert ok.returncode == 0, ok.stderr
    assert 'COUNT=0' in ok.stdout
