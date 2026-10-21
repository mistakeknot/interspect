"""The gate rejects mutations rather than repairing asserted identities."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

MODULE = Path(__file__).resolve().parents[2] / 'scripts/interspect_gate.py'


def gate_module():
    assert MODULE.exists(), 'typed gate module must exist'
    spec = importlib.util.spec_from_file_location('gate', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('kind,value,expected', [
    ('session_id', 'SESSION-1', 'session-1'), ('actor', 'A._-9', 'a._-9'),
    ('review_id', 'a:b@c+d._-9', 'a:b@c+d._-9'),
    ('finding_id', 'p0-1', 'p0-1'), ('source', 'INTERFLUX:REVIEW:FD-SAFETY', 'fd-safety'),
    ('source', 'interflux:fd-safety', 'fd-safety'), ('source', 'fd-a', 'fd-a'),
    ('override_reason', 'agent_wrong', 'agent_wrong'),
    ('session_id', 's' * 128, 's' * 128),
    ('review_id', 'r' * 256, 'r' * 256), ('source', 'fd-' + 'a' * 125, 'fd-' + 'a' * 125),
])
def test_canonical_roundtrip(kind, value, expected):
    gate = gate_module()
    canonical = gate.normalize_field(kind, value)
    assert canonical == expected
    assert gate.normalize_field(kind, canonical) == canonical
    with sqlite3.connect(':memory:') as db:
        assert db.execute('SELECT ?', (canonical,)).fetchone()[0].encode() == canonical.encode()


@pytest.mark.parametrize('kind', ['session_id', 'actor', 'review_id', 'finding_id', 'source', 'override_reason'])
@pytest.mark.parametrize('value', [None, False, 1, [], {}, '', 'unknown', 'NULL', ' a', 'a ',
    'a\n', '\n', 'a\x00', 'a\x7f', 'ſ', 'K', 'ß', 'İ', 'a\u200b', "a'b", 'a;b', 'a' * 257])
def test_invalid_never_becomes_identity(kind, value):
    with pytest.raises(ValueError):
        gate_module().normalize_field(kind, value)


@pytest.mark.parametrize('kind,value', [
    ('session_id', 'a:b'), ('actor', '@a'), ('review_id', 'Run'), ('finding_id', 'P0-1'),
    ('source', 'interflux:interflux:fd-a'), ('source', 'review:fd-a'), ('source', 'fd-1'),
    ('source', 'fd-a_b'), ('source', 'fd-' + 'a' * 126), ('session_id', 's' * 129),
    ('override_reason', 'AGENT_WRONG'), ('finding_key', 'anything'),
])
def test_field_boundaries(kind, value):
    with pytest.raises(ValueError):
        gate_module().normalize_field(kind, value)


@pytest.mark.parametrize('raw', ['{"low_confidence":true,"low_confidence":false}',
    '{"review_id":"a","review_id":"b"}', 'null', '[]', '{', '{"low_confidence":NaN}'])
def test_strict_json(raw):
    with pytest.raises(ValueError):
        gate_module().parse_object(raw)


def test_structured_key_separates_components_and_scope():
    gate = gate_module()
    with sqlite3.connect(':memory:') as db:
        keys = {gate.finding_key(db, scope, run, finding) for scope, run, finding in [
            ('review', 'a:b', 'c'), ('review', 'a', 'b:c'), ('session', 'a', 'b:c')]}
    assert len(keys) == 3
    assert all(json.loads(key)[0] == 'v2' for key in keys)
