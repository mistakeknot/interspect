#!/usr/bin/env python3
"""Typed low-confidence gate. Identities are asserted, never authenticated.

Requires Python 3.12+ and SQLite JSON1. Direct database writers are trusted;
persistent guards protect invariants, not provenance or actor authentication.
"""
import json
import re
import sqlite3
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

SENTINEL = 4102444800
REASONS = ('agent_wrong', 'deprioritized', 'already_fixed', 'stale_finding',
           'not_applicable', 'severity_miscalibrated')
GUARD_REVISION = 2


class GateDependencyError(RuntimeError):
    pass


def eligible_sql(alias=''):
    """Read-only counterpart of _interspect_eligible_sql, also for skill readers.

    Keep the predicates byte-identical (parity tested); shell callers do not
    need a Python subprocess for every query. Only internal aliases are allowed.
    """
    if alias and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias) is None:
        raise ValueError('invalid SQL alias')
    p = alias + '.' if alias else ''
    return (f"(COALESCE({p}quarantine_until,0) <= CAST(strftime('%s','now') AS INTEGER) "
            f"AND ({p}low_confidence=0 OR ({p}low_confidence=1 AND {p}gate_version=2 "
            f"AND {p}corroborated_at>0 AND {p}quarantine_until=0)))")


def require_json1(db):
    try:
        db.execute("SELECT json_array('v2'), json_valid('{}'), json_type('{}'), "
                   "json_extract('{}','$.low_confidence') FROM json_tree('{}')").fetchone()
    except sqlite3.Error as error:
        raise GateDependencyError('SQLite JSON1 is required; gate writes stopped') from error


def normalize_field(kind, value):
    """Reject invalid input without trimming, transliteration or truncation."""
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError('identity must be a nonempty ASCII string')
    if value.lower() in ('unknown', 'null'):
        raise ValueError('reserved identity')
    if kind in ('session_id', 'actor'):
        pattern = r'[a-z0-9][a-z0-9._-]{0,127}'
        value = value.lower()
    elif kind in ('review_id', 'finding_id'):
        pattern = r'[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}'
    elif kind == 'source':
        value = value.lower()
        for prefix in ('interflux:review:', 'interflux:'):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        if len(value) > 128:
            raise ValueError('source too long')
        pattern = r'fd-[a-z][a-z0-9-]*'
    elif kind == 'override_reason':
        if value not in REASONS:
            raise ValueError('invalid reason')
        return value
    else:
        raise ValueError('unknown identity field')
    if re.fullmatch(pattern, value, flags=re.ASCII) is None:
        raise ValueError('invalid identity alphabet or length')
    return value


def parse_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('non-JSON number')

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


def context_requires_gate(raw):
    """Conservative classification for legacy rows; malformed is never false."""
    try:
        context = parse_object(raw)
        low = context.get('low_confidence', False)
        return type(low) is not bool or low
    except (ValueError, TypeError):
        return True


def finding_key(db, scope, scope_id, finding_id):
    return db.execute("SELECT json_array('v2', ?, ?, ?)",
                      (scope, scope_id, finding_id)).fetchone()[0]


def canonical_fields(session, source, reason, context):
    if 'finding_key' in context:
        raise ValueError('caller supplied key')
    session = normalize_field('session_id', session)
    scope = 'review' if 'review_id' in context else 'session'
    return dict(gate_version=2, gate_session=session,
                gate_source=normalize_field('source', source),
                gate_reason=normalize_field('override_reason', reason),
                gate_scope=scope,
                gate_scope_id=normalize_field('review_id', context['review_id'])
                if scope == 'review' else session,
                gate_finding_id=normalize_field('finding_id', context.get('finding_id')))


GATE_COLUMNS = {
    'low_confidence': 'INTEGER DEFAULT 0', 'corroborated_at': 'INTEGER DEFAULT 0',
    'quarantine_until': 'INTEGER DEFAULT 0', 'finding_key': 'TEXT',
    'source_kind': "TEXT NOT NULL DEFAULT 'agent'",
    'gate_version': 'INTEGER NOT NULL DEFAULT 0',
    **{name: 'TEXT' for name in ('gate_session', 'gate_source', 'gate_reason',
       'gate_scope', 'gate_scope_id', 'gate_finding_id', 'gate_key',
       'corroborated_by', 'corroborated_via')},
}
IDENTITY_COLUMNS = ('session_id', 'source', 'override_reason', 'source_kind',
                    'gate_version', 'gate_session', 'gate_source', 'gate_reason',
                    'gate_scope', 'gate_scope_id', 'gate_finding_id', 'gate_key')


