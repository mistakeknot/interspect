"""Deterministic adversarial campaign; the oracle never imports the gate module.

Generator labels (valid tuple, invalid identity, same-session alias) are ground
truth. Assertion IDs retain the seed/field/minimal mutation for reproduction.
All shell calls cross the real Bash entry points in a subprocess.
"""
import itertools
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/interspect_gate.py'
LIB = ROOT / 'hooks/lib-interspect.sh'
SEEDS = (73021, 91483)
INVALID = [None, '', 'unknown', 'NULL', False, 7, [], {}, 'x ', ' x', 'x\n',
           '\u017f', '\u212a', '\u00df', '\u0130', 'é', 'e\u0301', 'x\u200b',
           "x';DROP TABLE evidence;--", 'x"', 'x/' , 'x' * 257]
INVALID += [chr(n) for n in range(32)] + ['\x7f']
FIELDS = ('session_id', 'review_id', 'finding_id', 'finding_key', 'override_reason', 'source', 'actor')
BASE_VALUE = dict(session_id='session-b', review_id='run', finding_id='p0-1',
                  finding_key='run:p0-1', override_reason='agent_wrong', source='fd-a', actor='session-b')


def shell(db, script, *args, input=None):
    return subprocess.run(['bash', '-c', 'source "$1"; _INTERSPECT_DB="$2"; shift 2; ' + script,
                           'gate-test', str(LIB), str(db), *args], input=input, text=True,
                          capture_output=True, env=dict(os.environ,
                          CLAUDE_PROJECT_DIR=str(db.parent), INTERSPECT_QUARANTINE_HOURS='0'), timeout=20)


def cli(db, operation, request):
    raw = request if isinstance(request, str) else json.dumps(request)
    return subprocess.run(['python3', str(MODULE), operation, str(db)], input=raw,
                          capture_output=True, text=True, timeout=10)


def row(session='session-a', **kwargs):
    value = dict(session_id=session, source='fd-a', event='override', override_reason='agent_wrong',
                 source_kind='agent', context=json.dumps(dict(low_confidence=True, review_id='run', finding_id='p0-1')))
    value.update(kwargs)
    return value


def explicit(**kwargs):
    value = dict(source='fd-a', override_reason='agent_wrong', scope='review', scope_id='run',
                 finding_id='p0-1', actor='session-b', mode='explicit')
    value.update(kwargs)
    return value


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp('gate-template')
    result = shell(root / 'unused', '_interspect_ensure_db')
    assert result.returncode == 0, result.stderr
    return root / '.clavain/interspect/interspect.db'


@pytest.fixture
def db(template, tmp_path):
    target = tmp_path / 'evidence.db'
    shutil.copyfile(template, target)
    return target


def rows(db):
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute('SELECT * FROM evidence ORDER BY id')]


