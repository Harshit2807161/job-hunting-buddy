"""Synthetic public observations; never use candidate browser sessions."""
import hashlib

from jhb.applications.boards import job_identity
from tests.applications.test_greenhouse_source import page, resolve

ASHBY = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'
TEXT = 'Our mission is to make reliable software accessible. ' * 4


def posting(url=ASHBY):
    return {'url': url, 'title': 'Software Engineer', 'description': '<p>'+TEXT+'</p>'}


def test_official_exact_ashby_description_binds_observed_identity_and_hash():
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[posting()])}, ASHBY)
    assert result['identity'] == list(job_identity(ASHBY))
    record = result['verified_job_description']
    assert record['source_url'] == ASHBY and record['job_identity'] == result['identity']
    assert record['text'] == TEXT.strip()
    assert record['sha256'] == hashlib.sha256(record['text'].encode()).hexdigest()
    assert isinstance(record['retrieved_at'], (int, float))


def test_other_job_jsonld_cannot_supply_current_job_description():
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[posting(ASHBY.replace('555555555555','555555555556'))])}, ASHBY)
    assert 'verified_job_description' not in result


def test_changed_explicit_job_and_board_index_never_route():
    changed = ASHBY.replace('555555555555','555555555556')
    result, _ = resolve({ASHBY: page(changed, job_postings=[posting(changed)])}, ASHBY)
    assert result['state'] == 'ambiguous' and result['application_url'] is None
    assert 'verified_job_description' not in result
    index = 'https://jobs.ashbyhq.com/example'
    result, _ = resolve({index: page(index, job_postings=[posting()])}, index)
    assert result['state'] == 'ambiguous' and result['application_url'] is None
    assert 'verified_job_description' not in result


def test_closed_and_verification_pages_never_emit_verified_description():
    for text in ('This job is no longer available', 'Verify you are human'):
        result, _ = resolve({ASHBY: page(ASHBY, text=text, job_postings=[posting()])}, ASHBY)
        assert 'verified_job_description' not in result


def test_application_only_source_reads_same_job_posting_for_jd():
    app=ASHBY+'/application'
    result,transport=resolve({app:page(app),ASHBY:page(ASHBY,job_postings=[posting()])},app)
    assert result['state']=='not_greenhouse' and result['verified_job_description']['source_url']==ASHBY
    assert [args['url'] for name,args in transport.calls if name=='browser_navigate']==[app,ASHBY]


def test_same_job_rendered_chrome_does_not_conflict_with_primary_jsonld():
    result,_=resolve({ASHBY:page(ASHBY,job_postings=[posting()],descriptions=[{'url':ASHBY,'title':'Example jobs','text':'Different site chrome '+TEXT}])},ASHBY)
    assert result['verified_job_description']['title']=='Software Engineer'


def test_distinct_primary_jobposting_descriptions_need_handoff():
    result,_=resolve({ASHBY:page(ASHBY,job_postings=[posting(),{**posting(),'description':'A conflicting role description. '*8}])},ASHBY)
    assert 'verified_job_description' not in result


def test_rendered_employment_requirements_are_not_hidden_by_shorter_jsonld():
    from jhb import eligibility
    restriction = 'Applicants must have work authorization that does not now or in the future require sponsorship of a visa.'
    rendered = {'url': ASHBY, 'title': 'Software Engineer', 'text': TEXT+'\nQualifications\n'+restriction}
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[posting()], descriptions=[rendered])}, ASHBY)
    description = result['verified_job_description']
    assert restriction in description['text']
    assert description['sha256'] == hashlib.sha256(description['text'].encode()).hexdigest()
    assert {r['category'] for r in eligibility.restrictions(description['text'])} == {'visa_sponsorship'}


