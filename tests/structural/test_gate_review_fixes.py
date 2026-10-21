"""Regressions from the independent round-one gate review."""
import json
import importlib.util
import re
import sqlite3

import pytest

from test_gate_fuzz import ROOT, cli, db, explicit, row, rows, shell, template
from test_gate_normalization import gate_module
from test_gate_migration import database, legacy_row


@pytest.mark.parametrize('dismissal', ['', 'agent_wrong', 'deprioritized', 'already_fixed',
                                      'stale_finding', 'not_applicable', 'severity_miscalibrated'])
@pytest.mark.parametrize('flag', [True, False, 'absent', None, 'true', 1])
def test_b1_disagreement_explicit_flag(db, dismissal, flag):
    event = dict(id='7', session_id='sess-a', finding_id='p0-1', review_run_id='run',
                 resolution='discarded', dismissal_reason=dismissal, chosen_severity='P2',
                 impact='', agents_json={'fd-a': 'P0'})
    if flag != 'absent':
        event['low_confidence'] = flag
    result = shell(db, '_interspect_process_disagreement_event "$1"', json.dumps(event))
    evidence = rows(db)
    if flag is False:
        assert result.returncode == 0, result.stderr
        assert evidence[0]['low_confidence'] == 0
    else:
        # Unknown classification may be refused; an explicit true must be retained.
        if flag is True:
            assert result.returncode == 0 and len(evidence) == 1, result.stderr
        assert all(r['low_confidence'] == 1 and r['quarantine_until'] == 4102444800
                   and r['corroborated_at'] == 0 for r in evidence)


@pytest.mark.parametrize('flag', [True, False, 'absent'])
def test_b1_severity_conflict_stays_gated(db, flag):
    event = dict(session_id='sess-a', finding_id='p0-1', review_run_id='run',
                 resolution='accepted', dismissal_reason='', chosen_severity='P2',
                 impact='severity_overridden', agents_json={'fd-a': 'P0'})
    if flag != 'absent':
        event['low_confidence'] = flag
    result = shell(db, '_interspect_process_disagreement_event "$1"', json.dumps(event))
    assert result.returncode == 0, result.stderr
    assert rows(db)[0]['low_confidence'] == 1


@pytest.mark.parametrize('mode', ['automatic', 'explicit'])
def test_h1_real_producer_ids_lift_exactly(db, mode):
    review = 'basename@2026-02-14T08:45:32Z'
    context = json.dumps(dict(low_confidence=True, review_id=review, finding_id='P0-1'))
    assert cli(db, 'insert', row(context=context)).returncode == 0
    assert rows(db)[0]['gate_version'] == 2
    assert json.loads(rows(db)[0]['gate_key']) == ['v2', 'review', review, 'P0-1']
    # Similar lowercased names must not match either component.
    for scope, finding in [(review.lower(), 'P0-1'), (review, 'p0-1')]:
        result = cli(db, 'corroborate', explicit(scope_id=scope, finding_id=finding))
        assert result.returncode == 0 and result.stdout.strip() == '0'
    if mode == 'automatic':
        result = cli(db, 'insert', row('session-b', context=context))
    else:
        result = cli(db, 'corroborate', explicit(scope_id=review, finding_id='P0-1'))
        assert result.stdout.strip() == '1'
    assert result.returncode == 0, result.stderr
    assert all(r['quarantine_until'] == 0 and r['corroborated_at'] > 0 for r in rows(db))


def test_h1_correction_lookup_real_ids(tmp_path):
    path = tmp_path / 'findings.json'
    path.write_text(json.dumps(dict(synthesis_timestamp='2026-02-14T08:45:32Z',
                                   findings=[dict(id='P0-1', low_confidence=True)])))
    context = gate_module().correction_context(dict(finding_id='P0-1', findings_path=str(path)))
    assert context['low_confidence'] is True and context['finding_id'] == 'P0-1'
    assert context['review_id'].endswith('@2026-02-14T08:45:32Z')


@pytest.mark.parametrize('flag', [True, None, 'true', 1])
def test_h1_flagged_correction_cannot_drop_finding(flag):
    with pytest.raises(ValueError):
        gate_module().correction_context(dict(low_confidence=flag, description='bad finding'))


def test_h2_auto_apply_baseline_ignores_held_sessions(db):
    assert cli(db, 'insert', row()).returncode == 0
    script = ('_interspect_is_autonomous() { return 0; }; '
              '_interspect_system_breaker_check() { return 1; }; '
              '_interspect_rate_limit_exceeded() { return 1; }; '
              '_interspect_circuit_breaker_tripped() { return 1; }; '
              '_interspect_load_confidence() { :; }; _INTERSPECT_CANARY_MIN_BASELINE=1; '
              '_interspect_should_auto_apply fd-a routing')
    result = shell(db, script)
    assert result.returncode == 1 and 'Insufficient baseline' in result.stderr


