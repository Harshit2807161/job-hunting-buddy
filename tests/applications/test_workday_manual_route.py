"""Observed Workday manual-entry identity only; no wizard or browser actions."""
import sqlite3

import pytest

from jhb.applications import boards, queue

POSTING = 'https://caresource.wd1.myworkdayjobs.com/en-US/CareSource/job/Remote/Data-Scientist-I_R13840'
IDENTITY = ('workday', 'caresource', 'caresource', 'R13840')


@pytest.mark.parametrize('suffix', ['', '/apply', '/apply/', '/apply/applyManually', '/apply/applyManually/'])
def test_observed_manual_wizard_is_the_same_posting(suffix):
    url = POSTING+suffix
    assert boards.job_identity(url) == IDENTITY
    assert boards.canonical_url(url) == POSTING
    assert boards.application_hash(url) == boards.application_hash(POSTING)


def test_recruiting_host_uses_same_exact_manual_suffix_rule():
    url = 'https://jobs.myworkdaysite.com/recruiting/caresource/CareSource/job/Remote/Data-Scientist-I_R13840'
    assert boards.job_identity(url+'/apply/applyManually') == IDENTITY
    assert boards.canonical_url(url+'/apply/applyManually') == url


@pytest.mark.parametrize('suffix', ['/applyManually', '/apply/applymanually', '/Apply/applyManually',
                                  '/apply/applyWithResume', '/apply/applyManually/step2',
                                  '/apply/applyManually/../login', '/apply/applyManually%2Fstep2',
                                  '/apply/applyManually\\step2', '/apply/applyManually/extra'])
def test_unobserved_wizard_paths_are_not_guessed(suffix):
    assert boards.job_identity(POSTING+suffix) is None
    assert boards.canonical_url(POSTING+suffix) is None
    assert boards.application_hash(POSTING+suffix) is None


@pytest.mark.parametrize('url', [POSTING.replace('caresource.wd1', 'other.wd1'),
                               POSTING.replace('/CareSource/', '/OtherSite/'),
                               POSTING.replace('R13840', 'R13841')])
def test_other_tenant_site_or_requisition_stays_distinct(url):
    assert boards.job_identity(url+'/apply/applyManually') != IDENTITY
    assert boards.application_hash(url+'/apply/applyManually') != boards.application_hash(POSTING)


def test_wizard_identity_does_not_enable_automation():
    assert boards.preparation_supported('workday') is False
    assert boards.submission_supported('workday') is False
    conn = sqlite3.connect(':memory:')
    try:
        assert queue.enqueue(conn, [{'url': POSTING+'/apply/applyManually',
            'dedupe_hash': 'synthetic-source', 'title': 'Data Scientist I', 'company': 'Synthetic'}]) == 0
        assert conn.execute('SELECT COUNT(*) FROM applications').fetchone()[0] == 0
    finally:
        conn.close()
