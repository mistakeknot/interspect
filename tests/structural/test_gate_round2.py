"""Behavioral reproductions from independent gate review isp7 (round two)."""
import json
import sqlite3

import pytest

from test_gate_fuzz import ROOT, cli, db, row, rows, shell, template
from test_gate_normalization import gate_module
from test_gate_migration import database, legacy_row


@pytest.mark.parametrize('alias', ['', 'e', 'evidence', 'row_2'])
def test_r3_python_and_shell_predicates_match(db, alias):
    result = shell(db, '_interspect_eligible_sql "$1"', alias)
    assert result.returncode == 0, result.stderr
    assert result.stdout == gate_module().eligible_sql(alias)


@pytest.mark.parametrize('nested', [True, {'low_confidence': True},
                                  '{"low_confidence":true}'])
def test_r1_nested_disagreement_stays_held(db, nested):
    if nested is True:
        nested = {'low_confidence': nested}
    event = dict(session_id='sess-a', finding_id='P0-1', review_run_id='run',
                 resolution='discarded', dismissal_reason='agent_wrong',
                 chosen_severity='P2', agents_json={'fd-a': 'P0'},
                 low_confidence=False, context=nested)
    result = shell(db, '_interspect_process_disagreement_event "$1"', json.dumps(event))
    assert result.returncode == 0, result.stderr
    evidence = rows(db)
    assert len(evidence) == 1
    assert evidence[0]['low_confidence'] == 1
    assert evidence[0]['quarantine_until'] == 4102444800
    assert evidence[0]['corroborated_at'] == 0


@pytest.mark.parametrize('nested', [None, [], '{', '{"low_confidence":1}',
                                  {'low_confidence': None}, {'low_confidence': 'true'}])
def test_r1_invalid_nested_disagreement_rejected(db, nested):
    event = dict(session_id='sess-a', finding_id='P0-1', review_run_id='run',
                 resolution='discarded', dismissal_reason='agent_wrong',
                 chosen_severity='P2', agents_json={'fd-a': 'P0'},
                 low_confidence=False, context=nested)
    result = shell(db, '_interspect_process_disagreement_event "$1"', json.dumps(event))
    assert result.returncode != 0
    assert rows(db) == []


def held_delegations(db, verdict='pass'):
    for i in range(3):
        context = json.dumps(dict(low_confidence=True, category='held-category',
                                  tier='deep', verdict=verdict, retry_needed=1, duration_s=4))
        result = cli(db, 'insert', row(f'held-{i}', source='codex-delegate',
                     event='delegation_outcome', context=context, diagnostic_context=context))
        assert result.returncode == 0, result.stderr
    assert all(r['gate_version'] == 0 and r['quarantine_until'] == 4102444800 for r in rows(db))


def test_r2_held_delegations_not_sufficient(db):
    held_delegations(db)
    result = shell(db, '_interspect_compute_delegation_stats')
    assert result.returncode == 0, result.stderr
    assert not json.loads(result.stdout).get('sufficient_data', False)
    assert json.loads(result.stdout).get('total_delegations', 0) == 0
    # An eligible row still counts.
    result = cli(db, 'insert', row('ordinary', source='codex-delegate',
                 event='delegation_outcome', context='{}', diagnostic_context='{"verdict":"pass"}'))
    assert result.returncode == 0, result.stderr
    result = shell(db, '_interspect_compute_delegation_stats')
    assert json.loads(result.stdout) == {'total_delegations': 1, 'sufficient_data': False}


