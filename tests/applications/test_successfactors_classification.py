"""Synthetic NS2 job identities: classification never enables a filler."""
import hashlib
import sqlite3

import pytest

from jhb.applications import boards, queue
from jhb.applications.greenhouse_source import classify_ats
from tests.applications.test_greenhouse_source import page, resolve

BASE = 'https://career-hcm03.ns2cloud.com/sfcareer/jobreqcareer'
URL = BASE+'?jobId=12345&company=Example_Corp'
IDENTITY = ('successfactors', 'career-hcm03.ns2cloud.com', 'Example_Corp', '12345')


@pytest.mark.parametrize('url', [URL, BASE+'?company=Example_Corp&jobId=12345&locale=en_US',
    BASE+'?jobId=%31%32%33%34%35&company=Example_Corp&utm_source=synthetic',
    URL.replace('career-hcm03', 'CAREER-HCM03'), URL.replace('.com/', '.com:443/')])
def test_exact_observed_ns2_job_identity_and_canonicalization(url):
    assert boards.board_type(url) == classify_ats(url) == 'successfactors'
    assert boards.job_identity(url) == IDENTITY
    assert boards.canonical_url(url) == URL
    assert boards.job_identity(boards.canonical_url(url)) == IDENTITY
    assert boards.application_hash(url) == hashlib.sha256('|'.join(IDENTITY).encode()).hexdigest()


@pytest.mark.parametrize('url', [
    BASE, BASE+'?company=Example_Corp', BASE+'?jobId=12345',
    BASE+'?jobId=&company=Example_Corp', BASE+'?jobId=0&company=Example_Corp',
    BASE+'?jobId=-1&company=Example_Corp', BASE+'?jobId=+1&company=Example_Corp',
    BASE+'?jobId=001&company=Example_Corp', BASE+'?jobId=1.5&company=Example_Corp',
    BASE+'?jobId=1e3&company=Example_Corp', BASE+'?jobId=12345&company=',
    BASE+'?jobId=12345&company=Example%2FCorp', BASE+'?jobId=12345&company=Example%00Corp',
    URL+'&jobId=67890', URL+'&company=Other', URL+'&jobId=', URL+'&JOBID=67890',
    URL+'&company=', URL+'&COMPANY=Other', URL+'&locale=en_US&locale=en_GB',
    URL+'&broken', URL+'&locale=%GG', URL+'#application',
    URL.replace('https:', 'http:'), URL.replace('https://', 'https://user:pass@'),
    URL.replace('.com/', '.com.attacker.test/'), URL.replace('.com/', '.com:8443/'),
    URL.replace('career-hcm03.', 'hcm03.'), URL.replace('career-hcm03.', 'career-hcm04.'),
    URL.replace('jobreqcareer', 'jobreqcareer/'), URL.replace('jobreqcareer', 'jobs'),
    URL.replace('sfcareer', 'sfcareer/../sfcareer'), URL.replace('sfcareer', '%73%66career'),
])
def test_listing_malformed_and_unreviewed_host_routes_never_have_job_identity(url):
    assert boards.job_identity(url) is None
    assert boards.canonical_url(url) is None
    assert boards.application_hash(url) is None


def test_exact_tenant_and_requisition_are_independent_identity_components():
    assert boards.application_hash(URL) != boards.application_hash(URL.replace('12345', '12346'))
    assert boards.application_hash(URL) != boards.application_hash(URL.replace('Example_Corp', 'Other'))
    assert boards.application_hash(URL) != boards.application_hash(URL.replace('Example_Corp', 'example_corp'))
    assert boards.board_type('https://career-hcm03.ns2cloud.com.attacker.test/sfcareer/jobreqcareer') == 'unknown'


def test_isolated_source_classifies_exact_ns2_route_without_adapter_authority():
    result, transport = resolve({URL: page(URL)}, URL)
    assert result['state'] == 'not_greenhouse'
    assert result['board_type'] == result['ats'] == 'successfactors'
    assert result['identity'] == list(IDENTITY) and result['application_url'] == URL
    assert [name for name, _ in transport.calls] == ['browser_navigate', 'browser_snapshot', 'browser_evaluate']
    assert boards.adapter('successfactors') == {'board': 'successfactors', 'prep_enabled': False,
                                               'submit_enabled': False, 'skill': None}
    conn = sqlite3.connect(':memory:');conn.row_factory = sqlite3.Row
    try:
        assert queue.enqueue(conn, [{'url': URL, 'dedupe_hash': 'synthetic-source',
                                     'company': 'Synthetic', 'title': 'Engineer'}]) == 0
        assert conn.execute('SELECT COUNT(*) FROM applications').fetchone()[0] == 0
    finally:conn.close()


@pytest.mark.parametrize('changed', [URL.replace('12345', '12346'), URL.replace('Example_Corp', 'Other')])
def test_changed_official_tenant_or_requisition_hands_off_without_route(changed):
    result, _ = resolve({URL: page(changed)}, URL)
    assert result['state'] == 'ambiguous' and result['application_url'] is None
    assert 'verified_job_description' not in result


def test_family_branding_without_job_identity_remains_a_handoff():
    result, _ = resolve({BASE: page(BASE)}, BASE)
    assert result['board_type'] == 'successfactors' and result['state'] == 'ambiguous'
    assert result['application_url'] is None and 'verified_job_description' not in result