def sql_identity(column, kind):
    """Connection-independent, NUL-safe canonical whitelist for persistent guards."""
    x = 'NEW.' + column
    alphabet = 'a-z0-9._-'
    initial = 'a-z0-9'
    maximum = 128
    if kind in ('review_id', 'finding_id'):
        alphabet = 'A-Za-z0-9._:@+-'
        initial = 'A-Za-z0-9'
        maximum = 256
    common = (f"typeof({x})='text' AND length(CAST({x} AS BLOB))=length({x}) "
              f"AND instr({x},char(0))=0 AND length({x}) BETWEEN 1 AND {maximum} "
              f"AND lower({x}) NOT IN ('unknown','null')")
    if kind == 'source':
        return f"({common} AND {x} GLOB 'fd-[a-z]*' AND {x} NOT GLOB '*[^a-z0-9-]*')"
    if kind == 'override_reason':
        members = ','.join(repr(reason) for reason in REASONS)
        return f"({common} AND {x} IN ({members}))"
    return f"({common} AND {x} GLOB '[{initial}]*' AND {x} NOT GLOB '*[^{alphabet}]*')"


def install_guards(db):
    # CASE evaluates lazily, so malformed JSON reaches RAISE instead of a JSON
    # parser error. Duplicate object keys are rejected, including nested ones.
    context_requires_hold = """CASE
        WHEN json_valid(NEW.context) IS NOT 1 THEN 1
        WHEN json_type(NEW.context) <> 'object' THEN 1
        WHEN EXISTS (SELECT 1 FROM json_tree(NEW.context)
                     GROUP BY parent,key HAVING COUNT(*) > 1) THEN 1
        WHEN json_type(NEW.context,'$.low_confidence') IS NULL THEN 0
        WHEN json_type(NEW.context,'$.low_confidence') NOT IN ('true','false') THEN 1
        ELSE json_type(NEW.context,'$.low_confidence')='true' END"""
    canonical = ' AND '.join([
        sql_identity('gate_session', 'session_id'), sql_identity('gate_source', 'source'),
        sql_identity('gate_reason', 'override_reason'), sql_identity('gate_finding_id', 'finding_id'),
        "NEW.source_kind='agent'", "NEW.gate_scope IN ('review','session')",
        "((NEW.gate_scope='review' AND " + sql_identity('gate_scope_id', 'review_id') +
        ") OR (NEW.gate_scope='session' AND NEW.gate_scope_id=NEW.gate_session COLLATE BINARY))",
        "NEW.gate_key=json_array('v2',NEW.gate_scope,NEW.gate_scope_id,NEW.gate_finding_id) COLLATE BINARY",
    ])
    held = (f'NEW.quarantine_until={SENTINEL} AND NEW.corroborated_at=0 '
            'AND NEW.corroborated_by IS NULL AND NEW.corroborated_via IS NULL')
    witness = """EXISTS (SELECT 1 FROM evidence w
        WHERE w.gate_version=2 AND w.low_confidence=1 AND w.source_kind='agent'
          AND w.gate_scope='review' AND NEW.gate_scope='review'
          AND w.gate_source=NEW.gate_source COLLATE BINARY
          AND w.gate_reason=NEW.gate_reason COLLATE BINARY
          AND w.gate_key=NEW.gate_key COLLATE BINARY
          AND w.gate_session=NEW.corroborated_by COLLATE BINARY
          AND w.gate_session<>NEW.gate_session COLLATE BINARY)"""
    lifted = ("NEW.gate_version=2 AND NEW.quarantine_until=0 "
              "AND typeof(NEW.corroborated_at)='integer' AND NEW.corroborated_at>0 AND " +
              sql_identity('corroborated_by', 'actor') +
              " AND NEW.corroborated_by<>NEW.gate_session COLLATE BINARY "
              "AND (NEW.corroborated_via='explicit' OR (NEW.corroborated_via='automatic' AND " +
              witness + '))')
    empty_gate = ' AND '.join('NEW.' + col + ' IS NULL' for col in
        ('gate_session', 'gate_source', 'gate_reason', 'gate_scope', 'gate_scope_id',
         'gate_finding_id', 'gate_key'))
    valid = ("typeof(NEW.low_confidence)='integer' AND NEW.low_confidence IN (0,1) "
             "AND typeof(NEW.gate_version)='integer' AND NEW.gate_version IN (0,2) "
             f"AND ((NEW.gate_version=0 AND {empty_gate}) OR "
             f"(NEW.gate_version=2 AND NEW.low_confidence=1 AND {canonical})) "
             f"AND (NEW.low_confidence=0 OR ({held}) OR ({lifted}))")
    immutable = ' OR '.join(f'NEW.{col} IS NOT OLD.{col}' for col in IDENTITY_COLUMNS)
    for action in ('INSERT', 'UPDATE'):
        statements = ["SELECT CASE WHEN NEW.source_kind='agent' AND NEW.low_confidence IS NOT 1 "
                      f"AND ({context_requires_hold}) THEN RAISE(ABORT,'context requires gate') END;",
                      f"SELECT CASE WHEN ({valid}) IS NOT 1 THEN RAISE(ABORT,'invalid gate state') END;"]
        if action == 'INSERT':
            statements.append(f"SELECT CASE WHEN NEW.low_confidence=1 AND ({held}) IS NOT 1 "
                              "THEN RAISE(ABORT,'new gate must be held') END;")
        else:
            statements.append(f"SELECT CASE WHEN OLD.low_confidence IS NOT 1 AND NEW.low_confidence=1 AND ({held}) IS NOT 1 "
                              "THEN RAISE(ABORT,'new gate must be held') END;")
            statements.append("SELECT CASE WHEN OLD.low_confidence=1 AND "
                              f"(NEW.low_confidence IS NOT 1 OR {immutable}) "
                              "THEN RAISE(ABORT,'gate identity is immutable') END;")
            statements.append("SELECT CASE WHEN OLD.low_confidence=1 AND OLD.corroborated_at>0 AND "
                "(NEW.corroborated_at IS NOT OLD.corroborated_at OR NEW.quarantine_until IS NOT OLD.quarantine_until "
                "OR NEW.corroborated_by IS NOT OLD.corroborated_by OR NEW.corroborated_via IS NOT OLD.corroborated_via) "
                "THEN RAISE(ABORT,'lift is immutable') END;")
        db.execute(f'CREATE TRIGGER interspect_gate_{action.lower()} BEFORE {action} ON evidence BEGIN ' +
                   '\n'.join(statements) + ' END')


