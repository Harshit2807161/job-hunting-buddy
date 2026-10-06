"""Synthetic graduate form: sourced facts, owned catalogs, retained audit binding."""
import asyncio
import copy
import hashlib
import json

import pytest

from jhb.applications import booklet, known_answers, native_question_context, planner, question_routing, review_inventory
from jhb.applications.cli_browser import BrowserOperationError

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
UNIVERSITY = 'Please select your current or most recent university.'
NOTE = 'If your university is not listed, please select "other."'
CASES = [
    ('What is your current/most recent employer?', 'text', [], 'Example Cloud'),
    ('Please list the city and state/province that you are located in today.', 'text', [], 'Example City, CA'),
    ('If you are not located in one of the above hubs, are you willing to relocate?', 'radio', ['Yes', 'No'], 'Yes'),
    ('Please select your graduation month', 'combobox', ['May', 'December'], 'December'),
    ('Please select your graduation year', 'combobox', ['2029', '2030'], '2030'),
    (UNIVERSITY, 'combobox', ['Example University', 'Other'], 'Example University'),
    ('Have you had a previous work experience in software engineering?', 'radio', ['Yes', 'No'], 'Yes'),
]


def field(label, kind='radio', choices=()):
    return {'ref': 'ashby:synthetic:'+hashlib.sha256(label.encode()).hexdigest()[:12],
            'label': label, 'type': kind, 'required': True,
            'options': [{'label': c, 'value': c} for c in choices],
            'description': NOTE if label == UNIVERSITY else '', 'description_truncated': False}


def profile():
    record = {'school': 'Example University', 'degree': 'Master of Science', 'major': 'Computer Science',
              'start_date': '2025-09', 'end_date': '2030-12', 'expected': True,
              'status': 'verified', 'source': 'synthetic original education record'}
    resume_experience = ('Earlier Labs  May 2023 – Aug 2023\nResearch Intern  Example City, CA\n• Built synthetic systems.\n'
        'Example Cloud  Jun 2024 – Sep 2024\nSoftware Development Engineer Intern  Example City, CA\n• Built synthetic software.')
    return {'schema_version': 1, 'answers': {k: booklet.answer(v, 'synthetic explicit profile') for k, v in {
        'preferences.application_city': 'Example City, CA', 'preferences.relocation': True,
        'education.expected_graduation_date': '2030-12-14', 'eligibility.authorized_us': True,
        'eligibility.ead_issued': False}.items()}, 'education_records': [record],
        'roles': {'sde': {'role.experience': booklet.answer(resume_experience, 'synthetic verified resume')}, 'ml': {}},
        'workflow_preferences': {'relocation': 'Open to relocating anywhere'}}


def facts():
    return booklet.for_role(profile(), 'sde')


@pytest.mark.parametrize('label,kind,choices,expected', CASES)
def test_seven_exact_questions_bind_verified_facts_and_retained_inventory(label, kind, choices, expected):
    values, question = facts(), field(label, kind, choices)
    key = known_answers.enrich(question, {}, values)
    assert key and values[key]['value'] == expected
    assert planner.key_for_field(question, values) == key
    plan = planner.validate_plan(planner.deterministic_plan({'fields': [question], 'buttons': []}, values),
                                 {'fields': [question], 'buttons': []}, values)
    assert plan['bindings'] == [{'ref': question['ref'], 'answer_key': key}]
    row = {'ref': question['ref'], 'question': label, 'key': key, 'value': expected, 'source': values[key]['source']}
    packet = review_inventory.build([question], [row], values, planner.key_for_field, complete=True)
    assert packet['review_inventory']['fields'][0]['status'] == 'answered'
    original_key = next(iter(values[key]['source']['records']))
    values[original_key] = {**values[original_key], 'status': 'needs_input'}
    assert planner.key_for_field(question, values) is None
    assert review_inventory.build([question], [row], values, planner.key_for_field, complete=True)['review_inventory']['fields'][0]['status'] == 'blank'


