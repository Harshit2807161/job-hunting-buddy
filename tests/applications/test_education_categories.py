"""Employer-specific category proposals never rewrite original education."""
import copy
import hashlib
import json

import pytest

from jhb import config
from jhb.applications import boards, booklet, education_categories as categories, planner, review_inventory, submission_runtime, worker
from jhb.applications.questions import _scope
from tests.applications.test_reviewed_derived_catalogs import URL, bind

KEY = 'custom.catalog.synthetic.major'
CHOICE = 'Synthetic Computing Category'
FIELD = {'ref': 'discipline--0', 'label': 'Discipline*', 'type': 'combobox', 'required': True,
         'options': [], 'description': '', 'description_truncated': False}


def fixture():
    job = {'url': URL, 'dedupe_hash': boards.application_hash(URL), 'company': 'Synthetic', 'title': 'Engineer'}
    original = {'school': 'Synthetic University', 'degree': 'Master of Science', 'major': 'Computer Science',
                'start_date': '2025-09', 'end_date': '2026-12', 'expected': True, 'gpa': '3.9',
                'source': 'Synthetic verified resume', 'status': 'verified'}
    record = {'status': 'verified', 'value': CHOICE, 'question': FIELD['label'], 'field_ref': FIELD['ref'],
              'scope': _scope(job), 'job_hash': job['dedupe_hash'], 'proposed': True,
              'source': {'kind': 'employer_category_projection', 'method': 'exact_job_observed_education_category',
                 'candidate_confirmation': False, 'category_only': True, 'credential_rewritten': False,
                 'actual_value': original['major'], 'original_source': original['source'],
                 'original_education_record': copy.deepcopy(original),
                 'original_record_sha256': hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                 'field_ref': FIELD['ref'], 'field_descriptor': copy.deepcopy(FIELD),
                 'job_hash': job['dedupe_hash'], 'application_identity': list(boards.job_identity(URL)),
                 'observed_choice': CHOICE, 'assessment_sha256': 'a'*64, 'native_evidence_sha256': 'b'*64}}
    book = {'schema_version': 1, 'answers': {}, 'roles': {'sde': {}, 'ml': {}},
            'education_records': [original, {**original, 'school': 'Other Synthetic College', 'major': 'Mathematics'}],
            'custom_answers': {KEY: record}, 'workflow_preferences': {}}
    return job, book, record


def answers(job, book):
    return {**booklet.for_role(book, 'sde', job=job), **worker._scoped_custom_answers(book, job, _scope(job))}


def test_exact_job_proposal_binds_current_original_record_without_mutation():
    job, book, record = fixture(); before = copy.deepcopy(book)
    catalog = answers(job, book)
    assert planner.key_for_field(FIELD, catalog) == KEY
    assert catalog['education.0.major']['value'] == 'Computer Science'
    assert record['proposed'] and record['source']['candidate_confirmation'] is False
    assert book == before and categories.BASIS not in record
    assert planner.key_for_field(FIELD, {KEY: record}) is None


@pytest.mark.parametrize('change', ['major', 'school', 'degree', 'gpa', 'date', 'expected', 'source', 'unverified',
    'field_ref', 'question', 'description', 'truncation', 'type', 'required', 'source_row', 'source_job', 'source_identity',
    'record_job', 'record_scope', 'record_hash', 'missing_evidence', 'malformed_digest', 'fake_confirmation', 'not_proposed', 'credential_rewrite',
    'changed_value', 'missing_metadata', 'wrong_context_basis'])
def test_changed_facts_scope_source_or_native_context_cannot_reuse_category(change):
    job, book, record = fixture(); field = copy.deepcopy(FIELD)
    original = book['education_records'][0]
    if change in {'major', 'school', 'degree', 'gpa', 'source'}: original[change] = 'Changed'
    if change == 'date': original['end_date'] = '2028-12'
    if change == 'expected': original['expected'] = False
    if change == 'unverified': original['status'] = 'unknown'
    if change == 'field_ref': field['ref'] = 'discipline--1'
    if change == 'question': field['label'] = 'Exact transcript major*'
    if change == 'description': field['description'] = 'Choose your exact transcript CIP code.'
    if change == 'truncation': field['description_truncated'] = True
    if change == 'type': field['type'] = 'select'
    if change == 'required': field['required'] = False
    if change == 'source_row': record['source']['field_ref'] = 'discipline--1'
    if change == 'source_job': record['source']['job_hash'] = 'd'*64
    if change == 'source_identity': record['source']['application_identity'][1] = 'us'
    if change == 'record_job': record['job_hash'] = 'd'*64
    if change == 'record_scope': record['scope']['board'] = 'wrong-employer'
    if change == 'record_hash': record['source']['original_record_sha256'] = 'd'*64
    if change == 'missing_evidence': record['source'].pop('native_evidence_sha256')
    if change == 'malformed_digest': record['source']['assessment_sha256'] = 'not a digest'
    if change == 'fake_confirmation': record['source']['candidate_confirmation'] = True
    if change == 'not_proposed': record['proposed'] = False
    if change == 'credential_rewrite': record['source']['credential_rewritten'] = True
    if change == 'changed_value': record['value'] = 'Different category'
    if change == 'missing_metadata': record['source']['field_descriptor'].pop('description_truncated')
    if change == 'wrong_context_basis':
        original['major'] = 'Changed'
        record[categories.BASIS] = {'record': record['source']['original_education_record']}
    assert planner.key_for_field(field, answers(job, book)) != KEY


