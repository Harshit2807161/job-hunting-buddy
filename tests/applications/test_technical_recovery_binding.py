"""Only exact private pre-review technical evidence can requeue preparation."""
import json

import pytest

from jhb import config
from jhb.applications import booklet, pipeline, queue
from tests.applications.test_pipeline import setup, job

URL = 'https://job-boards.greenhouse.io/example/jobs/987'


def legacy_failure(conn):
    queue.enqueue(conn, [job('synthetic-legacy', URL)])
    item = queue.claim(conn)
    path = config.ROOT / 'private' / 'applications' / item['job_hash'] / 'packet.json'
    packet = {'job': item['job'], 'state': 'failed', 'reason': 'Preparation failed: FileNotFoundError',
              'filled': [], 'events': [], 'submitted': False}
    booklet.write_private(path, packet)
    queue.finish(conn, item['job_hash'], 'failed', path.with_name('review.html'))
    return item, path, packet


@pytest.mark.parametrize('damage', ['no_job', 'wrong_hash', 'wrong_url', 'wrong_row_url', 'malformed_row',
                                  'invalid_json', 'public_mode', 'symlink_file', 'symlink_parent'])
def test_invalid_or_cross_job_legacy_packets_cannot_trigger_recovery(setup, damage):
    conn, _ = setup
    item, path, packet = legacy_failure(conn)
    if damage == 'no_job':
        packet.pop('job'); booklet.write_private(path, packet)
    elif damage == 'wrong_hash':
        packet['job'] = {**packet['job'], 'dedupe_hash': 'f'*64}; booklet.write_private(path, packet)
    elif damage == 'wrong_url':
        packet['job'] = {**packet['job'], 'url': URL.replace('/987', '/988')}; booklet.write_private(path, packet)
    elif damage == 'wrong_row_url':
        conn.execute('UPDATE applications SET job_json=?', (json.dumps({**item['job'], 'url': URL.replace('/987', '/988')}),))
        conn.commit()
    elif damage == 'malformed_row':
        conn.execute("UPDATE applications SET job_json='{' "); conn.commit()
    elif damage == 'invalid_json':
        path.write_text('{')
    elif damage == 'public_mode':
        path.chmod(0o644)
    elif damage == 'symlink_file':
        target = path.with_name('saved.json'); path.rename(target); path.symlink_to(target)
    else:
        target = path.parent.with_name('saved-directory'); path.parent.rename(target); path.parent.symlink_to(target, target_is_directory=True)
    assert pipeline.recover_technical_failures(conn) == 0
    assert conn.execute('SELECT state,attempts FROM applications').fetchone()[:] == ('failed', 1)


@pytest.mark.parametrize('protection', ['attempt', 'approval', 'orphan_marker', 'clicked', 'unknown_click',
                                      'submission_uncertain', 'exhausted'])
def test_prior_review_or_submission_evidence_never_reopens_preparation(setup, protection):
    conn, _ = setup
    item, path, packet = legacy_failure(conn)
    if protection in {'attempt', 'approval'}:
        table = 'authorized_submission_attempts' if protection == 'attempt' else 'application_approvals'
        conn.execute(f'CREATE TABLE {table}(job_hash TEXT,state TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES(?,?)', (item['job_hash'], 'waiting_review' if protection == 'attempt' else 'revoked'))
        conn.commit()
    elif protection == 'orphan_marker':
        booklet.write_private(config.ROOT / 'private' / 'authorized-submissions' / item['job_hash'] / 'attempt.json',
                              {'runtime_click_started': True})
    elif protection in {'clicked', 'unknown_click'}:
        packet['runtime_click_started'] = True if protection == 'clicked' else None
        booklet.write_private(path, packet)
    elif protection == 'submission_uncertain':
        queue.finish(conn, item['job_hash'], 'submission_uncertain', path.with_name('review.html'))
    else:
        conn.execute('UPDATE applications SET attempts=3'); conn.commit()
    before = tuple(conn.execute('SELECT state,attempts FROM applications').fetchone())
    assert pipeline.recover_technical_failures(conn) == 0
    assert tuple(conn.execute('SELECT state,attempts FROM applications').fetchone()) == before


@pytest.mark.parametrize('marker', [True, None, 'true', 1, 0])
def test_inline_retry_rejects_clicked_and_unknown_terminal_markers(marker):
    assert not pipeline._recoverable({'state': 'failed', 'retryable': True, 'error_kind': 'browser_transport',
                                     'runtime_click_started': marker})


def test_explicit_false_marker_keeps_valid_legacy_retry_and_original_budget(setup):
    conn, _ = setup
    _, path, packet = legacy_failure(conn)
    packet['runtime_click_started'] = False
    booklet.write_private(path, packet)
    assert pipeline.recover_technical_failures(conn) == 1
    assert conn.execute('SELECT state,attempts FROM applications').fetchone()[:] == ('retry', 1)
    assert pipeline.recover_technical_failures(conn) == 0
    assert queue.claim(conn)['attempts'] == 2
