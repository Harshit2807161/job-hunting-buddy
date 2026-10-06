"""Known facts survive wording and catalog changes without scope expansion."""
import asyncio
import copy
from datetime import date

import pytest

from jhb.applications import booklet, known_answers, native_question_context
from jhb.applications.planner import key_for_field, deterministic_plan, validate_plan


def field(label, choices=(), *, kind='radio', ref='q', country=None, description=''):
    return {'label': label, 'ref': ref, 'type': kind, 'required': True,
            'options': [{'label': x, 'value': x} for x in choices],
            'country_context': country, 'description': description, 'description_truncated': False}


def book():
    return {'answers': {k: booklet.answer(v, 'synthetic explicit candidate fact') for k, v in {
        'education.expected_graduation_date': '2026-12-14', 'preferences.start_date': '2026-12-14',
        'preferences.relocation': True, 'disclosure.gender': 'Male',
        'disclosure.sexual_orientation': 'Heterosexual / straight', 'disclosure.veteran': False,
        'screening.us_government_or_military_5y': False, 'disclosure.disability': False,
        'eligibility.authorized_us': True, 'eligibility.sponsorship': True,
        'disclosure.race': 'Asian', 'disclosure.hispanic': False}.items()},
        'workflow_preferences': {'office_locations': 'Yes to willingness to work at an office/HQ or onsite/hybrid locations; factual eligibility stays truthful',
                                 'relocation': 'Open to relocating anywhere'},
        'roles': {'sde': {}, 'ml': {}},
        'education_records': [{'school': 'Example University', 'degree': 'Master of Science',
            'major': 'Computer Science', 'gpa': '3.9/4.0', 'start_date': '2025-09', 'end_date': '2026-12',
            'expected': True, 'status': 'verified', 'source': 'synthetic original record'},
            {'school': 'Another University', 'degree': 'Bachelor of Science', 'major': 'Mathematics',
             'start_date': '2020-08', 'end_date': '2024-05', 'expected': False,
             'status': 'verified', 'source': 'synthetic original record'}]}


def values():
    return booklet.common_answers(book(), {'source': 'simplify', 'url': 'https://example.test/job'})


def resolve(question, answers=None, *, job=None):
    answers = values() if answers is None else answers
    known_answers.enrich(question, job or {}, answers, as_of=date(2026, 10, 5))
    key = key_for_field(question, answers)
    return answers[key]['value'] if key else None


@pytest.mark.parametrize('label,choices,expected', [
    ('What is your expected graduation month & year?', ['Already graduated', 'Sept - Dec 2026', 'Jan - April 2027'], 'Sept - Dec 2026'),
    ('What is your expected graduation date?', ['December 2026 / January 2027', 'May/June 2027', 'Other'], 'December 2026 / January 2027'),
    ('When are you available to start work?', ['Within 2 weeks', 'December 2026 / January 2027', 'July 2027'], 'December 2026 / January 2027'),
])
def test_explicit_date_fits_one_observed_month_bin(label, choices, expected):
    assert resolve(field(label, choices)) == expected


@pytest.mark.parametrize('choices', [
    ['Already graduated', 'Summer 2027', 'Other'],
    ['December 2026', 'Sept - Dec 2026'],
    ['December 2025 / January 2026', 'Other'],
    ['Dec - Sept 2026'],
])
def test_unknown_ambiguous_or_reversed_date_catalog_does_not_guess(choices):
    assert resolve(field('What is your expected graduation date?', choices)) is None


def test_graduation_projection_needs_same_current_expected_record():
    a=values(); a['education.expected_graduation_date']['value']='2027-12-14'
    assert resolve(field('What is your expected graduation date?', ['December 2027']), a) is None
    a=values(); del a['standing.current_education_school']
    assert resolve(field('What is your expected graduation date?', ['December 2026']), a) is None


def test_indexed_months_do_not_share_an_unscoped_repeater_answer():
    a=values();a['custom.old']={**booklet.answer('January','synthetic old unscoped response'),'question':'End date month'}
    assert resolve(field('End date month*', ['January','May','December'],kind='combobox',ref='end-month--0'),a)=='December'
    assert resolve(field('End date month*', ['January','May','December'],kind='combobox',ref='end-month--1'),a)=='May'
    assert resolve(field('End date month*', ['January','May','December'],kind='combobox',ref='unscoped'),values()) is None


@pytest.mark.parametrize('label', [
    'Are you willing to work four days per week in our San Francisco office?',
    'Are you able to work from our US office three days per week?',
    'Are you able to work from our Marina Del Rey, CA office on Mondays and Thursdays (2 days/week)?',
    'Are you able and willing to report to the office location listed in the job description, in a hybrid capacity?',
])
def test_saved_office_and_relocation_preferences_cover_schedule_wording(label):
    assert resolve(field(label,['Yes','No']))=='Yes'
    a=values();a['preferences.relocation']['status']='needs_input'
    assert resolve(field(label,['Yes','No']),a) is None