def migrated(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interspect_gate_schema'").fetchone():
        return False
    row = db.execute('SELECT version FROM interspect_gate_schema').fetchone()
    if row is not None and row[0] != 2:
        raise ValueError('unsupported gate schema version')
    columns = {r[1] for r in db.execute('PRAGMA table_info(interspect_gate_schema)')}
    if row is None or 'guard_revision' not in columns:
        return False
    revision = db.execute('SELECT guard_revision FROM interspect_gate_schema').fetchone()[0]
    if revision > GUARD_REVISION:
        raise ValueError('unsupported gate guard revision')
    return revision == GUARD_REVISION


def migrate(db, overrides_path=None):
    require_json1(db)
    # Read-only fast path: ordinary hook initialization must not take a write lock.
    if migrated(db):
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        if migrated(db):
            db.commit()
            return
        columns = {r[1]: r for r in db.execute('PRAGMA table_info(evidence)')}
        required = {'id', 'session_id', 'source', 'override_reason', 'context'}
        primary_key = [name for name, info in columns.items() if info[5]]
        if (not required <= columns.keys() or primary_key != ['id']
                or columns['id'][2].upper() != 'INTEGER'):
            raise ValueError('evidence requires its production id primary key and identity columns')
        if 'gate_version' in columns:
            info = columns['gate_version']
            if info[2].upper() != 'INTEGER' or info[3] != 1 or info[4] != '0':
                raise ValueError('gate version requires a pinned zero default')
        db.execute("SELECT json_array('v2')")  # JSON1 prerequisite, inside rollback boundary.
        for name, declaration in GATE_COLUMNS.items():
            if name not in columns:
                db.execute(f'ALTER TABLE evidence ADD COLUMN {name} {declaration}')
        db.execute('CREATE TABLE IF NOT EXISTS interspect_gate_audit (evidence_id INTEGER PRIMARY KEY, previous_row TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS interspect_gate_override_review (override_json TEXT NOT NULL)')
        # Refresh pre-fix v2 guards transactionally. Rollback restores old guards.
        for action in ('insert', 'update'):
            db.execute('DROP TRIGGER IF EXISTS interspect_gate_' + action)
        cursor = db.execute('SELECT * FROM evidence')
        names = [d[0] for d in cursor.description]
        candidates = [dict(zip(names, row)) for row in cursor.fetchall()]
        rows = [row for row in candidates if row['low_confidence'] == 1 or
                (row['source_kind'] == 'agent' and context_requires_gate(row['context']))]
        # Include every row exposed to prior readers, not just explicit lifts.
        # The old expiry-only readers also consumed uncorroborated/context-only
        # flags once quarantine elapsed. Repair must consult retained audits:
        # an earlier migration may already have held a row without reporting it.
        now = int(time.time())
        exposed_ids = {r[0] for r in db.execute(
            'SELECT id FROM evidence WHERE COALESCE(quarantine_until,0)<=? '
            'OR corroborated_at>0', (now,))}
        exposed_ids.update(r[0] for r in db.execute(
            "SELECT evidence_id FROM interspect_gate_audit WHERE "
            "COALESCE(json_extract(previous_row,'$.quarantine_until'),0)<=? "
            "OR json_extract(previous_row,'$.corroborated_at')>0", (now,)))
        affected_ids = exposed_ids.intersection(row['id'] for row in rows)
        for row in rows:
            db.execute('INSERT OR IGNORE INTO interspect_gate_audit VALUES (?,?)',
                       (row['id'], json.dumps(row, ensure_ascii=True)))
            fields = dict(gate_version=0, **{col: None for col in GATE_COLUMNS if col.startswith('gate_') and col != 'gate_version'})
            try:
                if row['source_kind'] != 'agent':
                    raise ValueError('invalid classification')
                if row['gate_version'] == 2:
                    # Existing v2 identities are separate from truncated diagnostics.
                    context = {'finding_id': row['gate_finding_id']}
                    if row['gate_scope'] == 'review':
                        context['review_id'] = row['gate_scope_id']
                    candidate = canonical_fields(row['gate_session'], row['gate_source'], row['gate_reason'], context)
                    if any(row[k] != v for k, v in candidate.items()):
                        raise ValueError('invalid previous canonical identity')
                    if row['gate_key'] != finding_key(db, candidate['gate_scope'], candidate['gate_scope_id'], candidate['gate_finding_id']):
                        raise ValueError('invalid previous canonical key')
                else:
                    context = parse_object(row['context'])
                    if context.get('low_confidence') is not True:
                        raise ValueError('invalid legacy classification')
                    candidate = canonical_fields(row['session_id'], row['source'], row['override_reason'], context)
                    # Never split a concatenated key or reconstruct a NULL base-era key.
                    legacy_scope = context['review_id'] if 'review_id' in context else 'unscoped:' + row['session_id']
                    if legacy_scope + ':' + context['finding_id'] != row['finding_key']:
                        raise ValueError('legacy key disagrees with context')
                candidate['gate_key'] = finding_key(db, candidate['gate_scope'], candidate['gate_scope_id'], candidate['gate_finding_id'])
                fields = candidate
            except (ValueError, TypeError, KeyError):
                pass  # An invalid legacy row remains diagnostic and permanently held.
            fields.update(low_confidence=1, quarantine_until=SENTINEL, corroborated_at=0,
                          corroborated_by=None, corroborated_via=None)
            assignments = ','.join(name + '=?' for name in fields)
            db.execute(f'UPDATE evidence SET {assignments} WHERE id=?', (*fields.values(), row['id']))
        if overrides_path is not None and Path(overrides_path).exists():
            overrides = parse_object(Path(overrides_path).read_text()).get('overrides', [])
            if not isinstance(overrides, list):
                raise ValueError('invalid routing overrides')
            for override in overrides:
                if not isinstance(override, dict) or not isinstance(override.get('evidence_ids', []), list):
                    raise ValueError('invalid override evidence IDs')
                # Writers accept JSON arrays without constraining member types.
                # Resolve scalar references using the actual INTEGER PK affinity:
                # 1, "1", 1.0, "01", "1e0", etc. refer to the same evidence.
                # json_each.atom is NULL for arrays/objects (not references).
                references = {r[0] for r in db.execute(
                    'SELECT id FROM evidence WHERE id IN (SELECT atom FROM json_each(?))',
                    (json.dumps(override.get('evidence_ids', [])),))}
                if affected_ids.intersection(references):
                    encoded = json.dumps(override, sort_keys=True)
                    db.execute('INSERT INTO interspect_gate_override_review SELECT ? '
                               'WHERE NOT EXISTS (SELECT 1 FROM interspect_gate_override_review WHERE override_json=?)',
                               (encoded, encoded))
        db.execute('CREATE INDEX IF NOT EXISTS idx_evidence_gate ON evidence '
                   '(gate_version,gate_source,gate_reason,gate_key,gate_session)')
        install_guards(db)
        db.execute('CREATE TABLE IF NOT EXISTS interspect_gate_schema (version INTEGER NOT NULL)')
        marker_columns = {r[1] for r in db.execute('PRAGMA table_info(interspect_gate_schema)')}
        if 'guard_revision' not in marker_columns:
            db.execute('ALTER TABLE interspect_gate_schema ADD COLUMN guard_revision INTEGER NOT NULL DEFAULT 0')
        db.execute('DELETE FROM interspect_gate_schema')
        db.execute('INSERT INTO interspect_gate_schema VALUES (2,?)', (GUARD_REVISION,))
        db.commit()
        if rows:
            print(f'interspect gate: {len(rows)} evidence rows held after migration; '
                  'see interspect_gate_audit', file=sys.stderr)
        if affected_ids:
            print('interspect gate: previously eligible evidence now held: '
                  + json.dumps(sorted(affected_ids)), file=sys.stderr)
    except BaseException:
        db.rollback()
        raise


# Both modes execute these identical bytes, with bound canonical parameters.
LIFT_SQL = """
WITH request AS (
 SELECT :source AS source, :reason AS reason,
        json_array('v2',:scope,:scope_id,:finding_id) AS gate_key
), witnesses AS (
 SELECT gate_session AS actor, gate_source AS source, gate_reason AS reason, gate_key
 FROM evidence WHERE :mode='automatic' AND gate_version=2 AND low_confidence=1
 AND source_kind='agent' AND gate_scope='review'
 AND gate_session IS NOT NULL AND gate_session<>'' AND gate_session NOT IN ('unknown','null')
 UNION ALL
 SELECT :actor, source, reason, gate_key FROM request
 WHERE :mode='explicit' AND :actor IS NOT NULL AND :actor<>'' AND :actor NOT IN ('unknown','null')
), eligible AS (
 SELECT t.id, MIN(w.actor COLLATE BINARY) AS actor
 FROM evidence t JOIN request r
 ON t.gate_source=r.source COLLATE BINARY AND t.gate_reason=r.reason COLLATE BINARY
 AND t.gate_key=r.gate_key COLLATE BINARY
 JOIN witnesses w ON w.source=t.gate_source COLLATE BINARY
 AND w.reason=t.gate_reason COLLATE BINARY AND w.gate_key=t.gate_key COLLATE BINARY
 WHERE t.gate_version=2 AND t.low_confidence=1 AND t.source_kind='agent'
 AND t.corroborated_at=0 AND t.quarantine_until=:sentinel
 AND t.gate_session IS NOT NULL AND t.gate_session<>'' AND t.gate_session NOT IN ('unknown','null')
 AND w.actor<>t.gate_session COLLATE BINARY AND (:mode='explicit' OR t.gate_scope='review')
 GROUP BY t.id
)
UPDATE evidence SET quarantine_until=0, corroborated_at=:now,
 corroborated_by=(SELECT actor FROM eligible WHERE id=evidence.id), corroborated_via=:mode
WHERE id IN (SELECT id FROM eligible)
"""


def lift(db, fields, mode, actor=None, now=None):
    if mode not in ('automatic', 'explicit') or fields['gate_scope'] not in ('review', 'session'):
        raise ValueError('invalid mode or scope')
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ValueError('invalid timestamp')
    db.execute(LIFT_SQL, dict(source=fields['gate_source'], reason=fields['gate_reason'],
        scope=fields['gate_scope'], scope_id=fields['gate_scope_id'],
        finding_id=fields['gate_finding_id'], mode=mode, actor=actor, now=now, sentinel=SENTINEL))
    return db.execute('SELECT changes()').fetchone()[0]


def insert(db, request):
    if 'finding_key' in request:
        raise ValueError('caller supplied key')
    kind = request.get('source_kind', 'agent')
    if kind not in ('agent', 'tool', 'pattern', 'skill'):
        raise ValueError('invalid source kind')
    context = parse_object(request.get('context', '{}'))
    low = context.get('low_confidence', False)
    if type(low) is not bool:
        raise ValueError('low_confidence must be a JSON boolean')
    if low and kind != 'agent':
        raise ValueError('low-confidence gate requires agent source kind')
    fields = dict(gate_version=0)
    if low:
        try:
            fields = canonical_fields(request.get('session_id'), request.get('source'),
                                      request.get('override_reason'), context)
        except ValueError:
            pass  # Known low-confidence diagnostic; no key and no witness identity.
    migrate(db, request.get('overrides_path'))
    db.execute('BEGIN IMMEDIATE')
    try:
        row = {key: request.get(key, '') for key in ('session_id', 'source', 'event',
            'override_reason', 'source_version', 'project')}
        for key, diagnostic in (('source', 'diagnostic_source'), ('event', 'diagnostic_event'),
                                ('override_reason', 'diagnostic_reason')):
            if diagnostic in request:
                row[key] = request[diagnostic]
        # Missing/invalid raw values remain diagnostics, never substitute an identity.
        for key, value in row.items():
            if not isinstance(value, str):
                row[key] = json.dumps(value)
        diagnostic = request.get('diagnostic_context', '{}')
        try:
            parse_object(diagnostic)
        except (ValueError, TypeError):
            # Sanitization may truncate JSON. Retain it as text in valid JSON;
            # classification comes only from the strictly parsed raw request.
            diagnostic = json.dumps(dict(diagnostic=diagnostic, low_confidence=low))
        row.update(context=diagnostic,
                   ts=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   source_kind=kind, low_confidence=int(low), corroborated_at=0,
                   quarantine_until=SENTINEL if low else request.get('quarantine_until', 0))
        row['seq'] = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM evidence WHERE session_id=?',
                                (row['session_id'],)).fetchone()[0]
        for key in ('source_event_id', 'source_table', 'raw_override_reason'):
            row[key] = request.get(key) or None
        row.update(fields)
        if fields['gate_version'] == 2:
            row['gate_key'] = finding_key(db, fields['gate_scope'], fields['gate_scope_id'], fields['gate_finding_id'])
        columns = ','.join(row)
        db.execute(f"INSERT INTO evidence ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        if fields['gate_version'] == 2:
            lift(db, fields, 'automatic')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def corroborate(db, request):
    if request.get('mode', 'explicit') != 'explicit' or 'finding_key' in request:
        raise ValueError('invalid explicit request')
    scope = request.get('scope', 'review')
    if scope not in ('review', 'session'):
        raise ValueError('invalid scope')
    fields = dict(gate_source=normalize_field('source', request.get('source')),
        gate_reason=normalize_field('override_reason', request.get('override_reason')),
        gate_scope=scope,
        gate_scope_id=normalize_field('review_id' if scope == 'review' else 'session_id', request.get('scope_id')),
        gate_finding_id=normalize_field('finding_id', request.get('finding_id')))
    actor = normalize_field('actor', request.get('actor'))
    now = request.get('now', int(time.time()))
    if type(now) is not int or now <= 0:
        raise ValueError('invalid timestamp')
    migrate(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        count = lift(db, fields, 'explicit', actor, now)
        db.commit()
        return count
    except BaseException:
        db.rollback()
        raise


def disagreement_requests(raw):
    """Parse before any shell/jq extraction can discard controls or JSON types."""
    event = parse_object(raw)
    agents = event.get('agents_json', '{}')
    agents = parse_object(agents) if isinstance(agents, str) else agents
    if not isinstance(agents, dict):
        raise ValueError('invalid agents')
    dismissal = event.get('dismissal_reason', '')
    reason = {'agent_wrong': 'agent_wrong', 'deprioritized': 'deprioritized',
              'already_fixed': 'stale_finding', 'not_applicable': 'not_applicable'}.get(dismissal, '')
    severity_conflict = dismissal == '' and event.get('resolution') == 'accepted' and event.get('impact') == 'severity_overridden'
    if severity_conflict:
        reason = 'severity_miscalibrated'
    if not event.get('finding_id') or not event.get('resolution') or not event.get('chosen_severity'):
        return []
    # Missing classification is uncertain, never evidence of high confidence.
    # Validate both locations before any OR can short-circuit validation.
    low = event_confidence(event) or event.get('low_confidence', True) or severity_conflict
    result = []
    for agent, severity in agents.items():
        if severity == event['chosen_severity']:
            continue
        context = {key: event.get(key, '') for key in ('finding_id', 'resolution', 'impact', 'dismissal_reason', 'chosen_severity')}
        context.update(agent_severity=severity, low_confidence=low)
        if 'review_run_id' in event:
            context['review_id'] = event['review_run_id']
        result.append(dict(session_id=event.get('session_id', ''), source=agent,
            event='disagreement_override', override_reason=reason, context=json.dumps(context),
            source_kind='agent', source_event_id=event.get('id', ''), source_table='review_events',
            raw_override_reason=dismissal))
    return result


def event_confidence(event):
    """Ordinary telemetry may omit the flag; a present flag must be boolean.

    Preserve either the top-level or nested context assertion. These producers
    have no severity-conflict inference; disagreement uses its stricter policy.
    """
    nested = event.get('context', {})
    if isinstance(nested, str):
        nested = parse_object(nested)
    if not isinstance(nested, dict):
        raise ValueError('invalid event context')
    flags = [obj.get('low_confidence', False) for obj in (event, nested)]
    if any(type(flag) is not bool for flag in flags):
        raise ValueError('invalid low-confidence classification')
    return any(flags)


def execution_defect_request(raw):
    event = parse_object(raw)
    low = event_confidence(event)
    agents = event.get('agents_json', {})
    agents = parse_object(agents) if isinstance(agents, str) else agents
    if not isinstance(agents, dict):
        raise ValueError('invalid agents')
    target = agents.get('target') or next(iter(sorted(agents)), '')
    if not target or not event.get('finding_id') or not event.get('resolution'):
        raise ValueError('missing execution defect fields')
    context = dict(finding_id=event['finding_id'], reporter=agents.get('reporter', ''),
                   target=target, severity=event.get('chosen_severity', ''),
                   resolution=event['resolution'], impact=event.get('impact', ''),
                   low_confidence=low)
    if 'review_run_id' in event:
        context['review_id'] = event['review_run_id']
    return dict(session_id=event.get('session_id', ''), source=target,
                event='execution_defect', override_reason='', context=json.dumps(context),
                source_kind='agent', source_event_id=event.get('id', ''),
                source_table='review_events')


def kernel_event_request(raw, session):
    event = parse_object(raw)
    low = event_confidence(event)
    if any(not isinstance(event.get(key), str) or not event[key] for key in ('source', 'type')):
        raise ValueError('missing kernel event fields')
    context = {key: event.get(key, '') for key in ('run_id', 'from_state', 'to_state', 'timestamp')}
    context.update(kernel_event_id=event.get('id', 0), low_confidence=low)
    return dict(session_id=session, source='kernel-' + event['source'], event=event['type'],
                override_reason='', context=json.dumps(context), source_kind='agent',
                source_event_id=event.get('id', ''), source_table='events')


def review_id_from_findings(path):
    data = parse_object(Path(path).read_text())
    base = Path(path).parent.name
    timestamp = data.get('synthesis_timestamp')
    # Validate components without changing spelling; IDs are case-sensitive.
    for component in (base, timestamp):
        if not isinstance(component, str) or not component.isascii() or re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}', component, flags=re.ASCII) is None:
            raise ValueError('invalid review run component')
    # The first @ is the component boundary. Without this restriction, (a@b,c)
    # and (a,b@c) both become a@b@c and can spuriously corroborate each other.
    if '@' in base:
        raise ValueError('ambiguous review basename')
    combined = base + '@' + timestamp
    if len(combined) > 256:
        raise ValueError('review run identity too long')
    return combined


def correction_context(request):
    low = request.get('low_confidence', False)
    if type(low) is not bool:
        raise ValueError('low_confidence must be a JSON boolean')
    context = dict(description=request.get('description', ''), low_confidence=low)
    finding = request.get('finding_id', '')
    if not finding:
        if low:
            raise ValueError('low-confidence correction requires a finding reference')
        return context
    normalize_field('finding_id', finding)
    path = request.get('findings_path', '')
    data = parse_object(Path(path).read_text())
    findings = data.get('findings')
    if not isinstance(findings, list):
        raise ValueError('missing findings')
    matches = [f for f in findings if isinstance(f, dict) and f.get('id') == finding]
    if len(matches) != 1 or type(matches[0].get('low_confidence')) is not bool:
        raise ValueError('unknown finding classification')
    context.update(finding_id=finding, low_confidence=low or matches[0]['low_confidence'])
    if 'synthesis_timestamp' in data:
        # Invalid/present run identity is an error; only absence selects session scope.
        context['review_id'] = review_id_from_findings(path)
        normalize_field('review_id', context['review_id'])
    return context


def main():
    try:
        operation = sys.argv[1]
        if operation == 'review-id':
            print(review_id_from_findings(sys.argv[2]))
            return 0
        if operation == 'disagreement':
            for request in disagreement_requests(sys.stdin.read()):
                print(json.dumps(request))
            return 0
        if operation == 'execution-defect':
            print(json.dumps(execution_defect_request(sys.stdin.read())))
            return 0
        if operation == 'kernel-event':
            print(json.dumps(kernel_event_request(sys.stdin.read(), sys.argv[2])))
            return 0
        if operation == 'correction-context':
            print(json.dumps(correction_context(parse_object(sys.stdin.read()))))
            return 0
        # Shell bridge encodes argv without jq extraction or string rewriting.
        if operation == 'shell-request':
            names = sys.argv[2].split(',')
            print(json.dumps(dict(zip(names, sys.argv[3:], strict=True))))
            return 0
        request = parse_object(sys.stdin.read())
        # Initialization owns file creation; an invalid request must not create a DB.
        database_uri = Path(sys.argv[2]).resolve().as_uri() + '?mode=rw'
        with sqlite3.connect(database_uri, uri=True, timeout=5) as db:
            if operation == 'migrate':
                migrate(db, request.get('overrides_path'))
                count = db.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0]
                if count:
                    print(f'interspect gate: REVIEW REQUIRED: {count} routing overrides; '
                          'see interspect_gate_override_review (report only, not reverted)', file=sys.stderr)
            elif operation == 'insert':
                if len(sys.argv) > 3:
                    request.update(diagnostic_context=sys.argv[3], project=sys.argv[4],
                                   source_version=sys.argv[5], quarantine_until=sys.argv[6])
                if 'quarantine_until' in request:
                    request['quarantine_until'] = int(request['quarantine_until'])
                insert(db, request)
            elif operation == 'corroborate':
                print(corroborate(db, request))
            else:
                raise ValueError('unknown gate operation')
        return 0
    except GateDependencyError:
        print('interspect gate: SQLite JSON1 is required; gate writes stopped', file=sys.stderr)
        return 2
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        # Never print attacker-controlled values, database paths or partial counts.
        print('interspect gate: request failed (' + type(error).__name__ + ')', file=sys.stderr)
        return 2 if isinstance(error, sqlite3.Error) else 1


if __name__ == '__main__':
    sys.exit(main())
