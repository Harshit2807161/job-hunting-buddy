"""Timezone-regression fixtures for the observed Ashby React text datepicker."""
import json
import os

import pytest

from jhb.applications import booklet, calendar_dates, review_inventory
from jhb.applications.manual_runtime import application_scope, dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks
from jhb import config

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
LABEL = 'Earliest residency start date?'
HTML = '''<!doctype html><html><form id=application class=ashby-application-form-container>
<div data-field-path=availability><label class=ashby-application-form-question-title>Earliest residency start date?</label>
<div class=react-datepicker-wrapper><div class=react-datepicker__input-container>
<input type=text required class=ashby-application-form-input-date placeholder="Pick date..." onblur="commit(this)">
</div></div></div>
<div data-field-path=note><label class=ashby-application-form-question-title for=note>Note</label><input id=note></div>
<button type=submit>Submit application</button></form><script>
window.submissions=0;window.commits=0;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function commit(e){window.commits++;const parsed=new Date(e.value);
if(!isNaN(parsed)){e.value=parsed.toLocaleDateString('en-US',{month:'2-digit',day:'2-digit',year:'numeric'})}
if(window.wrongDay)e.value='12/13/2026';}
</script></html>'''


@pytest.fixture
def browser(monkeypatch):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(config.ROOT / '.local-browsers'))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(timezone_id='America/Los_Angeles')
        page = context.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
        page.goto(URL)
        session = context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
            'wait': lambda seconds: page.wait_for_timeout(seconds*1000),
            'click_at_xy': lambda x, y: page.mouse.click(x, y), 'list_tabs': lambda: [{'url': URL, 'targetId': 'fixture'}],
            'current_tab': lambda: {'targetId': 'fixture'}, 'switch_tab': lambda target: None}
        def call(operation, **payload):return dispatch({'operation': operation, 'scope': application_scope(URL), **payload}, helpers)
        call('open', url=URL)
        try:yield page, helpers, call
        finally:browser.close()


def test_native_datepicker_commits_same_calendar_day_without_utc_shift(browser):
    page, _, call = browser
    # Reproduce the live defect: generic ISO text appears correct until blur.
    element = page.locator('.ashby-application-form-input-date')
    element.fill('2026-12-14');element.press('Tab')
    assert element.input_value() == '12/13/2026'
    field = next(f for f in call('observe')['fields'] if f['label'] == LABEL)
    assert field['type'] == 'text' and field['widget'] == 'native' and field['calendar_format'] == 'MM/DD/YYYY'
    result = call('fill', field=field, value='2026-12-14')
    assert result == {'verified': True, 'calendar_format': 'MM/DD/YYYY', 'calendar_day': '2026-12-14'}
    assert element.input_value() == '12/14/2026'
    page.locator('#note').click()
    assert element.input_value() == '12/14/2026'
    assert page.evaluate('window.__jhbGuard') is True and page.evaluate('window.submissions') == 0


def test_wrong_calendar_day_is_a_mechanic_failure_and_not_verified(browser):
    page, _, call = browser
    page.evaluate('window.wrongDay=true')
    field = next(f for f in call('observe')['fields'] if f['label'] == LABEL)
    with pytest.raises(ValueError, match='Calendar input did not retain the approved day'):
        call('fill', field=field, value='2026-12-14')
    assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True


@pytest.mark.parametrize('remove', ['class', 'wrapper'])
def test_general_text_fields_are_not_calendar_normalized_without_observed_widget_proof(browser, remove):
    page, _, call = browser
    if remove == 'class':page.evaluate("document.querySelector('input').classList.remove('ashby-application-form-input-date')")
    else:page.evaluate("document.querySelector('.react-datepicker-wrapper').classList.remove('react-datepicker-wrapper')")
    field = next(f for f in call('observe')['fields'] if f['label'] == LABEL)
    assert 'calendar_format' not in field


@pytest.mark.parametrize('actual,expected,result', [('12/14/2026', '2026-12-14', True),
    ('12/14/2026', '12/14/2026', True), ('12/13/2026', '2026-12-14', False),
    ('2026-12-14', '2026-12-14', False), ('02/30/2026', '2026-02-28', False),
    ('12/14/2026', 'December 14', False)])
def test_retained_audit_compares_strict_committed_calendar_day(actual, expected, result):
    assert calendar_dates.retained_day(actual, expected) is result