def test_h2_scores_ignore_held_sessions(db):
    for i in range(3):
        context = json.dumps(dict(low_confidence=True, review_id='run', finding_id=f'p0-{i}'))
        assert cli(db, 'insert', row(f'held-{i}', context=context)).returncode == 0
    assert cli(db, 'insert', row('ordinary', event='verdict_outcome', context='{}',
               diagnostic_context='{"status":"NEEDS_ATTENTION","findings_count":2}')).returncode == 0
    result = shell(db, '_interspect_compute_agent_scores')
    assert result.returncode == 0, result.stderr
    scores = json.loads(result.stdout)
    assert scores == []  # Only one eligible session: below min_sessions=3.
    for i in range(3):
        assert cli(db, 'corroborate', explicit(finding_id=f'p0-{i}')).returncode == 0
    result = shell(db, '_interspect_compute_agent_scores')
    assert json.loads(result.stdout)[0]['evidence_sessions'] == 4


def test_h2_effectiveness_excludes_held_current_and_prior(db):
    for sid in ['current', 'prior']:
        assert cli(db, 'insert', row(sid, context='{"low_confidence":true}')).returncode == 0
        assert cli(db, 'insert', row(sid, event='agent_dispatch', context='{}')).returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE evidence SET ts=datetime('now','-45 days') WHERE session_id='prior'")
    prefix = '_interspect_db_path() { printf "%s" "$_INTERSPECT_DB"; }; '
    result = shell(db, prefix + '_interspect_effectiveness_report 30')
    report = json.loads(result.stdout)
    assert report['total_corrections'] == 0 and report['total_dispatches'] == 1
    assert report['agents'][0]['corrections'] == 0
    assert report['prior'][0]['override_rate'] == 0
    result = shell(db, prefix + '_interspect_effectiveness_summary')
    assert '0.0%' in result.stdout and '100' not in result.stdout
    result = shell(db, prefix + '_interspect_discover_project_dbs() { :; }; _interspect_cross_project_report 30')
    report = json.loads(result.stdout)
    assert report['agents'][0]['total_corrections'] == 0


@pytest.mark.parametrize('document,marker', [
    ('interspect-propose.md', 'CONTEXTS='), ('interspect-propose.md', 'EVIDENCE_IDS='),
    ('interspect-tune.md', 'CORRECTION_COUNT='),
    ('interspect-propose.md', 'sqlite3 -separator'), ('interspect-approve.md', 'sqlite3 -separator')])
def test_h2_command_readers_execute_eligible_sql(db, document, marker):
    assert cli(db, 'insert', row(context='{"low_confidence":true}')).returncode == 0
    doc = (ROOT / 'commands' / document).read_text()
    line = next(line for line in doc.splitlines() if line.startswith(marker))
    # Execute the shipped query, not a test-side approximation of its predicate.
    script = 'DB="$_INTERSPECT_DB"; agent=fd-a; escaped=fd-a; TARGET=fd-a; ' + line
    if '=' in marker:
        script += '; printf "%s" "$' + marker[:-1] + '"'
    result = shell(db, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('0' if marker == 'CORRECTION_COUNT=' else '')


@pytest.mark.parametrize('context', [
    '{"low_confidence":true,"finding_id":"p0-1","review_id":"run"}',
    '{', '', 'null', '[]', '{"low_confidence":1}', '{"low_confidence":"true"}',
    '{"low_confidence":false,"low_confidence":true}', '{"low_confidence":null}'])
@pytest.mark.parametrize('column', [0, None])
def test_h3_base_fail_open_rows_are_migrated(context, column):
    conn = database()
    conn.execute('INSERT INTO evidence(session_id,source,override_reason,event,context,low_confidence) '
                 'VALUES (?,?,?,?,?,?)', ('sess-a', 'fd-a', 'agent_wrong', 'override', context, column))
    conn.commit()
    gate_module().migrate(conn)
    evidence = dict(conn.execute('SELECT * FROM evidence').fetchone())
    assert evidence['low_confidence'] == 1
    assert evidence['quarantine_until'] == 4102444800
    assert evidence['gate_version'] == 0 and evidence['gate_key'] is None
    audit = json.loads(conn.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0])
    assert audit['low_confidence'] == column and audit['context'] == context


