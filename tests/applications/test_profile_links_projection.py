"""Precise verified URL projections retain in both native text control types."""
import copy

import pytest

from jhb.applications import booklet, known_answers, review_inventory
from jhb.applications.manual_runtime import application_scope, dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
URLS = ['https://github.com/synthetic', 'https://scholar.google.com/citations?user=SYNTHETIC&hl=en', 'https://synthetic.example/']


def facts():
    return {key: booklet.answer(value, 'synthetic verified chosen-role profile') for key, value in
            zip(('links.github', 'links.scholar', 'links.portfolio'), URLS)}


@pytest.mark.parametrize('kind,separator', [('text', ' '), ('textarea', '\n')])
def test_projection_changes_separators_only_and_preserves_source_urls(kind, separator):
    answers = facts(); original = copy.deepcopy(answers)
    field = {'ref': 'links', 'label': known_answers._LINKS, 'type': kind, 'required': False, 'options': []}
    key = known_answers.enrich(field, {}, answers)
    assert key_for_field(field, answers) == key
    assert answers[key]['value'] == separator.join(URLS)
    assert answers[key]['source']['projection'] == {'control_type': kind, 'separator': separator, 'urls': URLS}
    assert all(answers[k] == v for k, v in original.items())
    assert key_for_field({**field, 'type': 'textarea' if kind == 'text' else 'text'}, answers) is None


def test_unverified_or_duplicate_urls_are_not_invented_in_projection():
    answers = facts()
    answers['links.scholar']['status'] = 'needs_input'
    answers['links.portfolio']['value'] = URLS[0]
    field = {'ref': 'links', 'label': known_answers._LINKS, 'type': 'text', 'required': False, 'options': []}
    key = known_answers.enrich(field, {}, answers)
    assert answers[key]['value'] == URLS[0]
    assert answers[key]['source']['projection']['urls'] == URLS[:1]
    assert 'links.scholar' not in answers[key]['source']['records']


@pytest.mark.parametrize('kind', ['text', 'textarea'])
def test_actual_native_fill_and_fresh_audit_keep_exact_projected_urls_without_submission(kind):
    from playwright.sync_api import sync_playwright
    control = '<input id=links type=text>' if kind == 'text' else '<textarea id=links></textarea>'
    html = '<form class=ashby-application-form-container><div data-field-path=links><label for=links class=ashby-application-form-question-title>'+known_answers._LINKS+'</label>'+control+'</div><button type=submit>Submit application</button></form><script>window.submissions=0;document.querySelector("form").onsubmit=e=>{e.preventDefault();window.submissions++}</script>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(); page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
        page.goto(URL); session = page.context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
                   'wait': lambda seconds: page.wait_for_timeout(seconds*1000),
                   'click_at_xy': lambda x, y: page.mouse.click(x, y),
                   'list_tabs': lambda: [{'targetId': 'fixture', 'url': URL}],
                   'current_tab': lambda: {'targetId': 'fixture'}, 'switch_tab': lambda target: None}
        def call(operation, **values):return dispatch({'operation': operation, 'scope': application_scope(URL), **values}, helpers)
        try:
            call('open', url=URL)
            fields = call('observe')['fields']; field = fields[0]
            answers = facts(); key = known_answers.enrich(field, {}, answers); record = answers[key]
            assert call('fill', field=field, value=record['value'])['verified']
            assert page.locator('#links').input_value() == record['value']
            filled = [{'ref': field['ref'], 'question': field['label'], 'key': key, 'value': record['value'], 'source': record['source']}]
            packet = {'job': {'url': URL}, 'filled': filled,
                      **review_inventory.build(fields, filled, answers, key_for_field, complete=True)}
            result = _checks({'target_id': 'fixture', 'documents': {}}, helpers, packet,
                             {'application_url': URL, 'authorization_scope': 'one exact application explicitly approved in the local review portal'})
            assert result['retained'][0]['state']['value'] == record['value']
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally:browser.close()


