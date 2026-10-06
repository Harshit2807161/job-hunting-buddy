"""Ground brief proposed prose in exact official jobs and chosen-role records."""
import hashlib
import time

import pytest

from jhb.applications.booklet import answer
from jhb.applications import narratives
from jhb.applications.boards import job_identity

URL='https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'

def job(company='Example',text='Our ultimate goal is to make search results useful and measurable. We build embedding models and APIs.'):
    return {'url':URL,'company':company,'selected_role':'ml','verified_job_description':{
        'status':'verified','source_url':URL,'job_identity':list(job_identity(URL)),'retrieved_at':time.time(),
        'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}}

def field(label):return {'ref':'synthetic','label':label,'type':'textarea','required':True}

def experience():
    return {'role.experience':answer('Example Lab  Jan 2024 – May 2024\nEngineer\n• Improved API latency by 20%.\nAmazon Web Services  Jun 2024 – Aug 2024\nEngineer\n• Improved API latency by 10%.','Synthetic chosen ML source')}


@pytest.mark.parametrize('company',['Example','Another Company'])
def test_short_company_interest_is_generic_and_uses_only_exact_official_goal(company):
    record=narratives.proposal(field(f'Why are you interested in working at {company}? (can be short)'),job(company),{})
    assert record['value']==f"I'm interested in helping {company} make search results useful and measurable."
    assert record['source']['description_sha256']==job(company)['verified_job_description']['sha256']
    assert 'source_url' in record['source']
    assert narratives.proposal(field('Why are you interested in working at Different? (can be short)'),job(company),{}) is None


def test_proud_work_preserves_metric_and_prefers_aws_on_equal_relevance():
    answers=experience();before=repr(answers)
    record=narratives.proposal(field("What's something you worked on that you were proud of?"),job(text='Build Python APIs and improve their reliability.'),answers)
    assert record['source']['section']=='Amazon Web Services'
    assert 'improved API latency by 10%.' in record['value']
    assert '\n' not in record['value'] and '•' not in record['value']
    assert record['source']['evidence_bullets'] == ['• Improved API latency by 10%.']
    assert '20%' not in record['value'] and record['proposed'] is True
    assert record['source']['review_status']=='proposed'
    assert repr(answers)==before


def test_proud_work_chooses_more_relevant_verified_section_and_never_other_role():
    answers=experience()
    answers['role.experience']['value']=answers['role.experience']['value'].replace('Improved API latency by 20%.','Improved embedding model search quality by 20%.')
    answers['other_role.experience']=answer('Other  Jan 2024 – May 2024\nEngineer\n• Improved embedding model search quality by 99%.','Wrong role')
    record=narratives.proposal(field("What's something you worked on that you were proud of?"),job(),answers)
    assert record['source']['section']=='Example Lab' and '20%' in record['value'] and '99%' not in record['value']
    answers['role.experience']['status']='needs_input'
    assert narratives.proposal(field("What's something you worked on that you were proud of?"),job(),answers) is None


def test_motivation_is_explicitly_a_proposal_from_verified_role_selection():
    record=narratives.proposal(field('What motivates you?'),job(),experience())
    assert record['proposed'] and record['source']['review_status']=='proposed'
    assert record['source']['selected_role']=='ml'
    assert 'applied ML research' in record['value']
    unknown=job();unknown.pop('selected_role')
    assert narratives.proposal(field('What motivates you?'),unknown,experience()) is None
    assert narratives.proposal(field('What motivates you?'),job(),{}) is None


@pytest.mark.parametrize('label',["What's something you worked on that you were proud of? Please, no AI text",'What motivates you? Please, no AI text','How many years of production ML experience do you have?'])
def test_candidate_only_or_factual_questions_have_no_generated_route(label):
    assert narratives.proposal(field(label),job(),experience()) is None


def test_project_improvement_does_not_prove_requested_failure_history():
    answers = {'role.experience': answer(
        'Synthetic Startup  Jan 2024 – May 2024\nEngineer\n'
        '• Implemented a hybrid filtering recommender system, replacing vanilla popularity-based sorting '
        'and increasing CTR by 30%, with improvements validated through A/B testing and Azure Monitor.',
        'Synthetic selected-role resume')}
    prompt = 'Describe one technical project you built. What did you own, what failed, and how did you test the fix? (150 words max.)'
    assert narratives.proposal(field(prompt), job(), answers) is None
