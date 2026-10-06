"""Native contact tasks and exact optional/EEOC wording reuse sourced facts."""
import copy
import pytest
from jhb.applications import booklet, known_answers, planner, question_routing


def book():
    b={'answers':{k:booklet.answer(v,'synthetic explicit candidate fact') for k,v in {
        'identity.first_name':'Synthetic', 'identity.last_name':'Candidate',
        'preferences.application_city':'San Diego, CA','identity.state':'CA','identity.country':'United States',
        'screening.personal_referral':False, 'disclosure.race':'Asian','disclosure.hispanic':False}.items()},
        'roles':{'sde':{},'ml':{}},'custom_answers':{},'workflow_preferences':{
            'preferred_first_name':{'optional':'leave blank','required':'use identity.first_name'}}}
    b['answers']['identity.preferred_name']={'value':None,'status':'declined','source':'synthetic explicit optional blank'}
    return b


def field(label, *, ref='f', kind='text', required=False, choices=(), description=''):
    return {'label':label,'ref':ref,'type':kind,'required':required,'choices':list(choices),
        'options':[{'label':c} for c in choices],'description':description,'description_truncated':False}


def route(f,b=None):
    return question_routing.field_route(f,booklet.common_answers(b or book()),job={})


def test_exact_native_contact_is_agent_catalog_work_then_binds_only_one_observed_city():
    f=field('Where are you currently located?',ref='ashby:_systemfield_location:control:0',kind='combobox',required=True)
    a=booklet.common_answers(book())
    assert route(f)==question_routing.KNOWN
    assert set(known_answers.catalog_basis(f,a))=={'preferences.application_city','identity.state','identity.country'}
    assert planner.key_for_field(f,a) is None
    f['options']=[{'label':'San Diego, California, United States'}]
    known_answers.enrich(f,{},a);key=planner.key_for_field(f,a)
    assert a[key]['value']=={'query':'San Diego','choice':'San Diego, California, United States'}


@pytest.mark.parametrize('change',['wrong_ref','novel_instruction','missing_country','different_state'])
def test_contact_probe_never_guesses_a_location_or_scope(change):
    b=book();f=field('Where are you currently located?',ref='ashby:_systemfield_location:control:0',kind='combobox')
    if change=='wrong_ref':f['ref']='arbitrary-location'
    if change=='novel_instruction':f['description']='Select the office you currently work from.'
    if change=='missing_country':del b['answers']['identity.country']
    if change=='different_state':b['answers']['identity.state']['value']='NY'
    assert not known_answers.catalog_basis(f,booklet.common_answers(b))
    assert route(f,b)==question_routing.CANDIDATE


def test_preferred_optional_blank_and_required_name_policy_are_shared():
    f=field('Preferred Name (if applicable)');b=book();before=copy.deepcopy(b)
    assert route(f,b)==question_routing.KNOWN
    a=booklet.common_answers(b)
    assert a[planner.key_for_field(f,a)]['status']=='declined'
    f['required']=True
    assert a[planner.key_for_field(f,a)]['value']=='Synthetic'
    del b['workflow_preferences']['preferred_first_name']
    assert route(f,b)==question_routing.CANDIDATE
    assert before['answers']==b['answers']


def test_known_no_personal_referral_resolves_only_conditional_optional_details():
    f=field("If someone at Example Corp referred you, we’d love to give them credit! Please share their name and your connection.")
    a=booklet.common_answers(book());key=planner.key_for_field(f,a)
    assert a[key]['status']=='declined' and a[key]['value'] is None
    assert route(f)==question_routing.KNOWN
    for change in ['required','positive_referral','unknown_referral','extra_instruction','different_prompt']:
        b=book();changed=copy.deepcopy(f)
        if change=='required':changed['required']=True
        if change=='positive_referral':b['answers']['screening.personal_referral']['value']=True
        if change=='unknown_referral':b['answers']['screening.personal_referral']['status']='needs_input'
        if change=='extra_instruction':changed['description']='Otherwise explain how you discovered this role.'
        if change=='different_prompt':changed['label']='Who can recommend your work?'
        assert route(changed,b)==question_routing.CANDIDATE


def race_field():
    return field('Race',ref='ashby:_systemfield_eeoc_race',kind='radio',choices=['Asian (Not Hispanic or Latino)','Hispanic or Latino'],description=known_answers.EEOC_RACE_DESCRIPTION)


def test_standard_eeoc_definition_help_is_compatible_with_explicit_race_and_hispanic_facts():
    f=race_field();a=booklet.common_answers(book())
    known_answers.enrich(f,{},a);key=planner.key_for_field(f,a)
    assert a[key]['value']=='Asian (Not Hispanic or Latino)'
    assert route(f)==question_routing.KNOWN
    b=book();del b['answers']['disclosure.hispanic']
    assert route(f,b)==question_routing.CANDIDATE


@pytest.mark.parametrize('change',['extra_instruction','changed_definition','wrong_ref','truncated','own_words'])
def test_eeoc_definition_allowlist_does_not_accept_novel_instructions(change):
    f=race_field()
    if change=='extra_instruction':f['description']+=' Also declare you are a US citizen.'
    if change=='changed_definition':f['description']=f['description'].replace('Indian Subcontinent','Europe')
    if change=='wrong_ref':f['ref']='custom-demographics'
    if change=='truncated':f['description_truncated']=True
    if change=='own_words':f['description']+=' Please answer in your own words.'
    assert route(f)==question_routing.CANDIDATE