def test_different_job_container_cannot_donate_restrictions():
    from jhb import eligibility
    rendered = {'url': ASHBY.replace('555555555555', '555555555556'), 'title': 'Software Engineer',
                'text': TEXT+'\nUS citizenship required.'}
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[posting()], descriptions=[rendered])}, ASHBY)
    assert eligibility.restrictions(result['verified_job_description']['text']) == []


def test_conflicting_exact_rendered_containers_require_description_handoff():
    rendered = [{'url': ASHBY, 'title': 'Software Engineer', 'text': TEXT+'\n'+tail}
                for tail in ['US citizenship required.', 'Visa sponsorship is available.']]
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[posting()], descriptions=rendered)}, ASHBY)
    assert 'verified_job_description' not in result


def test_employer_observed_individual_non_greenhouse_frame_is_followed_and_verified():
    employer='https://company.example/careers/software-engineer'
    result,transport=resolve({employer:page(employer,frames=[{'url':ASHBY}]),ASHBY:page(ASHBY,job_postings=[posting()])},employer)
    assert result['application_url']==ASHBY and result['identity']==list(job_identity(ASHBY))
    assert result['verified_job_description']['source_url']==ASHBY
    assert [args['url'] for name,args in transport.calls if name=='browser_navigate']==[employer,ASHBY]



def test_mcp_description_retains_requirement_and_preferred_section_boundaries():
    from jhb import eligibility
    html = ("<h2>Requirements</h2><ul><li>TS/SCI with Polygraph</li>"
            "<li>Build production Python services and maintain distributed data systems with good testing practices.</li></ul>"
            "<h2>Preferred</h2><ul><li>Kubernetes experience</li></ul>")
    result, _ = resolve({ASHBY: page(ASHBY, job_postings=[{**posting(), 'description': html}])}, ASHBY)
    description = result['verified_job_description']
    assert 'Requirements\nTS/SCI with Polygraph\n' in description['text']
    assert '\nPreferred\nKubernetes experience' in description['text']
    assert {item['category'] for item in eligibility.restrictions(description['text'])} == {'security_clearance', 'polygraph'}
    assert description['sha256'] == hashlib.sha256(description['text'].encode()).hexdigest()


def test_rendered_mcp_description_preserves_optional_heading_without_false_exclusion():
    from jhb import eligibility
    text = ('Requirements\nBuild reliable Python backend systems and developer tooling with robust observability and production tests.\n'
            'Preferred\nCurrent TS/SCI clearance\nKubernetes experience')
    result, _ = resolve({ASHBY: page(ASHBY, descriptions=[{'url': ASHBY, 'title': 'Software Engineer', 'text': text}])}, ASHBY)
    description = result['verified_job_description']
    assert description['text'] == text
    assert eligibility.restrictions(description['text']) == []


def test_mcp_required_clearance_reaches_worker_gate_before_candidate_browser(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from jhb.applications import worker
    from jhb.applications.boards import application_hash
    html = ('<h2>Requirements</h2><ul><li>US citizenship</li><li>TS/SCI with Polygraph</li>'
            '<li>Build production Python services and maintain reliable distributed data processing applications.</li></ul>'
            '<h2>Preferred</h2><p>Kubernetes experience</p>')
    outcome, _ = resolve({ASHBY: page(ASHBY, job_postings=[{**posting(), 'description': html}])}, ASHBY)
    job = {'dedupe_hash': application_hash(ASHBY), 'url': ASHBY, 'title': 'Software Engineer', 'company': 'Synthetic',
           'role_classes': 'swe', 'verified_job_description': outcome['verified_job_description']}
    monkeypatch.setattr('jhb.applications.cli_browser.BrowserUseCLI', lambda: pytest.fail('Excluded source reached candidate browser'))
    result, _ = asyncio.run(worker.run_job(job, {'roles': {'sde': {}, 'ml': {}}}, artifacts=tmp_path))
    assert result['state'] == 'skipped'
    assert result['filled'] == []
    assert {item['category'] for item in result['eligibility']['findings']} == {'citizenship', 'security_clearance', 'polygraph'}
