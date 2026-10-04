from copy import deepcopy
import os

import pytest

from jhb import config
from jhb.applications import booklet
from jhb.applications.cli_runtime import dispatch
from jhb.applications.planner import deterministic_plan, key_for_field, validate_plan


def book():
    return {'answers': {}, 'roles': {'sde': {}, 'ml': {}}, 'education_records': [
        {'school': 'Synthetic Graduate University', 'degree': 'Master of Science', 'major': 'Computer Science',
         'start_date': '2025-09', 'end_date': '2026-12-14', 'expected': True, 'status': 'verified',
         'source': 'synthetic verified original graduate record'},
        {'school': 'Synthetic Undergraduate University', 'degree': 'Bachelor of Science', 'major': 'Mathematics',
         'start_date': '2021-07', 'end_date': '2025-05', 'expected': False, 'status': 'verified',
         'source': 'synthetic verified original undergraduate record'},
    ]}


def snapshot():
    return {'fields': [{'ref': f'{column}-year--{index}', 'label': f'{column.title()} date year',
                        'type': 'number', 'required': False} for index in (0, 1) for column in ('start', 'end')],
            'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}


@pytest.mark.parametrize('role', ['sde', 'ml'])
def test_indexed_years_keep_distinct_records_and_full_original_provenance(role):
    original = book();before = deepcopy(original)
    answers = booklet.for_role(original, role)
    plan = validate_plan(deterministic_plan(snapshot(), answers), snapshot(), answers)
    assert plan['bindings'] == [
        {'ref': 'start-year--0', 'answer_key': 'education.0.start_year'},
        {'ref': 'end-year--0', 'answer_key': 'education.0.end_year'},
        {'ref': 'start-year--1', 'answer_key': 'education.1.start_year'},
        {'ref': 'end-year--1', 'answer_key': 'education.1.end_year'},
    ]
    assert [answers[b['answer_key']]['value'] for b in plan['bindings']] == ['2025', '2026', '2021', '2025']
    assert plan['next_ref'] is None
    assert original == before
    assert answers['education.0.end_date']['value'] == '2026-12-14'
    provenance = answers['education.0.end_year']['source']
    assert provenance['original_date'] == '2026-12-14'
    assert provenance['derived_from'] == 'education.0.end_date'
    assert provenance['original_source'] == original['education_records'][0]['source']
    assert provenance['expected'] is True
    assert answers['education.1.end_year']['source']['expected'] is False
    assert 'education.end_year' not in answers


@pytest.mark.parametrize('date,expected', [('2024', '2024'), ('2024-02', '2024'), ('2024-02-29', '2024')])
def test_explicit_year_year_month_and_valid_full_dates(date, expected):
    source = book();source['education_records'][0]['end_date'] = date
    assert booklet.for_role(source, 'sde')['education.0.end_year']['value'] == expected


@pytest.mark.parametrize('malformed', [None, 2026, True, '', '2026/12', '2026-13', '2026-00',
                                      '2026-2', '2026-12-32', '2025-02-29', '0000', '2026-12 expected',
                                      '2026-12-14T00:00:00', '٢٠٢٦', ' 2026', '2026 '])
def test_invalid_original_date_is_not_a_year_binding(malformed):
    source = book();source['education_records'][0]['end_date'] = malformed
    answers = booklet.for_role(source, 'sde')
    assert 'education.0.end_year' not in answers
    field = {'ref': 'end-year--0', 'label': 'End date year', 'type': 'number'}
    assert key_for_field(field, answers) is None
    assert answers['education.1.end_year']['value'] == '2025'


def test_unverified_education_cannot_supply_derived_years():
    source = book();source['education_records'][0]['status'] = 'needs_input'
    answers = booklet.for_role(source, 'sde')
    assert 'education.0.start_year' not in answers and 'education.0.end_year' not in answers
    assert key_for_field(snapshot()['fields'][0], answers) is None


@pytest.mark.parametrize('field', [
    {'ref': 'start-year--0', 'label': 'Salary year', 'type': 'number'},
    {'ref': 'end-year--0', 'label': 'End date year', 'type': 'text'},
    {'ref': 'start-year--2', 'label': 'Start date year', 'type': 'number'},
    {'ref': 'end_year--0', 'label': 'End date year', 'type': 'number'},
])
def test_year_bindings_require_exact_observed_ref_label_type_and_record(field):
    assert key_for_field(field, booklet.for_role(book(), 'sde')) is None


def test_cli_runtime_retains_all_four_derived_number_years_without_submission():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(config.ROOT / '.local-browsers'))
    from playwright.sync_api import sync_playwright
    html = '<!doctype html><html lang="en"><title>Synthetic education years</title><form id="application">'
    for index in (0, 1):
        for column in ('start', 'end'):
            ref = f'{column}-year--{index}'
            html += f'<label for="{ref}">{column.title()} date year</label><input id="{ref}" type="number">'
    html += '''<button type="submit">Submit application</button></form><script>
    window.submissions=0;document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
    </script></html>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page();requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=html)
        page.route('**/*', serve)
        url = 'https://job-boards.greenhouse.io/synthetic-years/jobs/1234';page.goto(url)
        session = page.context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
                   'wait': lambda seconds: page.wait_for_timeout(seconds * 1000), 'click_at_xy': page.mouse.click,
                   'list_tabs': lambda: [{'url': url, 'targetId': 'fixture'}], 'switch_tab': lambda target: None,
                   'current_tab': lambda: {'targetId': 'fixture', 'url': url}}
        try:
            dispatch({'operation': 'open', 'url': url}, helpers)
            observed = dispatch({'operation': 'observe'}, helpers)
            answers = booklet.for_role(book(), 'sde')
            plan = validate_plan(deterministic_plan(observed, answers), observed, answers)
            fields = {f['ref']: f for f in observed['fields']}
            assert len(plan['bindings']) == 4
            for binding in plan['bindings']:
                value = answers[binding['answer_key']]['value']
                assert dispatch({'operation': 'fill', 'field': fields[binding['ref']], 'value': value}, helpers)['verified']
                assert page.locator('[id="' + binding['ref'] + '"]').input_value() == value
            assert [page.locator('[id="' + ref + '"]').input_value() for ref in fields] == ['2025', '2026', '2021', '2025']
            page.get_by_role('button', name='Submit application').click()
            page.evaluate("document.getElementById('application').submit()")
            assert page.evaluate('window.submissions') == 0
            assert requests == [url]
        finally:
            browser.close()