@pytest.mark.parametrize('labels,expected', [([CHOICE, 'Other'], True), ([CHOICE, CHOICE], False),
    ([CHOICE, 'Computer Science'], False), (['Other'], False)])
def test_populated_owned_catalog_requires_unique_category_and_prefers_exact_original(labels, expected):
    job, book, _ = fixture()
    field = {**FIELD, 'options': [{'label': label} for label in labels]}
    assert (planner.key_for_field(field, answers(job, book)) == KEY) is expected


def test_previously_populated_descriptor_does_not_lose_option_binding():
    job, book, record = fixture()
    options = [{'label': CHOICE}, {'label': 'Other'}]
    record['source']['field_descriptor']['options'] = options
    assert planner.key_for_field({**FIELD, 'options': options}, answers(job, book)) == KEY
    assert planner.key_for_field(FIELD, answers(job, book)) is None
    assert planner.key_for_field({**FIELD, 'options': options+[{'label': 'Changed catalog'}]}, answers(job, book)) is None


def test_changed_exact_job_does_not_inherit_category_scope_or_saved_basis():
    job, book, record = fixture()
    record[categories.BASIS] = categories.scoped(record, book, job)[categories.BASIS]
    changed = {**job, 'url': URL.replace('1234', '9999')}
    changed['dedupe_hash'] = boards.application_hash(changed['url'])
    assert categories.BASIS not in categories.scoped(record, book, changed)
    assert planner.key_for_field(FIELD, answers(changed, book)) != KEY


@pytest.mark.parametrize('mode', ['delegated', 'portal'])
@pytest.mark.parametrize('change', [None, 'original_fact', 'forged_original', 'description', 'row', 'retained', 'revoked', 'no_binding'])
def test_closed_native_combobox_final_audit_rechecks_review_bound_original_facts(tmp_path, monkeypatch, mode, change):
    from playwright.sync_api import sync_playwright
    from jhb.applications.cli_runtime import dispatch
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    job, profile, saved = fixture()
    html = '''<form id=application><div class=field-wrapper><label for="discipline--0">Discipline*</label>
    <div class=select__value-container><span class=select__single-value>'''+CHOICE+'''</span>
    <input id="discipline--0" role=combobox aria-required=true aria-expanded=false></div></div>
    <button type=submit>Submit application</button></form><script>window.submissions=0;
    document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++}</script>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(); page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html)); page.goto(URL)
        session = page.context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
            'wait': lambda seconds: page.wait_for_timeout(seconds*1000), 'click_at_xy': lambda x, y: page.mouse.click(x, y),
            'list_tabs': lambda: [{'targetId': 'fixture', 'url': URL}], 'current_tab': lambda: {'targetId': 'fixture'}, 'switch_tab': lambda target: None}
        try:
            dispatch({'operation': 'open', 'url': URL}, helpers)
            fields = dispatch({'operation': 'observe'}, helpers)['fields']
            assert len(fields) == 1 and fields[0]['options'] == []
            catalog = answers(job, profile); assert planner.key_for_field(fields[0], catalog) == KEY
            row = {'ref': FIELD['ref'], 'question': FIELD['label']+' (education record 1)', 'key': KEY,
                   'value': saved['value'], 'source': copy.deepcopy(saved['source']), 'proposed': True}
            if change == 'forged_original': row['source']['original_education_record']['major'] = 'Changed'
            packet = {'job': job, 'selected_role': 'sde', 'filled': [row],
                      **review_inventory.build(fields, [row], catalog, planner.key_for_field, complete=True)}
            request, attempt, binding = bind(tmp_path, packet, profile, mode)
            before = (tmp_path/'private/packet.json').read_bytes()
            if change == 'original_fact':
                profile['education_records'][0]['major'] = 'Changed'; booklet.write_private(tmp_path/'private/book.json', profile)
            if change == 'revoked':
                profile['custom_answers'] = {}; booklet.write_private(tmp_path/'private/book.json', profile)
            if change == 'description':
                page.evaluate("document.getElementById('discipline--0').setAttribute('aria-describedby','hint');const p=document.createElement('p');p.id='hint';p.className='question-description';p.textContent='Enter exact transcript CIP code.';document.querySelector('.field-wrapper').appendChild(p)")
            if change == 'row': page.evaluate("document.getElementById('discipline--0').id='discipline--1'")
            if change == 'retained': page.evaluate("document.querySelector('.select__single-value').textContent='Other'")
            if change == 'no_binding':
                attempt.pop('review_binding', None); request.pop('authorization_path', None)
            if change in {'original_fact', 'revoked', 'forged_original'}:
                with pytest.raises(ValueError, match='facts'):
                    submission_runtime._checks(request, helpers, packet, attempt)
            else:
                result = submission_runtime._checks(request, helpers, packet, attempt)
                if change is None:
                    assert not result.get('state') and result['double_check_count'] == 1
                    assert result['fields'][0]['answer_key'] == KEY
                else:
                    assert result['state'] in {'waiting_review', 'waiting_input'} and result['click_started'] is False
            assert page.evaluate('window.submissions') == 0
            assert page.evaluate('window.__jhbGuard') is True
            assert (tmp_path/'private/packet.json').read_bytes() == before
        finally:
            browser.close()
