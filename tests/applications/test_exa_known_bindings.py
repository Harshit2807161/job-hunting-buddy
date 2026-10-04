"""Reuse precise saved facts without guessing compound immigration answers."""
import pytest
from jhb.applications.booklet import answer
from jhb.applications.planner import key_for_field
from jhb.applications.worker import _verified_start_month, _observed_relocation_choice


def field(label,type='text'):
    return {'ref':'synthetic','label':label,'type':type,'required':True}


@pytest.mark.parametrize('value,expected',[('January 2027','January 2027'),('Jan 2027','January 2027'),('2027-01','January 2027'),('2027-01-14','January 2027'),('2027-02-30',None),('ASAP',None)])
def test_calendar_availability_is_reduced_to_month_without_inventing_dates(value,expected):
    original=answer(value,'Synthetic verified availability')
    result=_verified_start_month(original)
    assert (result['value'] if result else None)==expected
    if result:
        assert result['source']['original_value']==value and result['source']['precision']=='month'
    assert original['value']==value
    assert _verified_start_month({**original,'status':'needs_input'}) is None


def test_exact_start_month_and_company_discovery_prompts_reuse_verified_context():
    answers={'standing.start_month':answer('January 2027','Synthetic date'),
             'standing.discovery_source':{**answer('Simplify','Synthetic Phase 1 discovery'),'company_question':'How did you hear about Example?'}}
    assert key_for_field(field("Earliest month you'd be able to join"),answers)=='standing.start_month'
    assert key_for_field(field('How did you hear about Example?'),answers)=='standing.discovery_source'
    assert key_for_field(field('How did you hear about Different Employer?'),answers) is None
    assert key_for_field(field('Were you referred by an Example employee?'),answers) is None


def test_relocation_or_question_is_true_only_when_verified_willingness_satisfies_it():
    q=field('Are you based in San Francisco or open to relocating?','radio')
    assert key_for_field(q,{'preferences.relocation':answer(True,'Synthetic approval')})=='preferences.relocation'
    assert key_for_field(q,{'preferences.relocation':answer(False,'Synthetic approval')}) is None
    assert key_for_field(q,{'preferences.relocation':answer(True,status='needs_input')}) is None
    assert key_for_field(field('Are you currently based in San Francisco?','radio'),{'preferences.relocation':answer(True,'Synthetic approval')}) is None


def test_compound_visa_type_and_expiry_cannot_be_answered_from_sponsorship_boolean():
    q=field('Do you require Visa sponsorship to work in your selected location? If so, which one? And when does your Visa expire?')
    assert key_for_field(q,{'eligibility.sponsorship':answer(True,'Synthetic approval')}) is None


def test_observed_relocation_choice_preserves_willingness_without_claiming_residence():
    q = {**field('Are you based in San Francisco or open to relocating?','radio'),
         'options': [{'label':'San Francisco based','value':'local'}, {'label':'Open to relocating','value':'relocate'}]}
    answers = {'preferences.relocation': answer(True,'Synthetic explicit relocation approval')}
    record = _observed_relocation_choice(q, answers)
    assert record['value'] == 'Open to relocating'
    assert record['source']['derived_from'] == 'preferences.relocation'
    assert record['source']['original_source'] == answers['preferences.relocation']['source']
    answers['standing.relocation_choice'] = record
    assert key_for_field(q, answers) == 'standing.relocation_choice'
    assert answers['preferences.relocation']['value'] is True


@pytest.mark.parametrize('change', [
    {'status':'needs_input'}, {'value':False}, {'source':None},
])
def test_observed_relocation_choice_requires_verified_explicit_willingness(change):
    q = {**field('Are you based in San Francisco or open to relocating?','radio'),
         'options': [{'label':'Open to relocating'}]}
    assert _observed_relocation_choice(q, {'preferences.relocation':{**answer(True,'Synthetic approval'),**change}}) is None


def test_relocation_choice_is_not_guessed_for_an_unobserved_or_ambiguous_option():
    answers = {'preferences.relocation': answer(True,'Synthetic approval')}
    q = {**field('Are you based in San Francisco or open to relocating?','radio'), 'options':[{'label':'San Francisco based'}]}
    assert _observed_relocation_choice(q, answers) is None
    q['options'] = [{'label':'Open to relocating'}, {'label':'Open to relocating'}]
    assert _observed_relocation_choice(q, answers) is None
    q['label'] = 'Are you currently based in San Francisco?'
    q['options'] = [{'label':'Open to relocating'}]
    assert _observed_relocation_choice(q, answers) is None