@pytest.mark.parametrize('label', [
    'Are you currently based in our San Francisco office?',
    'Are you able to start immediately at our US office?',
    'Are you able to work from our London office without sponsorship?',
    'Are you willing to work four days per week in our San Francisco office and waive overtime?',
])
def test_office_willingness_does_not_claim_current_residence_eligibility_or_extra_consent(label):
    assert resolve(field(label,['Yes','No'])) is None


def test_roleless_catalog_reuses_sourced_policy_not_truthy_unknown_instruction():
    b=book();a=booklet.common_answers(b,{'source':'simplify','url':'https://example.test/job'})
    assert a['standing.office_willingness']['value'] is True
    assert a['standing.discovery_source']['value']=='Simplify'
    assert a['standing.current_education_major']['value']=='Computer Science'
    assert booklet.for_role(b,'sde')['standing.office_willingness']==a['standing.office_willingness']
    for invalid in ('No onsite work', True, {'value':True}, {'value':False,'source':'explicit No'}):
        b['workflow_preferences']['office_locations']=invalid
        assert 'standing.office_willingness' not in booklet.common_answers(b)


@pytest.mark.parametrize('label,choices,expected', [
    ('How would you describe your gender identity? (mark all that apply)', ['Man','Woman','Non-binary'], 'Man'),
    ('How would you describe your sexual orientation? (mark all that apply)', ['Heterosexual','Gay','Asexual'], 'Heterosexual'),
    ('Are you a veteran or active member of the United States Armed Forces? (select one)', ['Yes, I am a veteran or active member','No, I am not a veteran or active member'], 'No, I am not a veteran or active member'),
    ('Race',['Asian (Not Hispanic or Latino)','Hispanic or Latino'], 'Asian (Not Hispanic or Latino)'),
])
def test_disclosure_projections_keep_exact_offered_wording(label, choices, expected):
    assert resolve(field(label, choices,kind='combobox'))==expected


def test_unknown_transgender_active_service_and_disability_history_stay_unknown():
    assert resolve(field('Do you identify as transgender? (select one)', ['Yes','No'])) is None
    a=values();del a['screening.us_government_or_military_5y']
    assert known_answers.enrich(field('Are you a veteran or active member of the United States Armed Forces? (select one)', ['Yes','No']),{},a) is None
    assert resolve(field('Disability status',['No, I have never had a disability','Yes, I have or have had a disability'])) is None
    assert resolve(field('How would you describe your gender identity? (mark all that apply)',['Man','Woman'],description='Report your sex assigned at birth.')) is None


@pytest.mark.parametrize('country,expected', [('United States','Yes'),('United Kingdom',None),(None,None)])
def test_relative_work_authorization_requires_verified_posting_context(country,expected):
    assert resolve(field('Are you authorized to work in the country where the job is located?', ['Yes','No'],country=country))==expected


def test_country_annotation_is_explicit_job_metadata_not_candidate_residence():
    f=field('Are you legally authorized to work in the country where the job is located?',['Yes','No'])
    snapshot={'fields':[f]};booklet.annotate_work_country(snapshot,{'work_country':'United States'})
    assert f['country_context']=='united states' and resolve(f)=='Yes'
    f['country_context']='canada';booklet.annotate_work_country(snapshot,{'work_country':'United States'});assert f['country_context']=='canada'


def test_expanded_authorization_choices_do_not_infer_any_employer_or_citizenship():
    for choices in [['I am authorized to work in the United States for any employer'], ['Yes, I am a US citizen'], ['Yes, without sponsorship']]:
        assert resolve(field('Are you legally authorized to work in the country where the job is located?',choices,country='United States')) is None


def test_sponsorship_combined_fact_does_not_resolve_timing_without_separate_facts():
    f=field('Will you now or in the future require company sponsorship to retain or extend your work authorization in the country where the job is located?',[
        'Yes, I will require immigration sponsorship now to legally work in the country where the job is located.',
        'Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located.',
        'No, I do not and will not require immigration sponsorship to legally work in the country where the job is located.'],country='United States')
    a=values();assert resolve(f,a) is None
    a['eligibility.sponsorship_future']=booklet.answer(True,'explicit future need');assert resolve(f,a) is None
    a['eligibility.sponsorship_now']=booklet.answer(False,'explicit no present need');assert 'in the future' in resolve(f,a)
    a['eligibility.sponsorship_now']['status']='needs_input';assert resolve(f,a) is None