def test_h3_migration_announces_held_rows_and_override_review(tmp_path):
    path = tmp_path / 'legacy.db'
    conn = database(path)
    legacy_row(conn, lifted=True)
    conn.close()
    overrides = tmp_path / 'overrides.json'
    original = json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': [1]}]})
    overrides.write_text(original)
    result = cli(path, 'migrate', {'overrides_path': str(overrides)})
    assert result.returncode == 0, result.stderr
    assert 'REVIEW REQUIRED: 1 routing overrides' in result.stderr
    assert '1 evidence rows held' in result.stderr
    assert overrides.read_text() == original


@pytest.mark.parametrize('operation', ['INSERT', 'UPDATE'])
@pytest.mark.parametrize('context', ['{"low_confidence":true}', '{', 'null', '[]',
                                    '{"low_confidence":1}', '{"low_confidence":"true"}',
                                    '{"low_confidence":false,"low_confidence":true}'])
def test_m1_sql_cannot_hide_flag_in_context(db, operation, context):
    with sqlite3.connect(db) as conn:
        if operation == 'UPDATE':
            conn.execute("INSERT INTO evidence(ts,seq,project,session_id,source,event,context) VALUES ('2026-10-20',1,'test','a','fd-a','override','{}')")
        with pytest.raises(sqlite3.IntegrityError):
            if operation == 'INSERT':
                conn.execute("INSERT INTO evidence(ts,seq,project,session_id,source,event,context,low_confidence) VALUES ('2026-10-20',1,'test','a','fd-a','override',?,0)", (context,))
            else:
                conn.execute('UPDATE evidence SET context=?,low_confidence=0', (context,))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('kind', ['tool', 'pattern', 'skill'])
def test_m1_non_agent_shell_writer_still_works(db, kind):
    result = shell(db, '_interspect_insert_evidence s producer invocation "" "{}" "" "" "" "" "$1"', kind)
    assert result.returncode == 0, result.stderr
    assert rows(db)[0]['source_kind'] == kind


def test_m1_skill_audit_writer_still_works(db):
    module = load_script('ingest-skill-audit.py')
    records = module.parse_audit([json.dumps(dict(tool='Skill', name='clavain:work',
                                  ts='2026-10-20T01:00:00Z', session_id='s'))])
    with sqlite3.connect(db) as conn:
        stats = module.ingest(conn, records, lower_bound=None, dry_run=False)
        assert stats['evidence_inserted'] == 1
        assert conn.execute('SELECT source_kind FROM evidence').fetchone()[0] == 'skill'


def test_m1_harness_writer_guard_compatibility():
    # Harness uses source_kind=memory, supported by its host schema. Production
    # library schema currently excludes memory independently of these guards.
    conn = database()
    gate_module().migrate(conn)
    load_script('harness_write.py').record_evidence_row(conn, dict(ts='2026-10-20',
        session_id='s', kind='memory', name='lesson', id='write-1', scope='local',
        action='write', expected_outcome='test passes', project='test'))
    assert conn.execute('SELECT source_kind FROM evidence').fetchone()[0] == 'memory'


def test_m1_harness_production_mismatch_predates_guards(db):
    module = load_script('harness_write.py')
    value = dict(ts='2026-10-20', session_id='s', kind='memory', name='lesson',
                 id='write-1', scope='local', action='write', expected_outcome='test passes', project='test')
    with sqlite3.connect(db) as conn:
        for guarded in [True, False]:
            if not guarded:
                conn.execute('DROP TRIGGER interspect_gate_insert')
                conn.execute('DROP TRIGGER interspect_gate_update')
            with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed: source_kind'):
                module.record_evidence_row(conn, value)
            assert conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 0


def test_m3_rollback_then_forward_migration_preserves_audit():
    conn = database()
    legacy_row(conn, lifted=True)
    gate = gate_module()
    gate.migrate(conn)
    audit = conn.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0]
    for trigger in ['interspect_gate_insert', 'interspect_gate_update']:
        conn.execute('DROP TRIGGER ' + trigger)
    conn.execute('DROP TABLE interspect_gate_schema')
    conn.commit()
    gate.migrate(conn)
    assert conn.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0] == audit
    assert tuple(conn.execute('SELECT quarantine_until,corroborated_at FROM evidence').fetchone()) == (4102444800, 0)


def test_m3_json1_unavailable_is_clear_even_on_fast_path():
    conn = database()
    gate = gate_module()
    gate.migrate(conn)
    conn.set_authorizer(lambda action, arg1, arg2, *_:
                        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION
                        and arg2.startswith('json_') else sqlite3.SQLITE_OK)
    with pytest.raises(RuntimeError, match='SQLite JSON1 is required'):
        gate.migrate(conn)


