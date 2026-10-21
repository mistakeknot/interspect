import json
import sqlite3

import pytest

from test_gate_normalization import gate_module


def database(path=':memory:', legacy=True):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY, session_id TEXT, source TEXT, '
               'override_reason TEXT, context TEXT, event TEXT, ts TEXT, seq INTEGER, '
               'source_version TEXT, project TEXT, source_event_id TEXT, source_table TEXT, '
               'raw_override_reason TEXT)')
    if legacy:
        for column in ['low_confidence INTEGER DEFAULT 0', 'corroborated_at INTEGER DEFAULT 0',
                       'quarantine_until INTEGER DEFAULT 0', 'finding_key TEXT',
                       "source_kind TEXT DEFAULT 'agent'"]:
            db.execute('ALTER TABLE evidence ADD COLUMN ' + column)
    db.commit()
    return db


def legacy_row(db, session='sess-a', context=None, key='run:p0-1', lifted=False):
    if context is None:
        context = json.dumps(dict(low_confidence=True, review_id='run', finding_id='p0-1'))
    db.execute('INSERT INTO evidence (session_id,source,override_reason,context,low_confidence,'
               'finding_key,quarantine_until,corroborated_at) VALUES (?,?,?,?,1,?,?,?)',
               (session, 'fd-a', 'agent_wrong', context, key, 0 if lifted else 4102444800,
                123 if lifted else 0))
    db.commit()


@pytest.mark.parametrize('legacy', [False, True])
def test_fresh_and_pre_gate(legacy):
    db = database(legacy=legacy)
    db.execute("INSERT INTO evidence (session_id, context) VALUES ('ordinary','{}')")
    db.commit()
    gate_module().migrate(db)
    row = dict(db.execute('SELECT * FROM evidence').fetchone())
    assert row['context'] == '{}' and row['low_confidence'] == 0
    assert row['gate_version'] == 0
    assert db.execute('SELECT version FROM interspect_gate_schema').fetchone()[0] == 2
    before = list(db.iterdump())
    gate_module().migrate(db)
    assert list(db.iterdump()) == before


@pytest.mark.parametrize('session,context,key,version', [
    ('sess-a', None, 'run:p0-1', 2), ('SESS-A', None, 'run:p0-1', 2),
    ('', None, 'run:p0-1', 0), (None, None, 'run:p0-1', 0),
    ('a\x00', None, 'run:p0-1', 0), ('sess-a', '{', 'run:p0-1', 0),
    ('sess-a', '{"finding_id":"p0-1","review_id":null}', 'run:p0-1', 0),
    ('sess-a', None, 'different:p0-1', 0), ('sess-a', None, None, 0),
    ('sess-a', '{"low_confidence":true,"finding_id":"p0-1"}', 'unscoped:sess-a:p0-1', 2),
])
def test_requarantine_and_audit(session, context, key, version):
    db = database()
    legacy_row(db, session, context, key, lifted=True)
    gate_module().migrate(db)
    row = dict(db.execute('SELECT * FROM evidence').fetchone())
    assert row['gate_version'] == version
    assert row['quarantine_until'] == 4102444800 and row['corroborated_at'] == 0
    assert row['session_id'] == session  # raw identity remains diagnostic
    assert row['corroborated_by'] is None
    old = json.loads(db.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0])
    assert old['corroborated_at'] == 123
    if version == 0:
        assert row['gate_key'] is None


def test_collision_keys_reconstructed_as_separate_tuples():
    db = database()
    for session, review, finding in [('a', 'a:b', 'c'), ('b', 'a', 'b:c')]:
        legacy_row(db, session, json.dumps(dict(low_confidence=True, review_id=review,
                                               finding_id=finding)), 'a:b:c')
    gate_module().migrate(db)
    assert db.execute('SELECT COUNT(DISTINCT gate_key) FROM evidence').fetchone()[0] == 2
    assert db.execute('SELECT SUM(corroborated_at) FROM evidence').fetchone()[0] == 0


def test_unsupported_schema_rolls_back():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE evidence (session_id TEXT)')
    before = list(db.iterdump())
    with pytest.raises(ValueError):
        gate_module().migrate(db)
    assert list(db.iterdump()) == before


@pytest.mark.parametrize('identity_schema', ['id TEXT PRIMARY KEY, source TEXT',
                                           'id INTEGER, source TEXT, PRIMARY KEY(id, source)'])
def test_id_must_be_the_single_integer_primary_key(identity_schema):
    db = sqlite3.connect(':memory:')
    # Table-level constraints must follow the ordinary columns.
    db.execute('CREATE TABLE evidence (session_id TEXT, override_reason TEXT, context TEXT, ' + identity_schema + ')')
    before = list(db.iterdump())
    with pytest.raises(ValueError):
        gate_module().migrate(db)
    assert list(db.iterdump()) == before


def test_interruption_rolls_back_schema_and_rows():
    db = database()
    legacy_row(db, lifted=True)
    before = list(db.iterdump())
    db.execute("CREATE TRIGGER interrupt BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'interrupt'); END")
    db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        gate_module().migrate(db)
    db.execute('DROP TRIGGER interrupt')
    assert list(db.iterdump()) == before


@pytest.mark.parametrize('mutation', [
    'low_confidence=0', 'quarantine_until=0', 'corroborated_at=123', 'gate_key=NULL',
    "gate_session='b'", 'gate_version=0', 'source_kind=NULL', 'low_confidence=NULL',
    "gate_scope='session'", "gate_scope_id='other'",
    "quarantine_until=0, corroborated_at=123",  # old-library UPDATE
])
def test_guards_reject_rewrites(mutation):
    db = database()
    legacy_row(db)
    gate_module().migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE evidence SET ' + mutation)


def test_fast_path_needs_no_write_lock(tmp_path):
    path = tmp_path / 'evidence.db'
    first = database(path)
    gate_module().migrate(first)
    first.execute('BEGIN IMMEDIATE')
    second = sqlite3.connect(path, timeout=0.01)
    gate_module().migrate(second)
    first.rollback()


def test_affected_overrides_are_listed_not_changed(tmp_path):
    db = database()
    legacy_row(db, lifted=True)
    overrides = tmp_path / 'routing-overrides.json'
    original = json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': [1]}]})
    overrides.write_text(original)
    gate_module().migrate(db, overrides)
    affected = db.execute('SELECT override_json FROM interspect_gate_override_review').fetchall()
    assert len(affected) == 1 and json.loads(affected[0][0])['agent'] == 'fd-a'
    assert overrides.read_text() == original


def test_unpinned_version_default_rejected_without_changes():
    db = database()
    db.execute('ALTER TABLE evidence ADD COLUMN gate_version INTEGER DEFAULT 2')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError):
        gate_module().migrate(db)
    assert list(db.iterdump()) == before


def test_converting_ordinary_row_cannot_arrive_lifted():
    db = database()
    db.execute("INSERT INTO evidence (session_id,source,override_reason,context) VALUES ('ordinary','fd-a','agent_wrong','{}')")
    db.commit()
    gate_module().migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("""UPDATE evidence SET low_confidence=1, gate_version=2,
            gate_session='ordinary',gate_source='fd-a',gate_reason='agent_wrong',
            gate_scope='review',gate_scope_id='run',gate_finding_id='p0-1',
            gate_key=json_array('v2','review','run','p0-1'), quarantine_until=0,
            corroborated_at=123,corroborated_by='other',corroborated_via='explicit'""")