def test_us_sponsorship_punctuation_and_new_relative_wording_are_not_new_questions():
    assert resolve(field('Will you now or in the future require visa sponsorship to work in the U.S.?',['Yes','No']))=='Yes'
    assert resolve(field('Will you now or in the future require sponsorship for employment visa status in this country?',['Yes','No'],country='United States'))=='Yes'
    assert resolve(field('Will you now or in the future require sponsorship for employment visa status in this country?',['Yes','No'],country='Canada')) is None


def test_derived_answer_invalidates_if_original_fact_or_observed_choices_change():
    a=values();f=field('How would you describe your sexual orientation? (mark all that apply)',['Heterosexual','Gay'])
    assert resolve(f,a)=='Heterosexual'
    a['disclosure.sexual_orientation']['status']='needs_input';assert resolve(f,a) is None
    a=values();assert resolve(f,a)=='Heterosexual';changed=copy.deepcopy(f);changed['options'].append({'label':'Straight'});assert key_for_field(changed,a) is None


def test_empty_greenhouse_catalog_is_inspected_before_planning_known_questions():
    f=field('What is your expected graduation month & year?',kind='combobox');a=values()
    url='https://job-boards.greenhouse.io/example/jobs/123';snap={'url':url,'fields':[f],'buttons':[]};calls=[]
    async def describe(item):calls.append(item['ref']);return {'type':'combobox','choices':['Already graduated','Sept - Dec 2026']}
    asyncio.run(native_question_context.enrich_async(snap,{'url':url},a,describe))
    assert calls==['q'];known_answers.enrich(f,{},a,as_of=date(2026,10,5));plan=validate_plan(deterministic_plan(snap,a),snap,a)
    assert a[plan['bindings'][0]['answer_key']]['value']=='Sept - Dec 2026'
    assert f['native_question_catalog']['source']=='owned_native_dropdown'
    assert not known_answers.needs_catalog(field('Do you identify as transgender? (select one)',kind='combobox'),a)


def test_education_month_never_invents_month_from_year_only():
    b=book();b['education_records'][1]['end_date']='2024'
    assert 'education.1.end_month' not in booklet.common_answers(b)


@pytest.mark.parametrize('choice', ['December 0000', 'January 0000 - May 2026', 'Summer 2026'])
def test_invalid_calendar_bin_is_not_an_exception_or_match(choice):
    assert resolve(field('What is your expected graduation date?', [choice])) is None


def test_current_gpa_catalog_uses_original_scale_and_rejects_overlap():
    assert resolve(field('What is your current GPA', ['3.0-3.49','3.5-4.0'],kind='combobox'))=='3.5-4.0'
    assert resolve(field('What is your current GPA', ['3.5-4.0','3.8-4.0'],kind='combobox')) is None
    a=values();a['standing.current_education_gpa']['value']='9.6/10'
    assert resolve(field('What is your current GPA', ['3.5-4.0'],kind='combobox'),a) is None


@pytest.mark.parametrize("change", ["missing_source", "unverified", "out_of_range", "invalid_record"])
def test_indexed_education_never_falls_back_to_another_record(change):
    b=book(); ref='school--1'
    if change=='missing_source': b['education_records'][1].pop('source')
    if change=='unverified': b['education_records'][1]['status']='needs_input'
    if change=='out_of_range': ref='school--3'
    if change=='invalid_record': b['education_records'][1]=None
    a=booklet.common_answers(b)
    assert a['education.school']['value']=='Example University'
    assert key_for_field(field('School*',kind='combobox',ref=ref),a) is None


def test_missing_first_education_source_is_skipped_without_losing_other_record():
    b=book(); b['education_records'][0].pop('source')
    a=booklet.common_answers(b)
    assert 'education.school' not in a
    assert 'education.0.school' not in a
    assert a['education.1.school']['value']=='Another University'
    assert 'standing.current_education_school' not in a


def test_role_catalog_preserves_exact_job_discovery_over_old_generic_and_role_values():
    b=book(); stale=booklet.answer('Indeed','synthetic old source')
    b['answers']['standing.discovery_source']=stale
    b['roles']['ml']['standing.discovery_source']=stale
    b['roles']['ml']['documents.resume']=booklet.answer('/synthetic/ml.pdf','synthetic selected resume')
    job={'source':'simplify','company':'Example Company','url':'https://example.test/job'}
    before=copy.deepcopy(b)
    a=booklet.for_role(b,'ml',job=job)
    assert a['standing.discovery_source']['value']=='Simplify'
    assert a['standing.discovery_source']['company_question']=='How did you hear about Example Company?'
    assert a['documents.resume']['value']=='/synthetic/ml.pdf'
    assert b==before
