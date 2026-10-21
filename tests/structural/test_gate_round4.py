"""Regressions for independent review isp8, including real earlier-head output."""
import json
import sqlite3
import importlib.util
import sys
import re
from datetime import datetime, timezone

import pytest

from test_gate_fuzz import ROOT, cli, db, row, rows, shell, template
from test_gate_normalization import gate_module


@pytest.mark.parametrize('format', ['v2', 'legacy'])
def test_old_generated_review_cannot_corroborate_new_pair(db, tmp_path, format):
    snapshot = json.loads((ROOT / 'tests/fixtures/gate-ambiguous-old-review.json').read_text())
    old = snapshot['row']
    assert snapshot['basename'] == 'a@b' and snapshot['synthesis_timestamp'] == 'c'
    assert old['gate_scope_id'] == 'a@b@c'
    if format == 'legacy':
        old = {k: v for k, v in old.items() if not k.startswith('gate_')}
        old['finding_key'] = 'a@b@c:P0-1'
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TRIGGER interspect_gate_insert')
        conn.execute('INSERT INTO evidence (' + ','.join(old) + ') VALUES (' +
                     ','.join('?' for _ in old) + ')', tuple(old.values()))
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=2')
        conn.commit()
        gate_module().migrate(conn)
    path = tmp_path / 'a' / 'findings.json'
    path.parent.mkdir()
    path.write_text(json.dumps(dict(synthesis_timestamp='b@c',
                    findings=[dict(id='P0-1', low_confidence=True)])))
    context = gate_module().correction_context(dict(findings_path=str(path), finding_id='P0-1'))
    result = cli(db, 'insert', row('new-session', context=json.dumps(context)))
    assert result.returncode == 0, result.stderr
    evidence = rows(db)
    assert len(evidence) == 2
    assert all(r['corroborated_at'] == 0 and r['quarantine_until'] == 4102444800 for r in evidence)
    assert evidence[0]['gate_version'] == 0 and evidence[0]['gate_key'] is None
    # Repeated repair must not resurrect the discarded key from diagnostics.
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM interspect_gate_schema')
        conn.commit()
        gate_module().migrate(conn)
    assert rows(db)[0]['gate_key'] is None


def skill_signals(db, state, n=1, value=0.0):
    """Real skill tuples, including ordinary quarantine and migrated flags."""
    with sqlite3.connect(db) as conn:
        if state == 'migrated':
            conn.execute('DROP TRIGGER interspect_gate_insert')
        for i in range(n):
            conn.execute("INSERT INTO evidence (session_id,source,event,source_kind,source_event_id,"
                         "context,ts,low_confidence,quarantine_until,seq,project) VALUES (?,?,?,?,?,?,?,?,?,1,'')",
                         (f'skill-{i}', 'test:skill', 'skill_invocation', 'skill', f'inv-{i}',
                          '{}', datetime.now(timezone.utc).isoformat(), int(state == 'migrated'),
                          4102444800 if state == 'quarantined' else 0))
            conn.execute("INSERT INTO skill_signals (skill_name,session_id,invocation_id,"
                         "signal_kind,value,observed_at) VALUES (?,?,?,?,?,datetime('now'))",
                         ('test:skill', f'skill-{i}', f'inv-{i}', 'error', value))
        if state == 'migrated':
            conn.execute('DELETE FROM interspect_gate_schema')
            conn.commit()
            gate_module().migrate(conn)
        elif state == 'wrong_skill':
            conn.execute("UPDATE evidence SET source='other:skill'")
        elif state == 'wrong_session':
            conn.execute("UPDATE evidence SET session_id='other-session'")
        elif state == 'orphan':
            conn.execute('DELETE FROM evidence')


@pytest.mark.parametrize('state', ['quarantined', 'migrated', 'wrong_skill', 'wrong_session', 'orphan'])
@pytest.mark.parametrize('reader', ['means', 'count', 'action', 'overlay', 'command'])
def test_skill_tune_requires_eligible_backing_tuple(db, state, reader):
    skill_signals(db, state)
    if reader == 'command':
        doc = (ROOT / 'commands/interspect-tune.md').read_text()
        query = next(line for line in doc.splitlines() if line.startswith('SIGNAL_COUNT='))
        script = 'DB="$_INTERSPECT_DB"; TARGET=test:skill; ' + query + '; echo "$SIGNAL_COUNT"'
    else:
        script = {'means': '_interspect_skill_signal_means test:skill',
                  'count': '_interspect_skill_invocation_count test:skill',
                  'action': '_interspect_select_skill_action test:skill',
                  'overlay': '_interspect_generate_skill_overlay test:skill skill_md_body_rewrite'}[reader]
    result = shell(db, script)
    if reader == 'overlay':
        assert result.returncode != 0 and 'PROPOSED' not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        if reader == 'action':
            assert result.stdout.strip() != 'skill_md_body_rewrite'
        else:
            assert result.stdout.strip() == ('' if reader == 'means' else '0')


def test_eligible_skill_tune_still_proposes_body_rewrite(db):
    skill_signals(db, 'eligible')
    result = shell(db, '_interspect_skill_invocation_count test:skill; '
                   '_interspect_generate_skill_overlay test:skill')
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('1\n') and 'PROPOSED (body rewrite' in result.stdout


@pytest.mark.parametrize('alias', ['s', 'signals_2'])
def test_skill_signal_predicate_parity(db, alias):
    result = shell(db, '_interspect_skill_signal_eligible_sql "$1"', alias)
    assert result.returncode == 0, result.stderr
    assert result.stdout == gate_module().skill_signal_eligible_sql(alias)


