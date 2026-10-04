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


def test_employer_observed_individual_non_greenhouse_frame_is_followed_and_verified():
    employer='https://company.example/careers/software-engineer'
    result,transport=resolve({employer:page(employer,frames=[{'url':ASHBY}]),ASHBY:page(ASHBY,job_postings=[posting()])},employer)
    assert result['application_url']==ASHBY and result['identity']==list(job_identity(ASHBY))
    assert result['verified_job_description']['source_url']==ASHBY
    assert [args['url'] for name,args in transport.calls if name=='browser_navigate']==[employer,ASHBY]