def insert_shell(db, value):
    result = shell(db, '_interspect_insert_evidence "$@"', value['session_id'], value['source'],
                   value['event'], value['override_reason'], value['context'], 'interspect-correction',
                   '', '', '', value['source_kind'])
    return result


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('batch', range((len(INVALID) + 33 + 3) // 4))
def test_forged_fields_across_boundaries(db, field, batch):
    # Four independent databases bound CPU use while preserving every mutation
    # and every real entry point. This remains one pytest suite, not agent work.
    def exercise(item):
        index, bad = item
        case_db = db.parent / ('case-' + str(index) + '.db')
        shutil.copyfile(db, case_db)
        _exercise_forged_fields(case_db, field, bad)

    mutations = INVALID + [BASE_VALUE[field] + chr(n) for n in (*range(32), 127)]
    cases = list(enumerate(mutations[batch * 4:(batch + 1) * 4]))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(exercise, cases))


def _exercise_forged_fields(db, field, bad):
    assert cli(db, 'insert', row()).returncode == 0
    if field == 'actor':
        result = cli(db, 'corroborate', explicit(actor=bad))
        assert result.returncode != 0 and result.stdout == ''
        if isinstance(bad, str) and '\0' not in bad:
            result = shell(db, '_interspect_corroborate_evidence fd-a run p0-1 "$1" agent_wrong', bad)
            assert result.returncode != 0 and result.stdout == ''
    else:
        forged = row('session-b')
        if field in ('review_id', 'finding_id', 'finding_key'):
            context = json.loads(forged['context'])
            context[field] = bad
            forged['context'] = json.dumps(context)
        else:
            forged[field] = bad
        result = cli(db, 'insert', forged)
        assert not any(r['corroborated_at'] for r in rows(db))
        assert all(r['gate_version'] == 0 for r in rows(db)[1:]), (field, repr(bad))
        if all(isinstance(forged[k], str) and '\0' not in forged[k] for k in
               ('session_id', 'source', 'override_reason')):
            insert_shell(db, forged)
            assert all(r['gate_version'] == 0 for r in rows(db)[1:]), (field, repr(bad))
        if field != 'session_id':
            key = 'scope_id' if field == 'review_id' else field
            result = cli(db, 'corroborate', explicit(**{key: bad}))
            assert result.returncode != 0 and result.stdout == '', (field, repr(bad))
            if isinstance(bad, str) and '\0' not in bad and field != 'finding_key':
                confirmation = explicit(**{key: bad})
                result = shell(db, '_interspect_corroborate_evidence "$@"',
                               *[confirmation[k] for k in ('source', 'scope_id', 'finding_id', 'actor', 'override_reason')])
                assert result.returncode != 0 and result.stdout == '', (field, repr(bad))
        if field in ('session_id', 'review_id', 'finding_id', 'source'):
            # Disagreement has its own reason mapping and must retain typed identities.
            with sqlite3.connect(db) as connection:
                connection.execute('DELETE FROM evidence')
            original = row(override_reason='severity_miscalibrated')
            assert cli(db, 'insert', original).returncode == 0
            event = dict(session_id='session-b', finding_id='p0-1', review_run_id='run',
                         resolution='accepted', chosen_severity='P0', impact='severity_overridden',
                         agents_json={'fd-a': 'P1'})
            if field == 'source':
                if not isinstance(bad, str):
                    return  # JSON object member names cannot carry non-string types.
                event['agents_json'] = {bad: 'P1'}
            else:
                event['review_run_id' if field == 'review_id' else field] = bad
            result = shell(db, '_interspect_process_disagreement_event "$1"', json.dumps(event))
    assert not any(r['corroborated_at'] for r in rows(db)), (field, repr(bad))


@pytest.mark.parametrize('field', ['low_confidence', 'scope', 'mode', 'source_kind'])
@pytest.mark.parametrize('bad', [None, '', True, 1, [], {}, 'unknown', 'x\n', 'ſ'])
def test_classification_fields_fail_closed(db, field, bad):
    assert cli(db, 'insert', row()).returncode == 0
    if field in ('scope', 'mode'):
        result = cli(db, 'corroborate', explicit(**{field: bad}))
        assert result.returncode != 0 and result.stdout == ''
    else:
        forged = row('session-b')
        if field == 'low_confidence':
            if type(bad) is bool:
                return  # Boolean true is a valid classifier, tested in the tuple oracle.
            context = json.loads(forged['context']); context[field] = bad
            forged['context'] = json.dumps(context)
        else:
            forged[field] = bad
        assert cli(db, 'insert', forged).returncode != 0
    assert not any(r['corroborated_at'] for r in rows(db))


@pytest.mark.parametrize('raw', [
    '{"low_confidence":true,"low_confidence":false,"review_id":"run","finding_id":"p0-1"}',
    '{"low_confidence":true,"review_id":"bad","review_id":"run","finding_id":"p0-1"}',
    '{"low_confidence":true,"review_id":"run","finding_id":"p0-1\\u0000"}',
    '{"low_confidence":true,"review_id":"run","finding_id":"p0-1\\n"}',
    '{"low_confidence":"true"}', '{"low_confidence":null}', '{', 'null', '[]'])
def test_raw_json_before_bash_extraction(db, raw):
    assert insert_shell(db, row()).returncode == 0
    insert_shell(db, row('session-b', context=raw))
    assert not any(r['corroborated_at'] for r in rows(db))


@pytest.mark.parametrize('field', ['session_id', 'review_run_id', 'finding_id', 'agents_json', 'low_confidence'])
def test_disagreement_duplicate_keys_rejected_before_extraction(db, field):
    assert cli(db, 'insert', row(override_reason='severity_miscalibrated')).returncode == 0
    event = dict(session_id='session-b', finding_id='p0-1', review_run_id='run',
                 resolution='accepted', chosen_severity='P0', impact='severity_overridden',
                 agents_json={'fd-a':'P1'}, low_confidence=True)
    raw = '{' + json.dumps(field) + ':null,' + json.dumps(event)[1:]
    result = shell(db, '_interspect_process_disagreement_event "$1"', raw)
    assert result.returncode != 0
    assert len(rows(db)) == 1 and rows(db)[0]['corroborated_at'] == 0


@pytest.mark.parametrize('seed', SEEDS)
def test_labelled_tuple_oracle_permutations(db, seed):
    rng = random.Random(seed)
    # Labels supplied by construction; no production normalizer or key function used.
    entries = [
        (row('Session-A'), ('fd-a', 'agent_wrong', 'review', 'run', 'p0-1'), 'session-a'),
        (row('SESSION-A'), ('fd-a', 'agent_wrong', 'review', 'run', 'p0-1'), 'session-a'),
        (row('session-b', source='INTERFLUX:REVIEW:FD-A'), ('fd-a', 'agent_wrong', 'review', 'run', 'p0-1'), 'session-b'),
        (row('session-c', source='interflux:fd-a'), ('fd-a', 'agent_wrong', 'review', 'run', 'p0-1'), 'session-c'),
        (row('session-d', source='fd-b'), ('fd-b', 'agent_wrong', 'review', 'run', 'p0-1'), 'session-d'),
        (row('session-e', override_reason='deprioritized'), ('fd-a', 'deprioritized', 'review', 'run', 'p0-1'), 'session-e'),
        (row('session-f', context='{"low_confidence":true,"review_id":"a:b","finding_id":"c"}'),
         ('fd-a', 'agent_wrong', 'review', 'a:b', 'c'), 'session-f'),
        (row('session-g', context='{"low_confidence":true,"review_id":"a","finding_id":"b:c"}'),
         ('fd-a', 'agent_wrong', 'review', 'a', 'b:c'), 'session-g'),
        (row('session-h', context='{"low_confidence":true,"finding_id":"p0-1"}'),
         ('fd-a', 'agent_wrong', 'session', 'session-h', 'p0-1'), 'session-h'),
        (row('session-a\n'), None, None),
        (row('session-b', source='interflux:interflux:fd-a'), None, None),
    ]
    for permutation in range(4):
        with sqlite3.connect(db) as connection:
            connection.execute('DELETE FROM evidence')
        rng.shuffle(entries)
        inserted = []
        for value, label, session in entries:
            assert insert_shell(db, value).returncode == 0
            inserted.append((label, session))
            actual = rows(db)
            for observed, (key, original) in zip(actual, inserted):
                witnesses = [s for k, s in inserted if key is not None and k == key and s != original]
                eligible = bool(key and key[2] == 'review' and witnesses)
                assert bool(observed['corroborated_at']) == eligible, (seed, permutation, value)
                if eligible:
                    assert observed['corroborated_by'] in witnesses
                    assert observed['quarantine_until'] == 0


@pytest.mark.parametrize('first,second', list(itertools.combinations(['session_id', 'source', 'review_id', 'finding_id'], 2)))
def test_two_field_mutations_remain_held(db, first, second):
    assert insert_shell(db, row()).returncode == 0
    forged = row('session-b')
    context = json.loads(forged['context'])
    for field in (first, second):
        target = context if field in ('review_id', 'finding_id') else forged
        target[field] = 'x\n'
    forged['context'] = json.dumps(context)
    assert insert_shell(db, forged).returncode == 0
    assert not any(r['corroborated_at'] for r in rows(db))


@pytest.mark.parametrize('column,value', [
    ('gate_session', None), ('gate_session', 'a\x00\n'), ('gate_source', 'fd-a\x00'),
    ('gate_reason', 'agent_wrong\x00'), ('gate_scope_id', 'run\x00'), ('gate_finding_id', 'p0-1\x00'),
    ('gate_scope', None), ('gate_scope', 'session'), ('gate_version', None), ('gate_version', 3),
    ('low_confidence', None), ('low_confidence', 0), ('gate_key', 'forged'), ('quarantine_until', 0)])
def test_direct_sql_guard_mutations(db, column, value):
    assert cli(db, 'insert', row()).returncode == 0
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'UPDATE evidence SET {column}=?', (value,))
        # Also test inserts, without the immutable-update guard masking validation.
        original = rows(db)[0]
        original.pop('id')
        original[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO evidence (' + ','.join(original) + ') VALUES (' +
                               ','.join('?' for _ in original) + ')', tuple(original.values()))


@pytest.mark.parametrize('field', ['session_id', 'review_id', 'finding_id', 'finding_key', 'source', 'override_reason'])
def test_migration_mutations_never_lift(db, field):
    for bad in INVALID:
        if field in ('session_id', 'source', 'override_reason') and not isinstance(bad, str):
            # Production TEXT affinity cannot retain the pre-storage JSON type.
            # Typed NULL identities are exercised in the migration schema fixtures.
            continue
        with sqlite3.connect(db) as connection:
            # Each legacy database is constructed without new guards or marker.
            for trigger in ('interspect_gate_insert', 'interspect_gate_update'):
                connection.execute('DROP TRIGGER IF EXISTS ' + trigger)
            for table in ('interspect_gate_schema', 'interspect_gate_audit', 'interspect_gate_override_review'):
                connection.execute('DROP TABLE IF EXISTS ' + table)
            connection.execute('DROP INDEX IF EXISTS idx_evidence_gate')
            connection.execute('DELETE FROM evidence')
            context = dict(low_confidence=True, review_id='run', finding_id='p0-1')
            value = dict(ts='now', session_id='session-a', seq=1, source='fd-a', event='override',
                         override_reason='agent_wrong', project='test', low_confidence=1,
                         finding_key='run:p0-1', corroborated_at=123, quarantine_until=0)
            if field in ('review_id', 'finding_id'):
                context[field] = bad
            elif field == 'finding_key':
                value[field] = json.dumps(bad) if not isinstance(bad, str) else bad
            else:
                # SQLite raw columns are TEXT in production, so preserve textual diagnostics.
                value[field] = json.dumps(bad) if not isinstance(bad, str) else bad
            value['context'] = json.dumps(context)
            connection.execute('INSERT INTO evidence (' + ','.join(value) + ') VALUES (' +
                               ','.join('?' for _ in value) + ')', tuple(value.values()))
        result = cli(db, 'migrate', {})
        assert result.returncode == 0, result.stderr
        actual = rows(db)[0]
        assert actual['gate_version'] == 0 and actual['gate_key'] is None, (field, bad)
        assert actual['corroborated_at'] == 0 and actual['quarantine_until'] == 4102444800


def test_runtime_python_and_json1():
    result = subprocess.run(['python3', '-c', 'import sys,sqlite3; assert sys.version_info >= (3,12); '
                             'print(sqlite3.connect(":memory:").execute("SELECT json_array(1)").fetchone()[0])'],
                            text=True, capture_output=True)
    assert result.returncode == 0 and result.stdout.strip() == '[1]'


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_review_join_is_injective(tmp_path, seed):
    rng = random.Random(seed)
    seen = {}
    for _ in range(32):
        parts = ['x' + ''.join(rng.choices('ab09:@+.-', k=6)) for _ in range(3)]
        # Both separators are allowed in raw review/finding identities. A
        # producer-derived review must still identify its components uniquely.
        for delimiter in (':', '@'):
            a, b, c = parts
            for base, timestamp in [(a + delimiter + b, c), (a, b + delimiter + c)]:
                path = tmp_path / base / 'findings.json'
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(dict(synthesis_timestamp=timestamp)))
                result = subprocess.run(['python3', str(MODULE), 'review-id', str(path)],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode:
                    assert result.stdout == ''
                    continue
                identity = result.stdout.strip()
                assert seen.setdefault(identity, (base, timestamp)) == (base, timestamp), (seed, identity)
    assert seen, 'The join fuzz must exercise accepted identities, not only rejection.'


@pytest.mark.parametrize('delimiter', [':', '@'])
@pytest.mark.parametrize('seed', SEEDS)
def test_structured_finding_and_scope_joins_do_not_collide(db, delimiter, seed):
    rng = random.Random(seed)
    for i in range(12):
        a, b, c = ['x' + ''.join(rng.choices('ab09:@', k=5)) for _ in range(3)]
        contexts = [dict(review_id=a + delimiter + b, finding_id=c),
                    dict(review_id=a, finding_id=b + delimiter + c),
                    dict(finding_id=a + delimiter + b),
                    dict(review_id='unscoped:scope-a', finding_id=a + delimiter + b)]
        with sqlite3.connect(db) as conn:
            conn.execute('DELETE FROM evidence')
        for n, context in enumerate(contexts):
            context['low_confidence'] = True
            result = cli(db, 'insert', row('scope-a' if n == 2 else f'session-{n}',
                                         context=json.dumps(context)))
            assert result.returncode == 0, result.stderr
        evidence = rows(db)
        assert len({r['gate_key'] for r in evidence}) == 4, (seed, i)
        assert all(r['gate_version'] == 2 and r['corroborated_at'] == 0 for r in evidence)
        # Legacy colon joins reconstruct from separate components, never split.
        with sqlite3.connect(db) as conn:
            conn.execute('DROP TRIGGER interspect_gate_insert')
            conn.execute('DROP TRIGGER interspect_gate_update')
            conn.execute('DROP TABLE interspect_gate_schema')
            for observed, context in zip(evidence, contexts):
                scope = context.get('review_id', 'unscoped:' + observed['session_id'])
                conn.execute('UPDATE evidence SET gate_version=0, context=?, finding_key=? WHERE id=?',
                             (json.dumps(context), scope + ':' + context['finding_id'], observed['id']))
        assert cli(db, 'migrate', {}).returncode == 0
        evidence = rows(db)
        usable_keys = []
        for observed, context in zip(evidence, contexts):
            # Legacy review IDs lack generator provenance: multiple @ signs
            # could be the old ambiguous basename/timestamp join. Repair must
            # discard these keys, even when this fixture used explicit IDs.
            ambiguous = context.get('review_id', '').count('@') > 1
            assert observed['corroborated_at'] == 0
            assert observed['quarantine_until'] == 4102444800
            if ambiguous:
                assert observed['gate_version'] == 0 and observed['gate_key'] is None
            else:
                assert observed['gate_version'] == 2 and observed['gate_key'] is not None
                usable_keys.append(observed['gate_key'])
        assert len(set(usable_keys)) == len(usable_keys)


@pytest.mark.parametrize('field', ['session_id', 'actor', 'source', 'override_reason', 'finding_key'])
@pytest.mark.parametrize('delimiter', [':', '@'])
def test_delimiters_cannot_forge_other_canonical_fields(db, field, delimiter):
    _exercise_forged_fields(db, field, BASE_VALUE[field] + delimiter + 'other')
