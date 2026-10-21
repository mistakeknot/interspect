from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from test_gate_normalization import gate_module
from test_gate_migration import database


def request(session='session-a', **changes):
    row = dict(session_id=session, source='fd-a', override_reason='agent_wrong', event='override',
               source_kind='agent', context=json.dumps(dict(low_confidence=True,
               review_id='run', finding_id='p0-1')), diagnostic_context='{}')
    row.update(changes)
    return row


def confirmation(**changes):
    row = dict(source='fd-a', override_reason='agent_wrong', scope='review', scope_id='run',
               finding_id='p0-1', actor='session-b', mode='explicit', now=123)
    row.update(changes)
    return row


def ready(path=':memory:'):
    db = database(path)
    gate_module().migrate(db)
    return db


def test_insert_and_lift_atomic_failure():
    db = ready()
    gate = gate_module()
    gate.insert(db, request())
    db.execute("CREATE TRIGGER fail BEFORE INSERT ON evidence WHEN NEW.session_id='session-b' "
               "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        gate.insert(db, request('session-b'))
    assert db.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1


def test_valid_pair_lifts_but_invalid_third_stays_held():
    db = ready()
    gate = gate_module()
    for session in ['session-a', '', 'SESSION-A', 'session-b']:
        gate.insert(db, request(session))
    rows = db.execute('SELECT gate_version, corroborated_at, corroborated_by FROM evidence ORDER BY id').fetchall()
    assert [bool(row[1]) for row in rows] == [True, False, True, True]
    assert rows[1][0] == 0
    assert all(row[2] for row in rows if row[1])


def test_explicit_self_and_scoped_confirmation():
    db = ready()
    gate = gate_module()
    gate.insert(db, request())
    assert gate.corroborate(db, confirmation(actor='SESSION-A')) == 0
    for change in [dict(source='fd-b'), dict(override_reason='deprioritized'), dict(scope_id='other')]:
        assert gate.corroborate(db, confirmation(**change)) == 0
    assert gate.corroborate(db, confirmation()) == 1
    assert gate.corroborate(db, confirmation()) == 0


def test_session_scope_requires_explicit_mode():
    db = ready()
    gate = gate_module()
    for session in ['session-a', 'session-b']:
        gate.insert(db, request(session, context='{"low_confidence":true,"finding_id":"p0-1"}'))
    assert db.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0
    assert gate.corroborate(db, confirmation(scope='session', scope_id='session-a')) == 1


def test_concurrent_writers(tmp_path):
    path = tmp_path / 'gate.db'
    ready(path).close()

    def writer(session):
        with sqlite3.connect(path, timeout=5) as db:
            gate_module().insert(db, request(session))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(writer, ['session-a', 'session-b']))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence WHERE corroborated_at>0').fetchone()[0] == 2


def test_lock_timeout_is_failure(tmp_path):
    path = tmp_path / 'gate.db'
    first = ready(path)
    first.execute('BEGIN IMMEDIATE')
    with sqlite3.connect(path, timeout=0.01) as second:
        with pytest.raises(sqlite3.OperationalError):
            gate_module().insert(second, request())
    first.rollback()
    assert first.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 0


@pytest.mark.parametrize('changes', [dict(mode='bogus'), dict(scope=None), dict(now=0),
    dict(now=True), dict(actor='a\n'), dict(scope_id='Run'), dict(finding_key='forged')])
def test_invalid_explicit_no_mutation(changes):
    db = ready()
    gate = gate_module()
    gate.insert(db, request())
    with pytest.raises(ValueError):
        gate.corroborate(db, confirmation(**changes))
    assert db.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0


def test_correction_lookup_failure_never_assumes_false(tmp_path):
    gate = gate_module()
    path = tmp_path / 'findings.json'
    for contents in ['{', '{"findings":[]}',
                     '{"findings":[{"id":"p0-1","low_confidence":null}]}',
                     '{"findings":[{"id":"p0-1","low_confidence":true,"low_confidence":false}]}']:
        path.write_text(contents)
        with pytest.raises(ValueError):
            gate.correction_context(dict(finding_id='p0-1', findings_path=str(path)))
    path.write_text('{"findings":[{"id":"p0-1","low_confidence":true}]}')
    context = gate.correction_context(dict(finding_id='p0-1', findings_path=str(path)))
    assert context['low_confidence'] is True and 'review_id' not in context


def test_caller_key_is_refused_even_outside_context():
    db = ready()
    with pytest.raises(ValueError):
        gate_module().insert(db, request(finding_key='forged'))
    assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 0