def test_review_inventory_retains_calendar_format_and_original_approved_value():
    field = {'ref': 'q', 'label': LABEL, 'type': 'text', 'required': True, 'calendar_format': 'MM/DD/YYYY'}
    answers = {'preferences.start_date': booklet.answer('2026-12-14', 'synthetic explicit date')}
    filled = [{'ref': 'q', 'question': LABEL, 'key': 'preferences.start_date', **{'value': '2026-12-14', 'source': 'synthetic explicit date'}}]
    inventory = review_inventory.build([field], filled, answers, key_for_field, complete=True)['review_inventory']
    assert inventory['complete'] is True and inventory['fields'][0]['calendar_format'] == 'MM/DD/YYYY'
    assert filled[0]['value'] == '2026-12-14'


def test_calendar_mechanic_remains_retryable_through_cli_error_envelope(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from jhb.applications.cli_browser import MARKER, BrowserOperationError, BrowserUseCLI
    from jhb.applications import cli_browser
    monkeypatch.setattr(cli_browser, 'ROOT', tmp_path)
    monkeypatch.setenv('BU_CDP_URL', 'http://127.0.0.1:12345')
    client = BrowserUseCLI()
    monkeypatch.setattr(client, '_run', lambda *a: SimpleNamespace(returncode=0, stderr='',
        stdout=MARKER+json.dumps({'error': 'Calendar input did not retain the approved day'})))
    with pytest.raises(BrowserOperationError) as result:
        client.call('fill', field={'ref': 'q'}, value='2026-12-14')
    assert result.value.retryable is True


@pytest.mark.parametrize('change', [None, 'day', 'widget'])
def test_fresh_submission_audit_checks_original_day_and_observed_calendar_metadata_without_click(browser, change):
    page, helpers, call = browser
    fields = call('observe')['fields']
    field = next(f for f in fields if f['label'] == LABEL)
    values = {'preferences.start_date': booklet.answer('2026-12-14', 'synthetic explicit ISO date')}
    filled = [{'ref': field['ref'], 'question': LABEL, 'key': 'preferences.start_date',
               'value': '2026-12-14', 'source': 'synthetic explicit ISO date'}]
    packet = {'job': {'url': URL}, 'filled': filled,
              **review_inventory.build(fields, filled, values, key_for_field, complete=True)}
    call('fill', field=field, value='2026-12-14')
    if change == 'day':page.evaluate("document.querySelector('input').value='12/13/2026'")
    elif change == 'widget':page.evaluate("document.querySelector('input').classList.remove('ashby-application-form-input-date')")
    result = _checks({'target_id': 'fixture', 'documents': {}}, helpers, packet,
        {'application_url': URL, 'authorization_scope': 'one exact application explicitly approved in the local review portal'})
    if change is None:
        assert result['fields'][0]['calendar_format'] == 'MM/DD/YYYY'
        assert result['retained'][0]['state']['value'] == '12/14/2026'
    else:
        assert result['state'] == 'waiting_review' and result['click_started'] is False
    assert filled[0]['value'] == '2026-12-14'
    assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True


def test_preparation_reobserves_changed_calendar_metadata_before_ready_packet():
    import asyncio
    from jhb.applications.planner import deterministic_plan
    from jhb.applications.worker import prepare
    base = {'ref': 'calendar', 'label': LABEL, 'type': 'text', 'required': True}
    class SyntheticCLI:
        blocked_requests = 0
        calls = 0
        filled = []
        async def open(self, url):pass
        def allowed_url(self, url):return url == URL
        async def observe(self):
            self.calls += 1
            control = base if self.calls == 1 else {**base, 'calendar_format': 'MM/DD/YYYY'}
            return {'url': URL, 'fields': [control], 'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
        async def fill(self, field, value):self.filled.append(field.get('calendar_format'))
        async def click_next(self, button):raise AssertionError('No terminal click')
    cli = SyntheticCLI()
    result, _ = asyncio.run(prepare(None, {'url': URL},
        {'preferences.start_date': booklet.answer('2026-12-14', 'synthetic explicit day')},
        deterministic_plan, None, cli_actions=cli))
    assert result['state'] == 'waiting_review' and cli.filled == [None, 'MM/DD/YYYY']
    assert result['review_inventory']['fields'][0]['calendar_format'] == 'MM/DD/YYYY'
    assert any(event['event'] == 'fields_revealed' for event in result['events'])
