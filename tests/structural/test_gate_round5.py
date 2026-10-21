"""isp9 regressions exercised through the production readers and migration."""
import json
import sqlite3
import importlib.util
import sys
from datetime import datetime, timezone

import pytest

from test_gate_fuzz import ROOT, cli, db, row, rows, shell, template, explicit
from test_gate_normalization import gate_module


def canary(db, agent='fd-a', baseline_window=None):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "INSERT INTO canary (file,commit_sha,group_id,applied_at,window_uses,"
            "window_expires_at,baseline_override_rate,baseline_fp_rate,"
            "baseline_finding_density,baseline_window,status) "
            "VALUES ('test','test',?,'2026-01-03',1,'2026-01-17',1,0.5,1,?,'active')",
            (agent, baseline_window)).lastrowid


@pytest.mark.parametrize('agent', ['fd-a', ''])
def test_default_quarantine_records_normal_sample_without_held_contribution(db, agent):
    canary(db, agent)
    # The shared shell fixture normally disables quarantine. Explicitly unset it
    # before the real producer runs: this is the deployed 48-hour default.
    result = shell(db, 'set -e; unset INTERSPECT_QUARANTINE_HOURS; '
                   '_interspect_insert_evidence session-a fd-a agent_dispatch "" "{}" interspect-evidence; '
                   '_interspect_insert_evidence session-a fd-a override agent_wrong "$1" interspect-correction; '
                   '_interspect_record_canary_sample session-a', row()['context'])
    assert result.returncode == 0, result.stderr
    evidence = rows(db)
    assert evidence[0]['quarantine_until'] > 0
    assert evidence[1]['low_confidence'] == 1 and evidence[1]['corroborated_at'] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT override_rate,fp_rate,finding_density FROM canary_samples').fetchall() == [(0, 0, 1)]
        assert conn.execute('SELECT uses_so_far FROM canary').fetchone()[0] == 1


@pytest.mark.parametrize('overlap', ['sample', 'baseline_evidence', 'baseline_session', 'global'])
def test_migration_invalidates_held_derived_canary_before_real_evaluation(db, overlap):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    assert rows(db)[0]['corroborated_at'] > 0
    # A mixed session must invalidate its aggregate, even with clean evidence.
    assert cli(db, 'insert', row(context='{}')).returncode == 0
    cid = canary(db, '' if overlap == 'global' else 'fd-a',
                 '2026-01-01..2026-01-03' if overlap.startswith('baseline') else None)
    other = canary(db, 'fd-other', '2026-01-01..2026-01-03')
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE evidence SET ts=?", ('2026-01-02' if overlap == 'baseline_evidence' else '2026-02-01',))
        conn.execute("INSERT INTO sessions(session_id,start_ts,project) VALUES ('SESSION-A',?, 'test')",
                     ('2026-01-02' if overlap == 'baseline_session' else '2026-02-01',))
        for target in (cid, other):
            conn.execute('INSERT INTO canary_samples(canary_id,session_id,ts,override_rate,fp_rate,finding_density) '
                         "VALUES (?,?,'2026-01-04',9,1,1)",
                         (target, 'SESSION-A' if overlap in ('sample', 'global') else 'unrelated'))
            conn.execute('UPDATE canary SET uses_so_far=1 WHERE id=?', (target,))
        # Upgrade a deployed revision-3 DB, without forcing repair explicitly.
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=3')
    result = cli(db, 'migrate', {})
    assert result.returncode == 0, result.stderr
    evaluated = shell(db, '_interspect_evaluate_canary "$1"', str(cid))
    assert evaluated.returncode == 0, evaluated.stderr
    assert json.loads(evaluated.stdout)['status'] == 'needs_review'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT uses_so_far,baseline_override_rate,baseline_fp_rate,baseline_finding_density '
                            'FROM canary WHERE id=?', (cid,)).fetchone() == (0, None, None, None)
        assert conn.execute('SELECT COUNT(*) FROM canary_samples WHERE canary_id=?', (cid,)).fetchone()[0] == 0
        reports = [json.loads(r[0]) for r in conn.execute('SELECT override_json FROM interspect_gate_override_review')]
        assert any(r.get('canary_id') == cid and r.get('evidence_ids') == [1] for r in reports)
        assert conn.execute('SELECT status,uses_so_far FROM canary WHERE id=?', (other,)).fetchone() == ('active', 1)
        # Even a retained/reintroduced sample under an invalid canary must not
        # surface in status or evaluation.
        conn.execute('INSERT INTO canary_samples(canary_id,session_id,ts,override_rate,fp_rate,finding_density) '
                     "VALUES (?,'retained','2026-01-04',9,1,1)", (cid,))
    status = shell(db, '_interspect_get_canary_summary')
    summary = next(c for c in json.loads(status.stdout) if c['id'] == cid)
    assert summary['sample_count'] == 0 and float(summary['avg_override_rate']) == 0
    assert 'canaries require review' in result.stderr
    assert cli(db, 'migrate', {}).returncode == 0
    evaluated = shell(db, '_interspect_evaluate_canary "$1"', str(cid))
    assert json.loads(evaluated.stdout)['status'] == 'needs_review'


def test_path_aware_reconciliation_after_implicit_migration(db, tmp_path):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM interspect_gate_schema')
    # The bridge can migrate before it has a routing-overrides path.
    assert cli(db, 'corroborate', explicit(scope_id='another-review')).returncode == 0
    path = tmp_path / 'overrides.json'
    original = json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': ['1']}]})
    path.write_text(original)
    for _ in range(2):
        result = cli(db, 'migrate', {'overrides_path': str(path)})
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == 1
    assert path.read_text() == original


