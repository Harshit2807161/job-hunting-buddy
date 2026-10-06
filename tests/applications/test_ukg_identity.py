"""Synthetic UKG identity/import tests; no candidate session or site mutations."""
import hashlib
import sqlite3

import pytest

from jhb import config
from jhb.applications import approvals, boards, booklet, overnight, queue
from jhb.dashboard import DashboardStore

HOST = 'wbdus.rec.pro.ukg.net'
TENANT = 'SYNTHETIC_TENANT'
BOARD = '11111111-2222-4333-8444-555555555555'
OPPORTUNITY = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
DETAIL = f'https://{HOST}/{TENANT}/JobBoard/{BOARD}/OpportunityDetail?opportunityId={OPPORTUNITY}'
APPLY = DETAIL.replace('OpportunityDetail', 'OpportunityApply')
IDENTITY = ('ukg', HOST, TENANT, BOARD, OPPORTUNITY)


def test_detail_and_apply_share_exact_identity_canonical_url_and_hash():
    for url in (DETAIL, APPLY, APPLY.replace(BOARD, BOARD.upper()).replace(OPPORTUNITY, OPPORTUNITY.upper())):
        assert boards.board_type(url) == 'ukg'
        assert boards.job_identity(url) == IDENTITY
        assert boards.canonical_url(url) == DETAIL
        assert boards.application_hash(url) == hashlib.sha256('|'.join(IDENTITY).encode()).hexdigest()
    assert boards.adapter('ukg') == {'board': 'ukg', 'prep_enabled': False, 'submit_enabled': False, 'skill': None}


@pytest.mark.parametrize('url', [
    DETAIL.replace('https:', 'http:'), DETAIL.replace(HOST, HOST+'.attacker.test'),
    DETAIL.replace(HOST, 'different.rec.pro.ukg.net'), DETAIL.replace(HOST, 'user:pass@'+HOST),
    DETAIL.replace(HOST, HOST+':444'), DETAIL+'#other', DETAIL.split('?')[0],
    DETAIL.replace('opportunityId=', 'opportunityid='), DETAIL+'&opportunityId='+OPPORTUNITY,
    DETAIL+'&OpportunityId='+OPPORTUNITY, DETAIL+'&other=1', DETAIL+'&other',
    DETAIL.replace(OPPORTUNITY, ''), DETAIL.replace(OPPORTUNITY, 'not-a-uuid'),
    DETAIL.replace(OPPORTUNITY, OPPORTUNITY+'%'), DETAIL.replace('opportunityId=', 'opportunityId%ZZ='),
    DETAIL.replace('JobBoard', 'jobboard'), DETAIL.replace('OpportunityDetail', 'Opportunity'),
    DETAIL.replace(BOARD, 'board-index'), DETAIL.replace('/'+TENANT+'/', '//'),
    DETAIL.replace('/'+TENANT+'/', '/'+('a'*129)+'/'),
    DETAIL.replace('/'+TENANT+'/', '/%2e%2e/'), DETAIL.replace('/'+TENANT+'/', '/'+TENANT+'\\other/'),
])
def test_ambiguous_unsafe_and_unobserved_routes_do_not_create_job_identities(url):
    assert boards.job_identity(url) is None
    assert boards.canonical_url(url) is None
    assert boards.application_hash(url) is None


@pytest.mark.parametrize('url', [
    DETAIL.replace(TENANT, 'different_tenant'), DETAIL.replace(TENANT, TENANT.lower()),
    DETAIL.replace(BOARD, '21111111-2222-4333-8444-555555555555'),
    DETAIL.replace(OPPORTUNITY, 'baaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
])
def test_each_tenant_board_and_opportunity_is_a_different_job(url):
    assert boards.job_identity(url) is not None
    assert boards.job_identity(url) != IDENTITY
    assert boards.application_hash(url) != boards.application_hash(DETAIL)


def test_manual_packet_can_be_inspected_but_never_scheduled_or_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    db_path = tmp_path/'jobs.sqlite3'
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    job = {'url': APPLY, 'dedupe_hash': boards.application_hash(APPLY), 'board_type': 'ukg',
           'company': 'Synthetic Employer', 'title': 'Synthetic AI Engineer'}
    assert queue.enqueue(conn, [job]) == 0  # Identity recognition is not prep enablement.
    packet_path = tmp_path/'private/applications'/job['dedupe_hash']/'packet.json'
    packet = {'job': job, 'state': 'waiting_review', 'submitted': False, 'missing': [], 'verification': [],
              'filled': [{'ref': 'given', 'question': 'First Name', 'key': 'identity.first_name',
                          'value': 'Synthetic', 'source': 'Synthetic candidate fact'}],
              'review_inventory': {'complete': True, 'fields': [
                  {'ref': 'given', 'question': 'First Name', 'type': 'text', 'required': True,
                   'status': 'answered', 'answer_key': 'identity.first_name'}]}}
    booklet.write_private(packet_path, packet)
    book_path = tmp_path/'private/book.json'
    booklet.write_private(book_path, {'schema_version': 1, 'answers': {}, 'roles': {'ml': {}, 'sde': {}},
                                    'custom_answers': {}})
    try:
        assert overnight.register_manual_draft(conn, packet_path) == 1
        assert overnight.register_manual_draft(conn, packet_path) == 0
        # Isolate capability gating from separately tested immutable review validation.
        monkeypatch.setattr(approvals, '_snapshot', lambda *args: (packet, {'selected_role': 'ml'}, 'synthetic-revision'))
        detail = DashboardStore(tmp_path, db_path, book_path).details(job['dedupe_hash'])
        assert detail['fields'][0]['answer'] == 'Synthetic'
        assert detail['approval']['can_approve'] is False
        assert 'final submission adapter' in detail['approval']['reason']
        table = conn.execute("SELECT name FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone()
        assert table is None or conn.execute('SELECT COUNT(*) FROM authorized_submission_attempts').fetchone()[0] == 0
        for protected in ('running', 'submitted', 'submission_uncertain', 'skipped'):
            conn.execute('UPDATE applications SET state=? WHERE job_hash=?', (protected, job['dedupe_hash']))
            conn.commit()
            assert overnight.register_manual_draft(conn, packet_path) == 0
            assert conn.execute('SELECT state FROM applications').fetchone()[0] == protected
    finally:
        conn.close()
