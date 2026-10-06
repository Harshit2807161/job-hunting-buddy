"""Exact audited ambiguities persist without inventing a submitted application."""
import json
from pathlib import Path
import sqlite3

import pytest

from jhb import config
from jhb.applications import historical, queue, source_queue
from test_tracking import setup, ASHBY, CANONICAL
from test_historical import row


@pytest.fixture
def audited(setup):
    conn, job, _, sheets, _ = setup
    job = {**job, 'dedupe_hash': 'a' * 64, 'title': 'Software Engineer - Early Career'}
    sheets.rows[8] = row(job, url='', title='Software Engineer, Early Career 2027', date='14th sep')
    historical.import_sheet(conn, executor=sheets)
    entry = conn.execute('SELECT entry_key FROM sheet_application_history WHERE row_number=8').fetchone()[0]
    assert historical.match(conn, job) is None  # This is an audited near-match, not an inferred duplicate.
    return conn, job, sheets, entry


def record(audited, **kwargs):
    conn, job, _, entry = audited
    return historical.record_hold(conn, job, entry_key=entry,
        reason='Audited same early-career family; historical cohort is absent from current description.', **kwargs)


def test_hold_survives_ats_alias_rediscovery_and_refresh_without_receipt(audited):
    conn, job, sheets, entry = audited
    held = record(audited)
    assert record(audited) == held
    changed = {**job, 'url': CANONICAL + '?utm_source=another', 'dedupe_hash': 'b' * 64,
               'title': 'Reformatted source title'}
    assert historical.match(conn, changed)['hold_id'] == held['hold_id']
    sheets.rows[9] = row(job, url='', company='Other employer', title='Other role')
    historical.import_sheet(conn, executor=sheets, refresh_seconds=0)
    result = historical.match(conn, changed)
    assert result['match_kind'] == 'audited_history_hold' and result['disposition'] == 'hold'
    assert result['applied_date_raw'] == '14th sep' and result['row_number'] == 8
    assert result['submission_confirmed'] is False and 'confirmed_at' not in result
    assert conn.execute('SELECT COUNT(*) FROM confirmed_submissions').fetchone()[0] == sheets.appends == 0
    assert conn.execute('SELECT COUNT(*) FROM audited_history_holds').fetchone()[0] == 1


@pytest.mark.parametrize('url', [ASHBY.replace('555555555555', '555555555556'),
                                 ASHBY.replace('/example/', '/different/')])
def test_audited_hold_never_spills_to_distinct_job_or_employer(audited, url):
    conn, job, _, _ = audited
    record(audited)
    assert historical.match(conn, {**job, 'url': url}) is None


def test_explicit_resolution_is_durable_and_does_not_create_submission(audited):
    conn, job, sheets, entry = audited
    held = record(audited)
    with pytest.raises(ValueError): historical.release_hold(conn, held['hold_id'], reason='')
    assert historical.match(conn, job)['hold_id'] == held['hold_id']
    historical.release_hold(conn, held['hold_id'], reason='Candidate confirmed distinct cohort after review.')
    historical.import_sheet(conn, executor=sheets, refresh_seconds=0)
    assert historical.match(conn, job) is None
    with pytest.raises(ValueError, match='resolved'):
        record(audited)
    saved = conn.execute('SELECT state,resolution_reason FROM audited_history_holds').fetchone()
    assert saved[0] == 'released' and saved[1].startswith('Candidate confirmed')
    assert conn.execute('SELECT COUNT(*) FROM confirmed_submissions').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['unknown_entry', 'bad_hash', 'home_url', 'different_company', 'snapshot', 'row'])
def test_record_rejects_invalid_or_unbound_evidence(audited, damage):
    conn, job, _, entry = audited
    if damage == 'unknown_entry': entry = 'f' * 64
    elif damage == 'bad_hash': job = {**job, 'dedupe_hash': 'not-exact'}
    elif damage == 'home_url': job = {**job, 'url': 'https://example.test/'}
    elif damage == 'different_company': job = {**job, 'company': 'Unrelated Employer'}
    elif damage == 'snapshot':
        path = Path(conn.execute('SELECT snapshot_path FROM sheet_application_history').fetchone()[0]); path.write_text('{}')
    else:
        conn.execute("UPDATE sheet_application_history SET row_json='[]'"); conn.commit()
    with pytest.raises(ValueError):
        historical.record_hold(conn, job, entry_key=entry, reason='Audited ambiguity')
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='audited_history_holds'").fetchone()


def test_explicit_company_alias_is_scoped_to_one_job_and_row(audited):
    conn, job, _, entry = audited
    alias = {**job, 'company': 'Example Holdings'}
    held = historical.record_hold(conn, alias, entry_key=entry, reason='Audited role ambiguity',
        company_alias_reason='Reviewer verified that this exact employer is listed under its holding-company name.')
    assert historical.match(conn, alias)['hold_id'] == held['hold_id']
    assert historical.match(conn, {**alias, 'url': ASHBY.replace('555555555555', '555555555556')}) is None