@pytest.mark.parametrize('choices,expected', [([], None), (['California', 'New York'], 'California'),
    (['New York'], None), (['California', 'California'], None)])
def test_state_combobox_projection_keeps_known_fact_and_requires_unique_catalog_match(choices, expected):
    field = {'ref': 'state', 'label': 'State/Country of Residence', 'type': 'combobox', 'required': True,
             'options': [{'label': v, 'value': v} for v in choices]}
    answers = {'identity.state': booklet.answer('California', 'synthetic verified contact state')}
    key = known_answers.enrich(field, {}, answers)
    assert key_for_field(field, answers) == key
    if expected:
        assert answers[key]['value'] == {'query': 'California', 'choice': expected}
        assert answers[key]['source']['projection']['native_catalog_verification_required'] is True
    else: assert key is None
    assert answers['identity.state']['value'] == 'California'


@pytest.mark.parametrize('available', [True, False])
@pytest.mark.parametrize('existing', ['', 'California', 'California, United States'])
def test_actual_state_autocomplete_never_accepts_typed_only_value(available, existing):
    from playwright.sync_api import sync_playwright
    html = '''<form class=ashby-application-form-container><div data-field-path=state>
<label class=ashby-application-form-question-title for=state>State/Country of Residence</label>
<input role=combobox aria-expanded=false oninput="window.inputEvents++;menu(this)" onkeydown="if(event.key==='ArrowDown')menu(this)"><div id=options role=listbox></div>
</div><button type=submit>Submit application</button></form><script>
window.inputEvents=0;window.commits=0;window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function menu(e){e.setAttribute('aria-controls','options');e.setAttribute('aria-expanded','true');
if(AVAILABLE)document.querySelector('#options').innerHTML='<div role="option" onclick="choose()">California, United States</div><div role="option">California City, California, United States</div><div role="option">California, Pennsylvania, United States</div>'}
function choose(){window.commits++;document.querySelector('input[role=combobox]').value='California, United States';document.querySelector('input[role=combobox]').setAttribute('aria-expanded','false');document.querySelector('#options').innerHTML=''}
</script>'''.replace('AVAILABLE', 'true' if available else 'false')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(); page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html)); page.goto(URL)
        session = page.context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
                   'wait': lambda seconds: page.wait_for_timeout(seconds*1000), 'click_at_xy': lambda x, y: page.mouse.click(x, y),
                   'list_tabs': lambda: [{'targetId': 'fixture', 'url': URL}], 'current_tab': lambda: {'targetId': 'fixture'},
                   'switch_tab': lambda target: None}
        def call(operation, **values):return dispatch({'operation': operation, 'scope': application_scope(URL), **values}, helpers)
        try:
            call('open', url=URL);field = call('observe')['fields'][0]
            if existing: page.locator('input[role=combobox]').fill(existing)
            page.evaluate('window.inputEvents=0')
            catalog = call('describe', field=field, query='California')
            assert page.locator('input[role=combobox]').input_value() == existing
            if existing: assert page.evaluate('window.inputEvents') == 0  # No clearing or retyping committed values.
            assert page.evaluate('window.commits') == 0
            field['options'] = [{'label': v, 'value': v} for v in catalog['choices']]
            answers = {'identity.state': booklet.answer('California', 'synthetic verified contact state'),
                       'identity.country': booklet.answer('United States', 'synthetic verified contact country')}
            key = known_answers.enrich(field, {}, answers)
            if available:
                assert call('fill', field=field, value=answers[key]['value'])['verified']
                assert page.evaluate('window.commits') == 1
                filled = [{'ref': field['ref'], 'question': field['label'], 'key': key,
                           'value': answers[key]['value'], 'source': answers[key]['source']}]
                packet = {'job': {'url': URL}, 'filled': filled,
                          **review_inventory.build([field], filled, answers, key_for_field, complete=True)}
                result = _checks({'target_id': 'fixture', 'documents': {}}, helpers, packet,
                                 {'application_url': URL, 'authorization_scope': 'one exact application explicitly approved in the local review portal'})
                assert result['retained'][0]['state']['selected'] == 'California, United States'
            else:
                assert key is None
                with pytest.raises(ValueError, match='Autocomplete choice is absent'):
                    call('fill', field=field, value={'query': 'California', 'choice': 'California, United States'})
                assert page.evaluate('window.commits') == 0  # Typed California alone is not a selected answer.
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally: browser.close()


