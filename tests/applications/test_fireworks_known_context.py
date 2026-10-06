"""Observed aliases reuse exact verified facts; unfamiliar scope remains unresolved."""
import copy

import pytest

from jhb.applications import booklet, known_answers, planner, question_routing


def field(label, *, ref='f', kind='text', choices=(), description=''):
    return {'ref':ref,'label':label,'type':kind,'required':True,'description':description,
            'description_truncated':False,'options':[{'label':x,'value':x} for x in choices]}


def synthetic_book():
    return {'answers':{k:booklet.answer(v,'synthetic explicit candidate fact') for k,v in {
        'identity.full_name':'Morgan Example','preferences.start_date':'2027-06-14',
        'preferences.relocation':True,'eligibility.authorized_us':True,'eligibility.sponsorship':True,
        'preferences.salary':170000}.items()},'workflow_preferences':{
        'office_locations':{'value':True,'source':'synthetic explicit office willingness'},
        'application_compliance':{'value':True,'source':'synthetic explicit interview rules and AI transcription approval'}},
        'roles':{'sde':{},'ml':{}}}


def resolve(control, *, book=None, country=None):
    b=book or synthetic_book(); before=copy.deepcopy(b); answers=booklet.common_answers(b)
    snapshot={'fields':[control]};booklet.annotate_work_country(snapshot,{'work_country':country})
    known_answers.enrich(control,{'work_country':country},answers)
    key=planner.key_for_field(control,answers)
    assert b==before
    return answers.get(key) if key else None


def test_exact_legal_name_reuses_verified_full_name_without_altering_book():
    assert resolve(field('Legal Name'))['value']=='Morgan Example'
    assert resolve(field('Legal Name',description='Enter a different legal identity used for this employer.')) is None
    b=synthetic_book();b['answers']['identity.full_name']['status']='needs_input'
    assert resolve(field('Legal Name'),book=b) is None


def test_ideal_start_date_preserves_verified_calendar_day_and_unknown_help():
    control=field('If offered a position, what is your ideal start-date?')
    control['calendar_format']='MM/DD/YYYY'
    assert resolve(control)['value']=='2027-06-14'
    assert control['calendar_format']=='MM/DD/YYYY'
    control['description']='You must start before your work authorization is issued.'
    assert resolve(control) is None


def test_hybrid_schedule_uses_both_explicit_office_and_relocation_willingness():
    control=field('Are you open to a hybrid schedule with in-office days on Monday, Wednesday, and Friday?',kind='radio',choices=['Yes','No'])
    assert resolve(control)['value']=='Yes'
    b=synthetic_book();b['answers']['preferences.relocation']['value']=False
    assert resolve(control,book=b) is None
    control['description']='Also certify that you already live within walking distance.'
    assert resolve(control) is None


@pytest.mark.parametrize('country,expected',[('United States','Yes'),(None,None),('Canada',None),('United Kingdom',None)])
def test_relative_sponsorship_requires_current_explicit_posting_country(country,expected):
    control=field('Will you now or in the future require visa sponsorship to work in the country where this position is located?',kind='radio',choices=['Yes','No'])
    answer=resolve(control,country=country)
    assert (answer['value'] if answer else None)==expected
    if country:assert control['country_context']==country.lower()


def recording_field():
    return field('Interview Recording Consent',ref='ashby:_systemfield_recording_consent',kind='radio',
        choices=['Yes, I consent to be recorded','Opt out of recording'],description=known_answers.INTERVIEW_RECORDING_DESCRIPTION)


def test_explicit_transcription_consent_is_shared_with_question_router():
    b=synthetic_book();control=recording_field();answers=booklet.common_answers(b)
    assert resolve(control,book=b)['value']=='Yes, I consent to be recorded'
    assert question_routing.field_route(control,answers,job={})==question_routing.KNOWN
    b['workflow_preferences']['interview_recording']={'value':False,'source':'synthetic explicit opt-out'}
    assert resolve(control,book=b)['value']=='Opt out of recording'


@pytest.mark.parametrize('change',['generic_compliance','no_policy','extra_help','truncated','wrong_ref','changed_options',
                                  'negated_recording','mention_without_approval'])
def test_recording_never_inherits_generic_terms_or_changed_scope(change):
    b=synthetic_book();control=recording_field()
    if change=='generic_compliance':b['workflow_preferences']['application_compliance']['source']='synthetic acceptance of application terms'
    if change=='no_policy':del b['workflow_preferences']['application_compliance']
    if change=='negated_recording':b['workflow_preferences']['application_compliance']['source']='Applicant accepted terms, not AI transcription; no interview recording approval'
    if change=='mention_without_approval':b['workflow_preferences']['application_compliance']['source']='Applicant accepted terms; AI transcription was discussed'
    if change=='extra_help':control['description']+=' Recordings may be sold to third parties.'
    if change=='truncated':control['description_truncated']=True
    if change=='wrong_ref':control['ref']='unknown-consent'
    if change=='changed_options':control['options'].append({'label':'Yes, including public distribution'})
    assert resolve(control,book=b) is None


def test_compensation_alias_uses_only_verified_salary_answer():
    control=field('Do you have any initial compensation expectations?',kind='number');control['required']=False
    assert resolve(control)['value']==170000
    b=synthetic_book();b['answers']['preferences.salary']['status']='needs_input'
    assert resolve(control,book=b) is None
    control['description']='Enter your present salary, not your expectation.'
    assert resolve(control) is None