@pytest.mark.parametrize('label,kind,choices,expected', CASES)
@pytest.mark.parametrize('change', ['instruction', 'truncated', 'label', 'type'])
def test_changed_question_scope_does_not_reuse_profile_projection(label, kind, choices, expected, change):
    values, question = facts(), field(label, kind, choices)
    assert known_answers.enrich(question, {}, values)
    if change == 'instruction': question['description'] = 'Report only full-time employment in this country.'
    if change == 'truncated': question['description_truncated'] = True
    if change == 'label': question['label'] += ' Can you start immediately?'
    if change == 'type': question['type'] = 'textarea'
    assert known_answers.enrich(question, {}, values) is None
    assert planner.key_for_field(question, values) is None


@pytest.mark.parametrize('change', ['ref', 'required', 'choices'])
def test_projection_reuse_requires_same_complete_observed_question(change):
    values, question = facts(), field('Please select your graduation year', 'combobox', ['2030'])
    assert known_answers.enrich(question, {}, values)
    if change == 'ref': question['ref'] += ':different'
    if change == 'required': question['required'] = False
    if change == 'choices': question['options'].append({'label': 'Other'})
    assert planner.key_for_field(question, values) is None


def test_three_known_empty_ashby_catalogs_are_native_work_then_bind_actual_observed_choices():
    values = facts()
    questions = [field(label, kind) for label, kind, _, _ in CASES if kind == 'combobox']
    snapshot = {'url': URL, 'fields': questions, 'buttons': []}
    for question in questions:
        assert known_answers.enrich(question, {}, values) is None
        assert question_routing.field_route(question, values) == question_routing.KNOWN
    catalogs = {label: choices for label, _, choices, _ in CASES}
    calls = []
    async def describe(question):
        calls.append(question['label'])
        return {'type': 'combobox', 'choices': catalogs[question['label']], 'truncated': False}
    asyncio.run(native_question_context.enrich_async(snapshot, {'url': URL}, values, describe))
    assert calls == [q['label'] for q in questions]
    rows = []
    for question in questions:
        assert question['native_question_catalog']['source'] == 'owned_native_dropdown'
        key = known_answers.enrich(question, {}, values)
        assert key
        rows.append({'ref': question['ref'], 'question': question['label'], 'key': key,
                     'value': values[key]['value'], 'source': values[key]['source']})
    audit = review_inventory.build(questions, rows, values, planner.key_for_field, complete=True)
    assert [f['status'] for f in audit['review_inventory']['fields']] == ['answered']*3
    # Closed controls must be inspected again; old options are never projected.
    for question in questions: question['options'] = []
    assert all(planner.key_for_field(q, values) is None for q in questions)
    asyncio.run(native_question_context.enrich_async(snapshot, {'url': URL}, values, describe))
    assert all(planner.key_for_field(q, values) for q in questions)


@pytest.mark.parametrize('problem', ['empty', 'truncated', 'duplicate', 'wrong_type', 'wrong_job'])
def test_catalog_failures_never_supply_guessed_university_or_other(problem):
    values, question = facts(), field(UNIVERSITY, 'combobox')
    snapshot = {'url': URL, 'fields': [question]}
    descriptor = {'type': 'combobox', 'choices': ['Example University', 'Other'], 'truncated': False}
    if problem == 'empty': descriptor['choices'] = []
    if problem == 'truncated': descriptor['truncated'] = True
    if problem == 'duplicate': descriptor['choices'] += ['Example University']
    if problem == 'wrong_type': descriptor['type'] = 'select'
    if problem == 'wrong_job': snapshot['url'] = URL.replace('11111111', '99999999')
    with pytest.raises(BrowserOperationError):
        native_question_context.enrich_sync(snapshot, {'url': URL}, values, lambda f: descriptor)
    assert known_answers.enrich(question, {}, values) is None