@pytest.mark.parametrize('query_index', range(6))
def test_r2_delegation_command_queries_exclude_held(db, query_index):
    import re
    held_delegations(db, verdict='fail' if query_index == 4 else 'pass')
    doc = (ROOT / 'commands/delegation-status.md').read_text()
    queries = re.findall(r'sqlite3[^\n]*"\$DB" "(.*?)"', doc, re.S)
    assert len(queries) == 6
    result = shell(db, 'sqlite3 "$_INTERSPECT_DB" "' + queries[query_index] + '"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('0' if query_index < 3 else '')


@pytest.mark.parametrize('reader', ['baseline', 'recurrence', 'tune'])
def test_r3_quarantined_tool_rows_excluded(db, reader):
    result = shell(db, 'INTERSPECT_QUARANTINE_HOURS=48; '
                   '_interspect_insert_evidence sess tool:bash tool_error "" "{}" tool-time-pattern "" "" "" tool')
    assert result.returncode == 0, result.stderr
    assert rows(db)[0]['quarantine_until'] > 0
    if reader == 'baseline':
        script = '_interspect_compute_tool_baseline tool:bash'
        expected = '{}'
    elif reader == 'recurrence':
        script = '_interspect_count_tool_pattern_recurrence tool:bash 2000-01-01'
        expected = '0'
    else:
        doc = (ROOT / 'commands/interspect-tune.md').read_text()
        query = next(line for line in doc.splitlines() if line.startswith('PATTERN_COUNT='))
        script = 'DB="$_INTERSPECT_DB"; TARGET=tool:bash; ' + query + '; echo "$PATTERN_COUNT"'
        expected = '0'
    result = shell(db, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE evidence SET quarantine_until=0')
    result = shell(db, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('{"tool_error":1}' if reader == 'baseline' else '1')


@pytest.mark.parametrize('reader', ['collectors', 'scoring', 'signals'])
def test_r3_quarantined_skill_invocations_excluded(db, reader):
    import importlib.util
    import sys
    for i in range(3):
        result = cli(db, 'insert', row(f'skill-{i}', source='test:skill', source_kind='skill',
                     event='skill_invocation', source_event_id=f'inv-{i}', context='{}',
                     quarantine_until=4102444800))
        assert result.returncode == 0, result.stderr
    sys.path.insert(0, str(ROOT / 'scripts'))
    module_path = 'signals/_common.py' if reader == 'collectors' else 'score-skills.py'
    spec = importlib.util.spec_from_file_location('round2_' + reader, ROOT / 'scripts' / module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with sqlite3.connect(db) as conn:
        for i in range(3):
            conn.execute('INSERT INTO skill_signals(skill_name,session_id,invocation_id,'
                         'signal_kind,value,observed_at) VALUES (?,?,?,?,?,?)',
                         ('test:skill', f'skill-{i}', f'inv-{i}', 'error', 1, '2026-01-01'))
        def query():
            if reader == 'collectors':
                return module.pending_evidence(conn, 'tokens')
            if reader == 'signals':
                return module.signal_rows(conn, 'test:skill', '2000-01-01')
            return module.qualifying_skills(conn, '2000-01-01', 3)
        assert query() == []
        conn.execute('UPDATE evidence SET quarantine_until=0')
        assert len(query()) == (1 if reader == 'scoring' else 3)


@pytest.mark.parametrize('name,event,expected', [
    ('_interspect_decomposition_calibration_ready', 'decomposition_outcome', '0'),
    ('_interspect_review_calibration_ready', 'review_phase_outcome', ''),
    ('_interspect_calibrate_reviews', 'review_phase_outcome', ''),
    ('_interspect_compute_plan_execution_stats', 'plan_execution_outcome', '')])
def test_r3_expired_flagged_calibration_rows_still_excluded(db, name, event, expected):
    import re
    context = json.dumps(dict(low_confidence=True, phase='plan', complexity='simple',
                              author_model='a', executor_model='b', validator_model='c',
                              **{'pass': True}))
    for i in range(3):
        assert cli(db, 'insert', row(f'held-{i}', event=event,
                   context=context, diagnostic_context=context)).returncode == 0
    # Emulate legacy expiry without corroboration. Readers must enforce both gates.
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TRIGGER interspect_gate_update')
        conn.execute('UPDATE evidence SET quarantine_until=0')
    lib = (ROOT / 'hooks/lib-interspect.sh').read_text()
    body = lib.split(name + '() {', 1)[1].split('\n}', 1)[0]
    query = re.search(r'sqlite3[^\n]*"\$db" "(.*?)"', body, re.S).group(1)
    result = shell(db, 'now=$(date +%s); sqlite3 "$_INTERSPECT_DB" "' + query + '"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_r3_token_baseline_query_excludes_quarantined_sessions(db):
    import ast
    for i, quarantine in enumerate([0, 4102444800]):
        assert cli(db, 'insert', row(f'skill-{i}', source='test:skill', source_kind='skill',
                   event='skill_invocation', source_event_id=f'inv-{i}', context='{}',
                   quarantine_until=quarantine)).returncode == 0
    # Evaluate the shipped SQL expression, including its shared predicate, with
    # no dependency on the optional external cass service or transcript corpus.
    tree = ast.parse((ROOT / 'scripts/signals/collect_tokens.py').read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == 'sessions_running')
    call = next(n for n in ast.walk(function) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == 'execute')
    query = eval(compile(ast.Expression(call.args[0]), '<shipped query>', 'eval'),
                 vars(gate_module()))
    with sqlite3.connect(db) as conn:
        assert conn.execute(query, ('test:skill',)).fetchall() == [('skill-0',)]


@pytest.mark.parametrize('reference', [1, '1', 1.0, '01', '1.0', '1e0', ' 1 ', True])
@pytest.mark.parametrize('previous', ['lifted', 'context_only'])
def test_r4_reports_all_formerly_eligible_override_references(tmp_path, capsys, reference, previous):
    conn = database()
    if previous == 'lifted':
        legacy_row(conn, lifted=True)
    else:
        conn.execute('INSERT INTO evidence(session_id,source,override_reason,context,'
                     'low_confidence,quarantine_until,corroborated_at) VALUES (?,?,?,?,0,0,0)',
                     ('sess-a', 'fd-a', 'agent_wrong', '{"low_confidence":true}'))
        conn.commit()
    path = tmp_path / 'overrides.json'
    original = json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': [reference]}]})
    path.write_text(original)
    gate_module().migrate(conn, path)
    assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == 1
    assert conn.execute('SELECT quarantine_until FROM evidence').fetchone()[0] == 4102444800
    assert path.read_text() == original
    assert 'previously eligible evidence now held: [1]' in capsys.readouterr().err


def test_r4_repair_uses_retained_audit(tmp_path):
    conn = database()
    conn.execute('INSERT INTO evidence(session_id,source,override_reason,context,'
                 'low_confidence,quarantine_until,corroborated_at) VALUES (?,?,?,?,0,0,0)',
                 ('sess-a', 'fd-a', 'agent_wrong', '{"low_confidence":true}'))
    conn.commit()
    gate = gate_module()
    gate.migrate(conn)
    # A previous revision already held the row but omitted override reporting.
    conn.execute('UPDATE interspect_gate_schema SET guard_revision=1')
    conn.commit()
    path = tmp_path / 'overrides.json'
    path.write_text(json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': ['1']}]}))
    gate.migrate(conn, path)
    assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == 1
    before = list(conn.iterdump())
    gate.migrate(conn, path)
    assert list(conn.iterdump()) == before


def test_r4_structured_non_ids_do_not_break_migration(tmp_path):
    conn = database()
    legacy_row(conn, lifted=True)
    path = tmp_path / 'overrides.json'
    path.write_text(json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids':
                                             [None, {}, [], '1oops', 1.5, '2']}]}))
    gate_module().migrate(conn, path)
    assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == 0


@pytest.mark.parametrize('quarantine', [None, 1, 4102444800])
def test_r4_expiry_reporting_includes_null_and_elapsed_only(tmp_path, quarantine):
    conn = database()
    conn.execute('INSERT INTO evidence(session_id,source,override_reason,context,'
                 'low_confidence,quarantine_until,corroborated_at) VALUES (?,?,?,?,0,?,0)',
                 ('sess-a', 'fd-a', 'agent_wrong', '{"low_confidence":true}', quarantine))
    conn.commit()
    path = tmp_path / 'overrides.json'
    path.write_text(json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': [1]}]}))
    gate_module().migrate(conn, path)
    assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == (
        0 if quarantine == 4102444800 else 1)


def test_r5_generated_review_collision_pairs(db, tmp_path):
    contexts = []
    for base, timestamp in [('a@b', 'c'), ('a', 'b@c')]:
        path = tmp_path / base / 'findings.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(synthesis_timestamp=timestamp,
                        findings=[dict(id='P0-1', low_confidence=True)])))
        try:
            contexts.append(gate_module().correction_context(
                dict(finding_id='P0-1', findings_path=str(path))))
        except ValueError:
            continue  # Rejecting ambiguous components is an allowed encoding policy.
    assert len({c['review_id'] for c in contexts}) == len(contexts)
    for i, context in enumerate(contexts):
        result = cli(db, 'insert', row(f'session-{i}', context=json.dumps(context)))
        assert result.returncode == 0, result.stderr
    assert all(r['corroborated_at'] == 0 and r['quarantine_until'] == 4102444800
               for r in rows(db))
