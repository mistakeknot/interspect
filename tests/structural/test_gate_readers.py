"""Quarantined diagnostics must not influence routing, confidence or baselines."""
import json
import sqlite3
import subprocess

import pytest

from test_gate_fuzz import ROOT, shell, cli, row, explicit, template, db


@pytest.mark.parametrize('operation', ['apply', 'approve'])
def test_confidence_readers_ignore_held_rows(db, operation):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'insert', row('ordinary', override_reason='deprioritized', context='{}')).returncode == 0
    target = db.parent / 'routing-overrides.json'
    entries = [dict(agent='fd-a', action='propose')] if operation == 'approve' else []
    target.write_text(json.dumps({'version': 1, 'overrides': entries}))
    # Stop exactly at the unrelated git side-effect boundary, after real SQL and JSON generation.
    prefix = '_interspect_load_confidence() { :; }; git() { exit 77; }; '
    if operation == 'apply':
        script = prefix + '_interspect_apply_override_locked "$1" routing-overrides.json "$2" fd-a reason "[]" test "$1/message" "$_INTERSPECT_DB"'
    else:
        script = prefix + '_interspect_approve_override_locked "$1" routing-overrides.json "$2" fd-a "$1/message" "$_INTERSPECT_DB"'
    result = shell(db, script, str(db.parent), str(target))
    assert result.returncode == 77, result.stderr
    assert json.loads(target.read_text())['overrides'][0]['confidence'] == 0


def test_baseline_and_overlay_exclude_held_but_stats_include_them(db):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'insert', row('ordinary', override_reason='deprioritized', context='{}')).returncode == 0
    with sqlite3.connect(db) as connection:
        connection.executemany('INSERT INTO sessions (session_id,start_ts,project) VALUES (?,?,?)',
                               [('ordinary', '2026-01-01', 'test'), ('session-a', '2026-01-02', 'test')])
    result = shell(db, '_interspect_load_confidence() { :; }; _INTERSPECT_CANARY_MIN_BASELINE=1; '
                       '_interspect_compute_canary_baseline 2099-01-01 "" fd-a')
    assert result.returncode == 0, result.stderr
    baseline = json.loads(result.stdout)
    assert baseline['override_rate'] == 1
    assert baseline['fp_rate'] == 0
    assert baseline['finding_density'] == 1
    result = shell(db, '_interspect_db_path() { printf "%s" "$_INTERSPECT_DB"; }; _interspect_generate_overlay fd-a')
    assert result.returncode == 0, result.stderr
    assert 'deprioritized' in result.stdout and 'agent_wrong' not in result.stdout
    result = shell(db, '_interspect_low_confidence_gate_stats')
    assert result.stdout.strip() == '1|0|1|0'


def test_old_library_cannot_lift_migrated_database(db, tmp_path):
    legacy = subprocess.run(['git', 'show', 'bd84716a23:hooks/lib-interspect.sh'],
                            cwd=ROOT, text=True, capture_output=True, check=True).stdout
    old = tmp_path / 'legacy.sh'
    old.write_text(legacy)
    assert cli(db, 'insert', row()).returncode == 0
    # Preserve a legacy key to make the vulnerable lookup actually find its target.
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE evidence SET finding_key='run:p0-1'")
    script = 'unset _LIB_INTERSPECT_LOADED; source "$1"; '
    result = shell(db, script + '_interspect_corroborate_evidence fd-a run p0-1 session-b agent_wrong', str(old))
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0
    result = shell(db, script + '_interspect_insert_evidence session-b fd-a override agent_wrong "$2" interspect-correction',
                   str(old), row()['context'])
    assert 'invalid gate state' in result.stderr or 'new gate must be held' in result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1
        assert connection.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0


def test_failed_migration_refuses_low_confidence_writes(db):
    with sqlite3.connect(db) as connection:
        connection.execute('DROP TABLE interspect_gate_schema')
        # Inject a real migration failure; audit tables now survive rollback/rerun.
        connection.execute('DROP TABLE interspect_gate_audit')
        connection.execute('CREATE TABLE interspect_gate_audit (broken TEXT)')
        connection.execute("INSERT INTO evidence(ts,session_id,seq,source,event,context,project,low_confidence,quarantine_until) "
                           "VALUES ('2026-01-01','old',1,'fd-a','override','{}','test',1,4102444800)")
    result = shell(db, '_interspect_insert_evidence session-a fd-a override agent_wrong "$1" interspect-correction', row()['context'])
    assert result.returncode != 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence WHERE session_id<>'old'").fetchone()[0] == 0
        assert connection.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0


def test_unsupported_schema_ensure_does_not_partially_modify(tmp_path):
    path = tmp_path / '.clavain/interspect/interspect.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE evidence (session_id TEXT)')
        before = list(connection.iterdump())
    result = shell(path, 'CLAUDE_PROJECT_DIR="$1"; _interspect_ensure_db', str(tmp_path))
    assert result.returncode != 0
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


def test_diagnostic_sanitization_does_not_repair_gate_identity(db):
    source = 'fd-a\x1b[31m'
    ordinary = shell(db, '_interspect_insert_evidence ordinary "$1" agent_dispatch "" "{}" interspect-evidence', source)
    assert ordinary.returncode == 0
    gated = shell(db, '_interspect_insert_evidence session-a "$1" override agent_wrong "$2" interspect-correction',
                  source, row()['context'])
    assert gated.returncode == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT source FROM evidence ORDER BY id').fetchall() == [('fd-a',), ('fd-a',)]
        assert connection.execute('SELECT gate_version,gate_key FROM evidence WHERE low_confidence=1').fetchone() == (0, None)


def test_invalid_explicit_request_does_not_create_database(tmp_path):
    path = tmp_path / 'absent.db'
    result = cli(path, 'corroborate', explicit(actor='\n'))
    assert result.returncode != 0 and result.stdout == ''
    assert not path.exists()


def test_explicit_database_failure_is_not_zero_affected(db):
    assert cli(db, 'insert', row()).returncode == 0
    with sqlite3.connect(db) as connection:
        connection.execute('BEGIN IMMEDIATE')
        result = shell(db, '_interspect_corroborate_evidence fd-a run p0-1 session-b agent_wrong')
        assert result.returncode == 2 and result.stdout == ''
        connection.rollback()
        assert connection.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0


def test_canonical_source_aliases_reach_routing_readers(db):
    for session, source in [('Session-A', 'FD-A'), ('SESSION-A', 'interflux:fd-a'),
                            ('session-b', 'INTERFLUX:REVIEW:FD-A')]:
        assert cli(db, 'insert', row(session, source=source)).returncode == 0
    result = shell(db, '_interspect_is_routing_eligible fd-a')
    assert result.returncode == 0 and result.stdout.strip() == 'eligible'
    result = shell(db, '_interspect_get_classified_patterns')
    assert result.returncode == 0
    assert result.stdout.startswith('fd-a|override|agent_wrong|3|2|'), result.stdout


def test_canary_samples_do_not_treat_held_evidence_as_a_use(db):
    assert cli(db, 'insert', row()).returncode == 0
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO canary (file,commit_sha,group_id,applied_at,window_uses,window_expires_at,status) "
                           "VALUES ('test','test','fd-a','2026-01-01',20,'2099-01-01','active')")
    result = shell(db, '_interspect_record_canary_sample session-a')
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM canary_samples').fetchone()[0] == 0
    assert cli(db, 'insert', row('session-b')).returncode == 0
    result = shell(db, '_interspect_record_canary_sample session-a')
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM canary_samples').fetchone()[0] == 1