def test_different_explicit_ats_id_in_imported_row_cannot_be_overridden_by_audit(audited):
    conn, job, sheets, _ = audited
    sheets.rows[10] = row(job, url=ASHBY.replace('555555555555', '555555555556'))
    historical.import_sheet(conn, executor=sheets, refresh_seconds=0)
    entry = conn.execute('SELECT entry_key FROM sheet_application_history WHERE row_number=10').fetchone()[0]
    with pytest.raises(ValueError, match='Distinct explicit ATS'):
        historical.record_hold(conn, job, entry_key=entry, reason='Do not override known different identity')


@pytest.mark.parametrize('damage', ['snapshot', 'missing_import', 'hold_binding'])
def test_changed_evidence_holds_for_reconciliation_instead_of_releasing(audited, damage):
    conn, job, _, _ = audited
    record(audited)
    if damage == 'snapshot':
        path = Path(conn.execute('SELECT snapshot_path FROM sheet_application_history').fetchone()[0]); path.unlink()
    elif damage == 'missing_import': conn.execute('DELETE FROM sheet_application_history')
    else:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM audited_history_holds').fetchone()[0])
        evidence['job_url'] = ASHBY.replace('555555555555', '555555555556')
        conn.execute('UPDATE audited_history_holds SET evidence_json=?', (json.dumps(evidence),))
    conn.commit()
    result = historical.match(conn, job)
    assert result['state'] == 'history_integrity_handoff' and result['disposition'] == 'hold'
    assert result['submission_confirmed'] is False


def test_hold_reaches_source_preparation_and_read_only_direct_worker_guard(audited):
    conn, job, _, _ = audited
    record(audited)
    source_queue.enqueue(conn, [job]); queue.enqueue(conn, [job])
    assert source_queue.claim(conn) is None and queue.claim(conn) is None
    for table in ('application_sources', 'applications'):
        state, attempts = conn.execute(f'SELECT state,attempts FROM {table}').fetchone()
        assert state == 'history_hold' and attempts == 0
    path = config.ROOT / 'data' / 'jobs.sqlite3'; path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as disk: conn.backup(disk)
    before = path.read_bytes()
    assert historical.cached_match(job)['match_kind'] == 'audited_history_hold'
    assert path.read_bytes() == before


@pytest.mark.parametrize('source', ['https://www.linkedin.com/jobs/view/123456/', 'https://careers.example.test/jobs/123',
                                   'http://www.indeed.com/job/synthetic-engineer-1234567890abcdef'])
def test_source_only_hold_is_exact_and_can_follow_its_resolved_job(audited, source):
    conn, job, _, entry = audited
    source_job = {**job, 'url': source}
    held = historical.record_hold(conn, source_job, entry_key=entry, reason='Audited source opening matches a legacy role.')
    assert historical.match(conn, source_job)['hold_id'] == held['hold_id']
    assert historical.match(conn, {**source_job, 'url': source.replace('123', '456')}) is None
    resolved = {**job, 'source_url': source, 'source_job_hash': source_job['dedupe_hash'], 'dedupe_hash': 'b' * 64}
    assert historical.match(conn, resolved)['hold_id'] == held['hold_id']
    if 'linkedin' not in source:
        assert historical.match(conn, {**source_job, 'dedupe_hash': 'c' * 64}) is None


def test_changed_source_hash_cannot_silently_release_its_original_hold(audited):
    conn, job, _, entry = audited
    job = {**job, 'url': 'https://careers.example.test/jobs/123'}
    historical.record_hold(conn, job, entry_key=entry, reason='Audited exact source job')
    evidence = json.loads(conn.execute('SELECT evidence_json FROM audited_history_holds').fetchone()[0])
    evidence['source_job_hash'] = 'c' * 64
    conn.execute('UPDATE audited_history_holds SET evidence_json=?', (json.dumps(evidence),)); conn.commit()
    assert historical.match(conn, job)['state'] == 'history_integrity_handoff'


def test_http_audit_key_preserves_scheme_without_enabling_browser_navigation(audited):
    from jhb.applications import boards
    conn, job, _, entry = audited
    url = 'http://www.indeed.com/job/synthetic-engineer-1234567890abcdef'
    job = {**job, 'url': url}
    held = historical.record_hold(conn, job, entry_key=entry, reason='Audited original HTTP job source')
    assert held['scope_key'] == 'source:' + url
    assert historical.match(conn, job)['hold_id'] == held['hold_id']
    assert historical.match(conn, {**job, 'url': url.replace('http:', 'https:')}) is None
    assert historical._url_key(url) is None and boards._parts(url) is None


@pytest.mark.parametrize('url', [
    'http://user:password@example.test/job/123', 'http://example.test:443/job/123',
    'http://example.test:8080/job/123', 'http://example.test:invalid/job/123',
    'http://example.test/job/../123', 'http://example.test/job/%2e%2e/123',
    'http://example.test/job/123#other', 'http://example.test/job/123\n',
    'http://example.test/', 'file:///job/123',
])
def test_invalid_http_source_cannot_acquire_an_audited_hold(audited, url):
    conn, job, _, entry = audited
    with pytest.raises(ValueError, match='exact job URL'):
        historical.record_hold(conn, {**job, 'url': url}, entry_key=entry, reason='Rejected source syntax')
