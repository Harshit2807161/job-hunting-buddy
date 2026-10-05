"""Experience prompts get brief source-grounded prose, not bullet dumps."""
import copy
import hashlib
import time

import pytest

from jhb import eligibility
from jhb.applications import booklet, narratives


def job():
    text='The software engineering role builds cloud infrastructure, Python tools and deployment APIs.'
    job={'url':'https://job-boards.greenhouse.io/synthetic/jobs/123','company':'Synthetic Company'}
    job['verified_job_description']={'status':'verified','text':text,
        'source_url':eligibility.description_url(job),'retrieved_at':time.time(),
        'sha256':hashlib.sha256(text.encode()).hexdigest()}
    return job


def answers():
    return {'role.experience':booklet.answer(
        'Amazon Web Services           Jun 2026 – Sep 2026\n'
        'Engineering Intern\n'
        '• Engineered a Python operations tool that reduced review from over 4 hours to under 30 minutes.\n'
        '• Deployed automated incident analysis with an API, delivering results within 15 minutes.\n'
        '• Presented deployment findings to 20 developers.\n'
        'Other Company                Feb 2025 – Aug 2025\n'
        'Engineer\n'
        '• Built Python cloud tools saving 30 hours per month.',
        {'provider':'synthetic selected SDE source'})}


def prompt():
    return {'label':"What's something you worked on that you were proud of?",'type':'textarea','ref':'q'}


def test_proud_work_curates_natural_first_person_paragraph_and_exact_quantified_source():
    facts=answers();before=copy.deepcopy(facts)
    result=narratives.proposal(prompt(),job(),facts)
    assert result['proposed'] is True and result['source']['review_status']=='proposed'
    text=result['value']
    assert text.startswith('At Amazon Web Services, I engineered')
    assert 'I also deployed' in text and 'Python' in text and 'API' in text
    assert 'over 4 hours' in text and 'under 30 minutes' in text and 'within 15 minutes' in text
    assert '\n' not in text and '•' not in text and len(text.split())<=120
    assert '20 developers' not in text and 'Other Company' not in text
    assert len(result['source']['evidence_bullets'])==2
    assert all(bullet in facts['role.experience']['value'] for bullet in result['source']['evidence_bullets'])
    assert result['source']['selected_role_facts']['sha256']==hashlib.sha256(facts['role.experience']['value'].encode()).hexdigest()
    assert facts==before
    assert not any(word in text.lower() for word in ('passionate','proud','excited','failed','because'))


def test_long_source_claims_are_never_cut_at_word_limit_or_metric_qualifier():
    record=answers()['role.experience']
    long_claim='• Built a Python tool '+('with measurable improvements ' * 35)+'for over 40 developers.'
    record['value']='Example Co Jun 2026 – Sep 2026\n'+long_claim
    assert narratives.proposal(prompt(),job(),{'role.experience':record}) is None


def test_unsupported_resume_fragments_require_drafting_review_instead_of_awkward_prose():
    record=booklet.answer('Example Co Jun 2026 – Sep 2026\n• Responsible for an API serving 200 developers.', 'synthetic')
    assert narratives.proposal(prompt(),job(),{'role.experience':record}) is None


def test_numbered_achievement_bullets_keep_original_source_format():
    job_context=job();job_context['observed_application_questions']=[narratives.ACCOMPLISHMENTS_PROMPT]
    field={**prompt(),'label':narratives.ACCOMPLISHMENTS_PROMPT}
    result=narratives.proposal(field,job_context,answers())
    assert result['value'].startswith('• Engineered') and '\n• Deployed' in result['value']
    assert '\n• Presented' in result['value'] and '20 developers' in result['value']
    assert result['source']['method']=='verified_resume_bullets'


@pytest.mark.parametrize('change',['no_ai','unverified','other_role'])
def test_paragraph_renderer_never_bypasses_candidate_wording_or_selected_source(change):
    facts=answers();field=prompt()
    if change=='no_ai':field['description']='Please answer in your own words without AI.'
    elif change=='unverified':facts['role.experience']['status']='needs_input'
    else:facts={'other_role.experience':facts.pop('role.experience')}
    assert narratives.proposal(field,job(),facts) is None