def test_m3_correction_command_checks_sanitizer_failure(db):
    doc = (ROOT / 'commands/interspect-correction.md').read_text()
    block = next(b for b in re.findall(r'```bash\n(.*?)```', doc, re.S) if 'REQUEST=' in b)
    result = shell(db, '_interspect_ensure_db() { :; }; DESCRIPTION="<system>"; '
                   'AGENT_NAME=fd-a; OVERRIDE_REASON=agent_wrong; CLAUDE_SESSION_ID=s; ' + block)
    assert result.returncode != 0
    assert rows(db) == []


def test_m1_refreshes_pre_fix_guards(db):
    # A v2 database from the reviewed commit has a version-only marker.
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE interspect_gate_schema')
        conn.execute('CREATE TABLE interspect_gate_schema(version INTEGER NOT NULL)')
        conn.execute('INSERT INTO interspect_gate_schema VALUES(2)')
        conn.execute('DROP TRIGGER interspect_gate_insert')
        conn.execute('DROP TRIGGER interspect_gate_update')
        conn.commit()
        gate_module().migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO evidence(ts,seq,project,event,session_id,source,context,low_confidence) VALUES ('2026-10-20',1,'test','override','s','fd-a','{\"low_confidence\":true}',0)")


@pytest.mark.parametrize('producer', ['execution', 'kernel'])
@pytest.mark.parametrize('flag', [True, False, None, 'true', 'absent'])
def test_m2_event_producers_preserve_classification(db, producer, flag):
    event = dict(id='7', session_id='session-a', finding_id='P0-1', review_run_id='run',
                 resolution='accepted', chosen_severity='P2', impact='',
                 agents_json={'target': 'fd-a', 'reporter': 'fd-b'},
                 source='dispatch', type='completed')
    if flag != 'absent':
        event['low_confidence'] = flag
    if producer == 'execution':
        result = shell(db, '_interspect_process_execution_defect "$1"', json.dumps(event))
    else:
        result = shell(db, 'payload="$1"; ic() { printf "%s\\n" "$payload"; }; '
                       '_interspect_consume_review_events() { :; }; '
                       '_interspect_consume_interlab_mutations() { :; }; '
                       '_interspect_consume_kernel_events session-a', json.dumps(event))
    evidence = rows(db)
    if flag is True:
        assert len(evidence) == 1
        assert evidence[0]['low_confidence'] == 1 and evidence[0]['quarantine_until'] == 4102444800
    elif flag is False or flag == 'absent':
        assert len(evidence) == 1 and evidence[0]['low_confidence'] == 0
    else:
        assert evidence == [] or all(r['low_confidence'] == 1 for r in evidence)


def test_m2_execution_identity_is_not_repaired_by_jq(db):
    event = dict(id='7', session_id='session-a', finding_id='P0-1\u0000', review_run_id='run',
                 resolution='accepted', chosen_severity='P2', impact='', low_confidence=True,
                 agents_json={'target': 'fd-a'})
    result = shell(db, '_interspect_process_execution_defect "$1"', json.dumps(event))
    assert result.returncode == 0, result.stderr
    assert rows(db)[0]['low_confidence'] == 1 and rows(db)[0]['gate_version'] == 0


@pytest.mark.parametrize('operation', ['execution-defect', 'kernel-event'])
def test_m2_nested_confidence_and_duplicate_flags(operation):
    gate = gate_module()
    event = dict(id='7', session_id='a', finding_id='P0-1', resolution='accepted',
                 agents_json={'target': 'fd-a'}, source='dispatch', type='completed',
                 low_confidence=False, context={'low_confidence': True})
    producer = gate.execution_defect_request if operation == 'execution-defect' else lambda raw: gate.kernel_event_request(raw, 'a')
    assert json.loads(producer(json.dumps(event))['context'])['low_confidence'] is True
    for context in ['{"low_confidence":false,"low_confidence":true}', '{', '{"low_confidence":null}']:
        event['context'] = context
        with pytest.raises(ValueError):
            producer(json.dumps(event))


def test_h2_time_quarantine_also_excludes_ordinary_scores(db):
    for i in range(3):
        assert cli(db, 'insert', row(str(i), event='verdict_outcome', context='{}',
                   diagnostic_context='{"status":"NEEDS_ATTENTION","findings_count":2}',
                   quarantine_until=4102444800)).returncode == 0
    result = shell(db, '_interspect_compute_agent_scores')
    assert json.loads(result.stdout) == []