@pytest.mark.parametrize('choices', [['Other'], ['Unrelated University', 'Other'], ['Example University', 'Example University']])
def test_university_requires_exact_unique_native_match(choices):
    assert known_answers.enrich(field(UNIVERSITY, 'combobox', choices), {}, facts()) is None


@pytest.mark.parametrize('change', ['different_degree_end', 'unverified_degree', 'ambiguous_degree', 'year_only'])
def test_graduation_month_requires_same_unique_current_degree_and_explicit_month(change):
    values = facts()
    if change == 'different_degree_end': values['education.expected_graduation_date']['value'] = '2029-12-14'
    if change == 'unverified_degree': values['standing.current_education_school']['source']['original_record']['status'] = 'needs_input'
    if change == 'ambiguous_degree': del values['standing.current_education_school']
    if change == 'year_only': values['education.expected_graduation_date']['value'] = '2030'
    question = field('Please select your graduation month', 'combobox', ['December'])
    assert known_answers.enrich(question, {}, values) is None
    assert not known_answers.needs_catalog(question, values)


@pytest.mark.parametrize('change', ['tie', 'two_current', 'missing_source', 'future_record', 'incomplete_record'])
def test_latest_employer_is_not_first_list_item_or_ambiguous_history(change):
    values = facts()
    if change == 'tie': values['experience.0.end_date']['value'] = '2024-09'
    if change == 'two_current':
        for index in (0, 1):
            values[f'experience.{index}.current']['value'] = True
            values[f'experience.{index}.end_date']['value'] = ''
    if change == 'missing_source': values['experience.1.company']['source'] = ''
    if change == 'future_record': values['experience.1.end_date']['value'] = '2099-09'
    if change == 'incomplete_record': del values['experience.1.title']
    assert known_answers.enrich(field(CASES[0][0], 'text'), {}, values) is None


def test_previous_software_work_does_not_infer_no_from_missing_experience_or_claim_fulltime():
    values = facts()
    values['experience.1.title']['value'] = 'Research Intern'
    assert known_answers.enrich(field(CASES[-1][0], choices=['Yes', 'No']), {}, values) is None
    question = field('Have you had previous full-time work experience in software engineering?', choices=['Yes', 'No'])
    assert known_answers.enrich(question, {}, facts()) is None


@pytest.mark.parametrize('label,choices', [
    ('Are you currently authorized to work in the US?', ['Yes, no restrictions', 'Yes, with time limitations', 'No, I require work authorization']),
    ('Approximately how long is your current work authorization valid?', ['Less than 1 year', '1–2 years', '2+ years']),
    ('Do you have a STEM degree that would make you eligible for extended work authorization?', ['Yes', 'No', 'Unsure']),
    ('Please rank your preference in location', []),
])
def test_current_authorization_stem_and_ranking_remain_unresolved(label, choices):
    values, question = facts(), field(label, 'radio' if choices else 'textarea', choices)
    assert known_answers.enrich(question, {}, values) is None
    assert planner.key_for_field(question, values) is None
    assert not known_answers.needs_catalog(question, values)


def test_ashby_new_catalog_path_does_not_probe_other_question_families():
    question = field('What is your current GPA', 'combobox')
    snapshot = {'url': URL, 'fields': [question]}
    values = facts(); values['standing.current_education_gpa'] = booklet.answer('3.9/4.0', 'synthetic source')
    def no_probe(_): raise AssertionError('Unallowlisted catalog must not be opened')
    native_question_context.enrich_sync(snapshot, {'url': URL}, values, no_probe)


def test_derivations_leave_original_source_facts_unchanged():
    values = facts(); before = copy.deepcopy(values)
    for label, kind, choices, _ in CASES:
        known_answers.enrich(field(label, kind, choices), {}, values)
    assert {k: values[k] for k in before} == before