@pytest.mark.parametrize('message', ['Autocomplete choice is absent', 'Autocomplete choice is ambiguous',
                                    'Autocomplete did not retain the committed choice'])
def test_catalog_absence_is_retryable_mechanics_instead_of_new_candidate_fact(tmp_path, monkeypatch, message):
    import json
    from types import SimpleNamespace
    from jhb.applications import cli_browser
    monkeypatch.setattr(cli_browser, 'ROOT', tmp_path);monkeypatch.setenv('BU_CDP_URL', 'http://127.0.0.1:12345')
    client = cli_browser.BrowserUseCLI()
    monkeypatch.setattr(client, '_run', lambda *a: SimpleNamespace(returncode=0, stderr='', stdout=cli_browser.MARKER+json.dumps({'error': message})))
    with pytest.raises(cli_browser.BrowserOperationError) as failure: client.call('fill', field={'ref': 'state'}, value={'query': 'California', 'choice': 'California, United States'})
    assert failure.value.retryable is True


def test_controller_queries_only_verified_residence_and_exposes_actual_owned_catalog():
    import asyncio
    from jhb.applications.manual_ats import ManualATSCLI
    field = {"ref": "state", "label": "State/Country of Residence", "type": "combobox", "required": True, "options": []}
    class CLI(ManualATSCLI):
        def __init__(self):super().__init__(URL);self.calls=[]
        async def invoke(self, operation, **values):
            self.calls.append((operation, copy.deepcopy(values)))
            return {"fields": [dict(field)], "buttons": []} if operation=="observe" else {"choices": ["California, United States", "California, Maryland, United States"], "truncated": False}
    cli=CLI()
    async def run():
        await cli.ensure_profile({"identity.state": booklet.answer("California", "synthetic approved state")})
        return await cli.observe()
    snapshot=asyncio.run(run())
    assert cli.calls[1]==("describe", {"field": field, "query": "California"})
    assert snapshot["fields"][0]["options"][0]["label"]=="California, United States"
    answers={"identity.state":booklet.answer("California", "synthetic approved state"), "identity.country":booklet.answer("United States", "synthetic approved country")}
    key=known_answers.enrich(snapshot["fields"][0], {}, answers)
    assert answers[key]["value"]=={"query":"California", "choice":"California, United States"}
    # Missing initial catalog is a bounded mechanism failure, not a factual gap.
    async def unavailable(operation, **values):return {"fields":[dict(field)], "buttons":[]} if operation=="observe" else {"choices":[], "truncated":False}
    cli.invoke=unavailable
    from jhb.applications.cli_browser import BrowserOperationError
    with pytest.raises(BrowserOperationError, match="catalog is unavailable"):asyncio.run(cli.observe())


@pytest.mark.parametrize("country,status,expected", [("United States","verified",True), ("Canada","verified",False), ("United States","needs_input",False)])
def test_country_qualified_state_choice_requires_verified_matching_contact_country(country,status,expected):
    field={"ref":"state", "label":"State/Country of Residence", "type":"combobox", "required":True,
           "options":[{"label":"California, United States", "value":"catalog-us"},
                      {"label":"California City, California, United States", "value":"different-city"},
                      {"label":"California, Pennsylvania, United States", "value":"different-state"}]}
    answers={"identity.state":booklet.answer("California","synthetic contact state"),
             "identity.country":booklet.answer(country,"synthetic contact country",status)}
    key=known_answers.enrich(field, {}, answers)
    assert (key is not None) is expected
    if key: assert answers[key]["value"]["choice"]=="California, United States"