def test_later_exposure_is_reported_when_first_audit_was_held(db, tmp_path):
    assert cli(db, 'insert', row()).returncode == 0
    # First repair audits a held row. That original audit must stay unchanged.
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM interspect_gate_schema')
    assert cli(db, 'migrate', {}).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    # Another repair now removes a real exposure, before a policy path arrives.
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM interspect_gate_schema')
    assert cli(db, 'migrate', {}).returncode == 0
    path = tmp_path / 'overrides.json'
    path.write_text(json.dumps({'overrides': [{'agent': 'fd-a', 'evidence_ids': [1]}]}))
    result = cli(db, 'migrate', {'overrides_path': str(path)})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM interspect_gate_override_review').fetchone()[0] == 1
        audit = json.loads(conn.execute('SELECT previous_row FROM interspect_gate_audit').fetchone()[0])
        assert audit['corroborated_at'] == 0


def test_disjoint_canary_windows_preserve_unaffected_aggregates(db):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    cid = canary(db, baseline_window='2026-01-01..2026-01-03')
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE evidence SET ts='2025-01-01'")
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=3')
    assert cli(db, 'migrate', {}).returncode == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status,baseline_override_rate FROM canary WHERE id=?', (cid,)).fetchone() == ('active', 1)


def test_canary_invalidation_failure_rolls_back_evidence_and_aggregates(db):
    assert cli(db, 'insert', row()).returncode == 0
    assert cli(db, 'corroborate', explicit()).returncode == 0
    cid = canary(db)
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO canary_samples(canary_id,session_id,ts,override_rate) '
                     "VALUES (?,'session-a','2026-01-04',9)", (cid,))
        conn.execute('UPDATE interspect_gate_schema SET guard_revision=3')
        conn.execute("CREATE TRIGGER stop_invalidation BEFORE UPDATE ON canary BEGIN SELECT RAISE(ABORT,'stop'); END")
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(sqlite3.IntegrityError):
            gate_module().migrate(conn)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize('metric,expected', [('evidence_n', 2), ('agreement_rate', 1), ('output_diversity', 1)])
def test_consensus_audit_excludes_held_and_time_quarantined_rows(db, metric, expected):
    for session in ('normal-a', 'normal-b'):
        assert cli(db, 'insert', row(session, event='agent_dispatch', override_reason='', context='{}')).returncode == 0
    assert cli(db, 'insert', row('normal-a', source='fd-b')).returncode == 0
    assert cli(db, 'insert', row('normal-c', source='fd-c', context='{}', quarantine_until=4102444800)).returncode == 0
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('round5_audit', ROOT / 'scripts/calibrate-audit.py')
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    point = audit.compute_consensus_trend_point(db, datetime.now(timezone.utc), 90)
    assert point[metric] == expected


def aliases(db, source='fd-a', diagnostic_source=None):
    for session in ('abc', 'ABC', 'aBC', 'abC', 'Abc', 'AbC', 'ABc', 'aBc', 'b', 'c'):
        context = json.dumps(dict(low_confidence=True, review_id='run', finding_id='p0-1',
                                  status='NEEDS_ATTENTION', findings_count=1))
        request = row(session, source=source, event='verdict_outcome', context=context,
                      diagnostic_context=context)
        if diagnostic_source:
            request['diagnostic_source'] = diagnostic_source
        assert cli(db, 'insert', request).returncode == 0


@pytest.mark.parametrize('source,minimum,allowed', [('fd-a', 4, False), ('interflux:review:FD-A', 3, True)])
def test_autonomy_uses_canonical_session_and_source(db, source, minimum, allowed):
    aliases(db, source)
    result = shell(db, '_interspect_is_autonomous() { return 0; }; '
                   '_interspect_system_breaker_check() { return 1; }; '
                   '_interspect_rate_limit_exceeded() { return 1; }; '
                   '_interspect_circuit_breaker_tripped() { return 1; }; '
                   '_interspect_load_confidence() { :; }; '
                   '_INTERSPECT_CANARY_MIN_BASELINE="$1"; '
                   '_interspect_should_auto_apply fd-a routing', str(minimum))
    assert (result.returncode == 0) == allowed, result.stderr


@pytest.mark.parametrize('diagnostic_source', [None, 'fd-diagnostic'])
def test_scoring_uses_canonical_session_and_source(db, diagnostic_source):
    aliases(db, diagnostic_source=diagnostic_source)
    result = shell(db, '_interspect_compute_agent_scores')
    assert result.returncode == 0, result.stderr
    scores = json.loads(result.stdout)
    assert len(scores) == 1 and scores[0]['agent'] == 'fd-a'
    assert scores[0]['evidence_sessions'] == 3


def test_canary_uses_canonical_session_for_metrics_and_dedup(db):
    aliases(db)
    canary(db)
    for sid in ('abc', 'ABC'):
        result = shell(db, '_interspect_record_canary_sample "$1"', sid)
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT session_id,finding_density FROM canary_samples').fetchall() == [('abc', 8)]
        assert conn.execute('SELECT uses_so_far FROM canary').fetchone()[0] == 1


@pytest.mark.parametrize('target', ["fd-a' OR 1=1 --", 'fd-%', '../fd-a', 'fd-a'])
def test_tune_agent_validation_executes_shipped_snippet(db, target):
    assert cli(db, 'insert', row()).returncode == 0  # held only
    content = (ROOT / 'commands/interspect-tune.md').read_text()
    snippet = content.split('### Agent mode (KIND=agent)', 1)[1].split('```bash', 1)[1].split('```', 1)[0]
    result = shell(db, 'DB="$_INTERSPECT_DB"; TARGET="$1"; ' + snippet + '\nprintf "%s" "$CORRECTION_COUNT"', target)
    if target == 'fd-a':
        assert result.returncode == 0 and result.stdout == '0'
    else:
        assert result.returncode != 0, result.stdout
