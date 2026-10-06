"""Exact legal-name and future-only prompts reuse verified facts without conflation."""
import copy
import pytest
from jhb.applications import booklet, known_answers, native_question_context, planner

URL = 'https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
LABEL = 'Would you require visa sponsorship at any time in the future?'

def field(label=LABEL, kind='combobox'):
    return {'ref':'question_101','label':label,'type':kind,'required':True,
            'description':'','description_truncated':False,'country_context':'United States','options':[]}


def test_legal_names_use_identity_not_preferred_name_or_signature():
    answers = {k:booklet.answer(v,'synthetic candidate profile') for k,v in {
        'identity.first_name':'Synthetic','identity.last_name':'Candidate',
        'identity.preferred_name':'Nickname','identity.full_name':'Synthetic Candidate'}.items()}
    for label,key in [('Legal First Name','identity.first_name'),('Legal Last Name','identity.last_name')]:
        assert planner.key_for_field(field(label,'text'),answers)==key
    assert planner.key_for_field(field('Previous Legal Last Name','text'),answers) is None
    assert planner.key_for_field(field('Legal First Name of Referrer','text'),answers) is None


def test_future_only_closed_catalog_is_inspected_then_bound_to_separate_fact():
    answers={'eligibility.sponsorship_future':booklet.answer(True,'synthetic explicit future sponsorship response'),
             'eligibility.sponsorship_now':booklet.answer(),
             'eligibility.sponsorship':booklet.answer(True,'synthetic now-or-future response')}
    before=copy.deepcopy(answers);control=field();calls=[]
    assert set(known_answers.catalog_basis(control,answers))=={'eligibility.sponsorship_future'}
    native_question_context.enrich_sync({'url':URL,'fields':[control]},{'url':URL},answers,
        lambda f:calls.append(f['ref']) or {'choices':['Yes','No'],'type':'combobox'})
    assert calls==['question_101']
    assert planner.key_for_field(control,answers)=='eligibility.sponsorship_future'
    plan=planner.deterministic_plan({'fields':[control],'buttons':[]},answers)
    assert plan['bindings']==[{'ref':'question_101','answer_key':'eligibility.sponsorship_future'}]
    assert answers==before and answers['eligibility.sponsorship_now']['status']=='needs_input'


@pytest.mark.parametrize('change',['absent','unverified','unsourced','string','different_country','novel_help','combined_only'])
def test_future_catalog_does_not_derive_unsupported_facts(change):
    answers={'eligibility.sponsorship_future':booklet.answer(True,'synthetic explicit future response')}
    control=field()
    if change in {'absent','combined_only'}:answers.clear()
    if change=='combined_only':answers['eligibility.sponsorship']=booklet.answer(True,'synthetic combined response')
    if change=='unverified':answers['eligibility.sponsorship_future']['status']='needs_input'
    if change=='unsourced':answers['eligibility.sponsorship_future']['source']=''
    if change=='string':answers['eligibility.sponsorship_future']['value']='possibly'
    if change=='different_country':control['country_context']='Canada'
    if change=='novel_help':control['description']='Include all immediate work permits required before starting.'
    assert not known_answers.catalog_basis(control,answers)
    native_question_context.enrich_sync({'url':URL,'fields':[control]},{'url':URL},answers,
        lambda f:pytest.fail('Unverified or out-of-scope catalog must not be probed'))
    assert planner.key_for_field(control,answers) is None