@pytest.mark.parametrize('change', [None, 'base_fact', 'forged_source', 'retained_value', 'native_options'])
def test_submission_audit_uses_review_bound_original_facts_and_actual_catalogs(tmp_path, monkeypatch, change):
    from jhb import config
    from jhb.applications import approvals, boards, submission_runtime
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    book, values = profile(), facts()
    job = {'url': URL, 'dedupe_hash': boards.application_hash(URL), 'company': 'Example', 'title': 'Software Engineer', 'selected_role': 'sde'}
    questions = [field(label, kind, choices) for label, kind, choices, _ in CASES]
    rows = []
    for question in questions:
        key = known_answers.enrich(question, job, values)
        rows.append({'ref': question['ref'], 'question': question['label'], 'key': key,
                     'value': values[key]['value'], 'source': values[key]['source']})
    packet = {'job': job, 'selected_role': 'sde', 'filled': rows,
              **review_inventory.build(questions, rows, values, planner.key_for_field, complete=True)}
    if change == 'forged_source':
        packet['filled'][0]['source']['records']['experience.1.company']['value'] = 'Invented Employer'
    private = tmp_path/'private'; private.mkdir()
    pp, bp = private/'packet.json', private/'book.json'
    booklet.write_private(pp, packet); booklet.write_private(bp, book)
    binding = {'packet_path': str(pp), 'packet_sha256': hashlib.sha256(pp.read_bytes()).hexdigest(),
               'book_path': str(bp), 'selected_role': 'sde', 'facts_sha256': approvals._facts(book, job, 'sde')}
    if change == 'base_fact':
        book['answers']['preferences.relocation']['value'] = False
        booklet.write_private(bp, book)
    attempt = {'application_url': URL, 'job_hash': job['dedupe_hash'], 'packet_path': str(pp),
               'packet_sha256': binding['packet_sha256'], 'review_binding': binding}
    catalogs = {q['ref']: copy.deepcopy(q['options']) for q in questions}
    snapshot = {'url': URL, 'fields': copy.deepcopy(questions), 'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
    for q in snapshot['fields']:
        if q['type'] == 'combobox': q['options'] = []
    calls = []
    def dispatch(request, helpers, url):
        calls.append(request['operation'])
        if request['operation'] == 'observe': return copy.deepcopy(snapshot)
        assert request['operation'] == 'describe'
        options = [c['label'] for c in catalogs[request['field']['ref']]]
        if change == 'native_options': options.append('New choice')
        return {'type': 'combobox', 'choices': options, 'truncated': False}
    def state(helpers, question, board):
        value = next(row['value'] for row in rows if row['ref'] == question['ref'])
        if change == 'retained_value' and question['type'] == 'text': value = 'Manual change'
        return {'value': value, 'selected': [value] if question['type'] == 'radio' else value, 'invalid': False}
    def js(expression):
        if expression == 'location.href': return URL
        if expression == 'window.__jhbGuard===true': return True
        return False
    monkeypatch.setattr(submission_runtime, '_board_dispatch', dispatch)
    monkeypatch.setattr(submission_runtime, '_control_state', state)
    monkeypatch.setattr(submission_runtime, '_native_form_submit', lambda *args: True)
    helpers = {'current_tab': lambda: {'targetId': 'synthetic'}, 'js': js}
    request = {'target_id': 'synthetic', 'documents': {}}
    if change in {'base_fact', 'forged_source'}:
        with pytest.raises(ValueError, match='facts'):
            submission_runtime._checks(request, helpers, packet, attempt)
        assert calls == ['observe']
    else:
        result = submission_runtime._checks(request, helpers, packet, attempt)
        if change is None:
            assert not result.get('state') and result['double_check_count'] == 7
        else:
            assert result['state'] == 'waiting_review' and result['click_started'] is False
        assert calls == ['observe', 'describe', 'describe', 'describe']
    assert json.loads(pp.read_text()) == packet  # Audits never rewrite retained evidence.
