from datetime import date
from copy import deepcopy

import pytest

from jhb.applications import booklet
from jhb.applications.planner import completed_cs_degree_answer, deterministic_plan, key_for_field, validate_plan


@pytest.mark.parametrize("label,key,value", [
    ("Would you require sponsorship to work in the United States now or in the future?", "eligibility.sponsorship", True),
    ("Will you now or in the future require immigration sponsorship by our company to attain or maintain your employment eligibility (e.g., H-1B, E-3, TN, O-1, STEM OPT EAD, or any immigration work authorization requiring a written submission from the company to a government agency)?", "eligibility.sponsorship", True),
    ("Are you able to work out of the Pittsburgh, PA office 5 days a week?", "standing.office_willingness", True),
    ("Do you have a non-compete, non-disclosure, non-solicitation agreement or any other post-employment agreement?", "screening.non_compete", False),
    ("What is your ideal start date?", "preferences.start_date", "January 2027"),
    ("Your current location", "preferences.application_city", "Synthetic City, CA"),
    ("What are your base salary expectations?", "preferences.salary", 100000),
    ("Are you at least 18 years old?", "eligibility.over_18", True),
    ("Are you willing to work in the office 5 days a week?", "standing.office_willingness", True),
    ("Are you willing to work in an office setting 5 days a week?", "standing.office_willingness", True),
    ("Please provide your current address.", "standing.mailing_address", "123 Synthetic Street, Synthetic City, CA, 00000, United States"),
])
def test_observed_wording_reuses_only_approved_answer(label, key, value):
    answers = {key: booklet.answer(value, "explicit synthetic standing preference")}
    snapshot = {"fields": [{"ref": "q", "label": label + "*", "type": "combobox", "required": True}], "buttons": []}
    plan = validate_plan(deterministic_plan(snapshot, answers), snapshot, answers)
    assert plan['bindings'] == [{'ref': 'q', 'answer_key': key}]
    assert answers[key]['value'] == value


@pytest.mark.parametrize("label", [
    "Would you require sponsorship to work in the United States now?",
    "Are you medically able to perform every essential function without accommodation?",
    "Are you able to work out of the Pittsburgh office and travel overseas every month?",
    "Do you have a signed confidentiality agreement with a previous employer?",
    "Can you provide proof that you are authorized to work in the United States?",
])
def test_related_wording_does_not_invent_new_facts(label):
    answers = {'eligibility.sponsorship': booklet.answer(True, 'synthetic'),
               'eligibility.authorized_us': booklet.answer(True, 'synthetic'),
               'standing.office_willingness': booklet.answer(True, 'synthetic'),
               'screening.non_compete': booklet.answer(False, 'synthetic')}
    assert key_for_field({'ref': 'q', 'label': label, 'type': 'combobox'}, answers) is None


def test_new_known_fact_template_does_not_override_employer_answer():
    label = "Please provide your current address."
    values = {'identity.address': booklet.answer('123 Synthetic Street', 'synthetic standing fact'),
              'custom.employer_address': {**booklet.answer('456 Other Street', 'explicit synthetic employer answer'),
                                          'question': label}}
    assert key_for_field({'ref': 'q', 'label': label, 'type': 'text'}, values) == 'custom.employer_address'


def test_discovery_source_never_invents_personal_referral_or_overrides_explicit_source():
    values={'standing.discovery_source':booklet.answer('Simplify','synthetic Phase 1 discovery'),
            'screening.referral':booklet.answer()}
    field={'ref':'source','label':'How did you hear about us?','type':'text'}
    assert key_for_field(field,values)=='standing.discovery_source'
    assert key_for_field({**field,'label':'Who personally referred you?'},values) is None
    values['screening.referral']=booklet.answer('Candidate supplied source','explicit synthetic user')
    assert key_for_field(field,values)=='screening.referral'


def test_proof_and_personal_referral_require_separate_explicit_records():
    proof={'ref':'proof','label':'Can you provide proof that you are authorized to work in the United States?','type':'combobox'}
    values={'eligibility.authorized_us':booklet.answer(True,'synthetic')}
    assert key_for_field(proof,values) is None
    values['eligibility.proof_authorization_us']=booklet.answer(True,'explicit synthetic user proof confirmation')
    assert key_for_field(proof,values)=='eligibility.proof_authorization_us'
    values['screening.personal_referral']=booklet.answer(False,'explicit synthetic standing preference')
    assert key_for_field({**proof,'label':'Did someone refer you to apply to this role?'},values)=='screening.personal_referral'


def education(major='Computer Science', expected=False, end='2025-05', degree='Bachelor of Science'):
    return {'school': 'Synthetic University', 'degree': degree, 'major': major,
            'expected': expected, 'end_date': end, 'status': 'verified', 'source': 'synthetic original resume'}


def test_expected_cs_masters_and_completed_other_bachelors_answer_no():
    records = [education(expected=True, end='2026-12', degree='Master of Science'),
               education(major='Mathematics and Computing')]
    original = deepcopy(records)
    record = completed_cs_degree_answer(records, as_of=date(2026, 10, 4))
    assert record['value'] is False and record['status'] == 'verified'
    assert record['source']['as_of'] == '2026-10-04'
    assert record['source']['method'] == 'completed_degree_exact_major'
    assert records == original
    answers = {'standing.completed_cs_degree': record}
    field = {'ref': 'q', 'label': 'Do you have your bachelor’s degree or master’s degree in computer science?', 'type': 'combobox'}
    assert key_for_field(field, answers) == 'standing.completed_cs_degree'


@pytest.mark.parametrize('degree', ['Bachelor of Science', 'Master of Science', 'B.S.', 'M.S.', "Bachelor's", "Master's"])
def test_completed_exact_cs_degree_proves_yes(degree):
    record = completed_cs_degree_answer([education(degree=degree)], as_of=date(2026, 10, 4))
    assert record['value'] is True


def test_expected_date_passing_does_not_prove_degree_was_awarded():
    assert completed_cs_degree_answer([education(expected=True, end='2025-05')], as_of=date(2026, 10, 4)) is None


def test_month_only_completion_date_is_conservative_within_that_month():
    assert completed_cs_degree_answer([education(end='2026-10')], as_of=date(2026, 10, 4)) is None
    assert completed_cs_degree_answer([education(end='2026-10')], as_of=date(2026, 10, 31))['value'] is True


@pytest.mark.parametrize('records', [[], [education(end='unknown')], [education(end='2025-13')],
                                      [{**education(), 'status': 'needs_input'}], [{**education(), 'major': None}]])
def test_incomplete_records_require_handoff(records):
    assert completed_cs_degree_answer(records, as_of=date(2026, 10, 4)) is None


def test_related_field_and_equivalence_questions_are_not_bound_to_exact_cs_answer():
    answers = {'standing.completed_cs_degree': booklet.answer(False, 'verified exact CS degree assessment')}
    for label in ["Do you have a degree in computer science or a related field?",
                  "Do you have a bachelor's degree or equivalent experience in computer science?"]:
        assert key_for_field({'ref': 'q', 'label': label, 'type': 'combobox'}, answers) is None


def test_employer_catalog_mapping_does_not_change_actual_degree_major():
    record = education(major='Mathematics and Computing')
    record['form_mappings'] = [{'major_option': 'Computer Science'}]
    assert completed_cs_degree_answer([record], as_of=date(2026, 10, 4))['value'] is False