def refinement_module():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('round4_refinement', ROOT / 'scripts/infer-skill-goals.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('state', ['quarantined', 'migrated', 'wrong_skill', 'wrong_session', 'orphan', 'eligible'])
def test_goal_refinement_requires_eligible_signals(db, state):
    skill_signals(db, state, n=20, value=1.0)
    module = refinement_module()
    weights = dict(speed=0.6, precision=0.2, completeness=0.2)
    with sqlite3.connect(db) as conn:
        module.upsert_goals(conn, skill_name='test:skill', weights=weights,
                            classified_from='skill_md', classifier_version='test',
                            classified_at='2026-01-01', skill_md_hash_val='original')
        before = conn.execute('SELECT * FROM skill_goals').fetchall()
        stats = module.refine_pass(conn, None, dry_run=False, now_iso='2026-02-01', classifier_version='test')
        if state == 'eligible':
            assert stats == dict(refine_eligible=1, refined=1)
            result = json.loads(conn.execute('SELECT goal_weights FROM skill_goals').fetchone()[0])
            assert result == pytest.approx(dict(speed=0.42, precision=0.44, completeness=0.14))
        else:
            assert stats == dict(refine_eligible=0, refined=0)
            assert conn.execute('SELECT * FROM skill_goals').fetchall() == before
            assert module.observed_mix(conn, 'test:skill') == ({}, 0)


def modification(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO modifications(id,group_id,ts,mod_type,target_file,confidence) "
                     "VALUES (1,'skill/test:skill',datetime('now'),'skill_tune','unused',1)")


def sample(db, i, value=0):
    return shell(db, '_interspect_record_skill_canary_sample 1 test:skill "$1" error 1 "$2"',
                 f'inv-{i}', str(value))


@pytest.mark.parametrize('state', ['quarantined', 'migrated', 'wrong_skill', 'wrong_session', 'orphan', 'eligible'])
def test_canary_sample_writer_requires_eligible_invocation(db, state):
    skill_signals(db, state)
    modification(db)
    for _ in range(2):
        result = sample(db, 0)
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM skill_canary_samples').fetchone()[0] == int(state == 'eligible')


def hold_sample_evidence(db, state, first=0):
    with sqlite3.connect(db) as conn:
        params = (first + 1,)
        if state == 'migrated':
            conn.execute('DROP TRIGGER interspect_gate_update')
            conn.execute('UPDATE evidence SET low_confidence=1 WHERE id>=?', params)
            conn.execute('DELETE FROM interspect_gate_schema')
            conn.commit()
            gate_module().migrate(conn)
        elif state == 'quarantined':
            conn.execute('UPDATE evidence SET quarantine_until=4102444800 WHERE id>=?', params)
        elif state == 'wrong_session':
            conn.execute("UPDATE evidence SET session_id='other-session' WHERE id>=?", params)
        elif state == 'wrong_skill':
            conn.execute("UPDATE evidence SET source='other:skill' WHERE id>=?", params)
        elif state == 'orphan':
            conn.execute('DELETE FROM evidence WHERE id>=?', params)


def canary_reader(db, reader):
    if reader == 'status':
        doc = (ROOT / 'commands/interspect-status.md').read_text()
        query = re.search(r'SKILL_CANARIES=\$\(sqlite3[^\n]*"\$DB" "(.*?)"\)', doc, re.S).group(1)
        return shell(db, 'sqlite3 -separator "|" "$_INTERSPECT_DB" "' + query + '"')
    # Only overlay removal is stubbed: assertions exercise real sample SQL,
    # thresholds and modification updates without touching home directories.
    return shell(db, '_interspect_disable_skill_overlay() { return 0; }; '
                 '_interspect_evaluate_skill_canary 1')


@pytest.mark.parametrize('state', ['quarantined', 'migrated', 'wrong_skill', 'wrong_session', 'orphan'])
@pytest.mark.parametrize('reader', ['evaluation', 'status'])
def test_retained_canary_samples_recheck_eligibility(db, state, reader):
    skill_signals(db, 'eligible', n=3)
    modification(db)
    for i in range(3):
        assert sample(db, i).returncode == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM skill_canary_samples').fetchone()[0] == 3
    hold_sample_evidence(db, state)
    result = canary_reader(db, reader)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('pending' if reader == 'evaluation' else '')
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM modifications').fetchone()[0] == 'applied'
        assert conn.execute('SELECT COUNT(*) FROM skill_canary_samples').fetchone()[0] == 3


@pytest.mark.parametrize('state', ['quarantined', 'migrated'])
@pytest.mark.parametrize('reader', ['evaluation', 'status'])
def test_canary_averages_only_eligible_samples_after_minimum(db, state, reader):
    skill_signals(db, 'eligible', n=6)
    modification(db)
    for i in range(6):
        assert sample(db, i, int(i < 3)).returncode == 0
    hold_sample_evidence(db, state, first=3)
    result = canary_reader(db, reader)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('ok' if reader == 'evaluation' else 'test:skill|3|0.0')
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM modifications').fetchone()[0] == 'applied'


def test_eligible_canary_regression_still_reverts(db):
    skill_signals(db, 'eligible', n=3)
    modification(db)
    for i in range(3):
        assert sample(db, i).returncode == 0
    result = canary_reader(db, 'evaluation')
    assert result.returncode == 0 and result.stdout.strip() == 'reverted'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM modifications').fetchone()[0] == 'reverted'
