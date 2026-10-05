"""Synthetic Ashby contact catalog tests; never candidate Chrome."""

import pytest

from jhb.applications import booklet, known_answers, review_inventory
from jhb.applications.manual_runtime import application_scope, dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
@pytest.mark.parametrize('available', [True, False])
@pytest.mark.parametrize('existing', ['', 'San Diego, California, United States'])
@pytest.mark.parametrize('label,note', [('Location', 'City, State, and Country'),
    ('Home Location', 'The city you currently live in. Start typing and select from the list.')])
def test_native_contact_city_queries_restores_commits_and_fresh_audits(available, existing, label, note):
    from playwright.sync_api import sync_playwright
    html = '''<form class=ashby-application-form-container><div data-field-path=_systemfield_location>
<label class=ashby-application-form-question-title for=_systemfield_location>Location</label>
<div class=ashby-application-form-question-description>City, State, and Country</div>
<input role=combobox aria-expanded=false oninput="window.inputEvents++;menu(this)" onkeydown="if(event.key==='ArrowDown')menu(this)"><div id=options role=listbox></div>
</div><button type=submit>Submit application</button></form><script>
window.inputEvents=0;window.commits=0;window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function menu(e){e.setAttribute('aria-controls','options');e.setAttribute('aria-expanded','true');
if(AVAILABLE)document.querySelector('#options').innerHTML='<div role="option" onclick="choose()">San Diego, California, United States</div><div role="option">San Diego, Texas, United States</div><div role="option">San Diego, California, Canada</div>'}
function choose(){window.commits++;document.querySelector('input[role=combobox]').value='San Diego, California, United States';document.querySelector('input[role=combobox]').setAttribute('aria-expanded','false');document.querySelector('#options').innerHTML=''}
</script>'''.replace('AVAILABLE', 'true' if available else 'false').replace('>Location</label>', '>'+label+'</label>').replace('City, State, and Country', note)
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
            catalog = call('describe', field=field, query='San Diego')
            assert page.locator('input[role=combobox]').input_value() == existing
            if existing: assert page.evaluate('window.inputEvents') == 0  # No clearing or retyping committed values.
            assert page.evaluate('window.commits') == 0
            field['options'] = [{'label': v, 'value': v} for v in catalog['choices']]
            answers = {'preferences.application_city': booklet.answer('San Diego, CA', 'synthetic explicit application city'),
                       'identity.city': booklet.answer('La Jolla', 'synthetic mailing city'),
                       'identity.state': booklet.answer('CA', 'synthetic verified contact state'),
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
                assert result['retained'][0]['state']['selected'] == 'San Diego, California, United States'
                page.evaluate("""(()=>{const note=document.createElement('div');
                  note.className='ashby-application-form-question-description';
                  note.textContent='Choose your preferred office, not residence.';
                  document.querySelector('.ashby-application-form-question-description').remove();
                  document.querySelector('[data-field-path=_systemfield_location]').append(note)})()""")
                changed = call('observe')['fields'][0]
                assert known_answers.enrich(changed, {}, answers) is None
                assert key_for_field(changed, answers) is None
                with pytest.raises(ValueError, match='outside its approved scope'):
                    call('describe', field=field, query='San Diego')
                with pytest.raises(ValueError, match='Observed manual field has changed'):
                    call('fill', field=field, value=answers[key]['value'])
                assert page.evaluate('window.commits') == 1
            else:
                assert key is None
                assert key_for_field(field, answers) is None
                with pytest.raises(ValueError, match='Autocomplete choice is absent'):
                    call('fill', field=field, value={'query': 'San Diego', 'choice': 'San Diego, California, United States'})
                assert page.evaluate('window.commits') == 0  # Typed California alone is not a selected answer.
            assert answers['identity.city']['value'] == 'La Jolla'
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally: browser.close()



@pytest.mark.parametrize('choices', [[], ['San Diego'], ['San Diego, California, Canada'], ['San Diego, Texas, United States'], ['San Diego, California, United States']*2])
def test_contact_location_projection_rejects_partial_wrong_region_or_ambiguous_catalog(choices):
    answers = {'preferences.application_city': booklet.answer('San Diego, CA', 'explicit city'),
               'identity.city': booklet.answer('La Jolla', 'mailing'),
               'identity.state': booklet.answer('CA', 'state'), 'identity.country': booklet.answer('United States', 'country')}
    field = {'ref':'ashby:_systemfield_location:control:0','label':'Location','type':'combobox','required':True,
             'options':[{'label':v,'value':v} for v in choices]}
    assert known_answers.enrich(field, {}, answers) is None
    assert key_for_field(field, answers) is None
    assert answers['identity.city']['value']=='La Jolla'


def test_custom_employer_location_not_treated_as_standard_contact_field():
    assert not known_answers.contact_location({'ref':'custom-office','label':'Location','type':'combobox'})


@pytest.mark.parametrize('description,truncated,allowed', [
    ('', False, True), ('City, State, and Country', False, True),
    ('City, State, and Country', True, False),
    ('Choose your preferred office, not residence.', False, False),
    ('City, State, and Country where you hold citizenship', False, False),
])
def test_contact_instruction_allowlist_is_exact(description, truncated, allowed):
    field = {'ref': 'ashby:_systemfield_location:control:0', 'label': 'Location',
             'type': 'combobox', 'description': description, 'description_truncated': truncated}
    assert known_answers.plain_contact_location(field) is allowed


def test_unverified_or_contradictory_application_city_cannot_query_catalog():
    answers={'preferences.application_city':booklet.answer('San Diego, TX','explicit city'),
             'identity.state':booklet.answer('CA','state'),'identity.country':booklet.answer('United States','country')}
    assert known_answers.contact_location_basis(answers) is None
    answers['preferences.application_city']={'value':'San Diego, CA','source':'unconfirmed','status':'needs_input'}
    assert known_answers.contact_location_basis(answers) is None


def test_controller_uses_verified_application_city_and_rejects_missing_catalog():
    import asyncio
    from jhb.applications.manual_ats import ManualATSCLI
    from jhb.applications.cli_browser import BrowserOperationError
    field={'ref':'ashby:_systemfield_location:control:0','label':'Location','type':'combobox','required':True,'options':[]}
    class CLI(ManualATSCLI):
        def __init__(self): super().__init__(URL);self.calls=[];self.available=True
        async def invoke(self,operation,**payload):
            self.calls.append((operation,payload))
            if operation=='observe':return {'fields':[dict(field)],'buttons':[]}
            return {'choices':['San Diego, California, United States'] if self.available else [],'truncated':False}
    answers={'preferences.application_city':booklet.answer('San Diego, CA','explicit city'),
             'identity.city':booklet.answer('La Jolla','mailing'),
             'identity.state':booklet.answer('CA','state'),'identity.country':booklet.answer('United States','country')}
    cli=CLI()
    async def inspect():
        await cli.ensure_profile(answers)
        return await cli.observe()
    snapshot=asyncio.run(inspect())
    queries=[p['query'] for op,p in cli.calls if op=='describe']
    assert queries==['San Diego']
    key=known_answers.enrich(snapshot['fields'][0],{},answers)
    assert answers[key]['value']=={'query':'San Diego','choice':'San Diego, California, United States'}
    changed={**snapshot['fields'][0],'description':'Choose your preferred office, not residence.'}
    assert key_for_field(changed,answers) is None
    assert known_answers.enrich(changed,{},answers) is None
    cli.available=False
    with pytest.raises(BrowserOperationError,match='catalog is unavailable'):
        asyncio.run(cli.observe())
    assert answers['identity.city']['value']=='La Jolla'


def test_no_application_city_does_not_fall_back_to_mailing_location():
    answers={'identity.location':booklet.answer('La Jolla, CA, USA','mailing'),
             'identity.state':booklet.answer('CA','state'),'identity.country':booklet.answer('United States','country')}
    assert known_answers.contact_location_basis(answers) is None
    field={'ref':'ashby:_systemfield_location:control:0','label':'Location','type':'combobox','required':True,'options':[]}
    assert key_for_field(field,answers) is None


def test_custom_location_requires_exact_scoped_projection_and_retains_native_choice():
    """A custom Location is not silently reclassified as contact residence."""
    from playwright.sync_api import sync_playwright
    from jhb.applications import worker
    path = '11111111-aaaa-bbbb-cccc-222222222222'
    html = '''<form class=ashby-application-form-container>
<div data-field-path="CUSTOM"><label class=ashby-application-form-question-title>Location</label>
<input class=ashby-application-form-input-autocomplete role=combobox aria-expanded=false
 oninput="menu(this)" onkeydown="if(event.key==='Escape')closeMenu(this)">
<div id=options role=listbox></div></div><button type=submit>Submit application</button></form>
<script>window.commits=0;window.submissions=0;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function closeMenu(e){e.setAttribute('aria-expanded','false');document.querySelector('#options').innerHTML=''}
function menu(e){e.setAttribute('aria-controls','options');e.setAttribute('aria-expanded','true');
document.querySelector('#options').innerHTML=e.value?'<div role="option" onclick="choose()">San Diego, California, United States</div><div role="option">San Diego, Texas, United States</div>':''}
function choose(){window.commits++;const e=document.querySelector('input');e.value='San Diego, California, United States';closeMenu(e)}
</script>'''.replace('CUSTOM', path)
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
            call('open', url=URL); field = call('observe')['fields'][0]
            answers = {'preferences.application_city': booklet.answer('San Diego, CA', 'synthetic explicit application city'),
                       'identity.state': booklet.answer('CA', 'synthetic state'),
                       'identity.country': booklet.answer('United States', 'synthetic country')}
            assert not known_answers.contact_location(field)
            assert known_answers.enrich(field, {}, answers) is None
            with pytest.raises(ValueError, match='outside its approved scope'):
                call('describe', field=field, query='San Diego')
            with pytest.raises(ValueError, match='approved query and exact choice'):
                call('fill', field=field, value='San Diego, CA')
            scope = {'ats': 'ashby', 'region': 'global', 'board': 'example'}
            key = 'custom.scoped.native_contact'
            record = {**booklet.answer({'query': 'San Diego', 'choice': 'San Diego, California, United States'},
                                      {'method': 'synthetic_exact_observed_catalog_projection', 'basis': answers.copy()}),
                      'question': field['label'], 'field_ref': field['ref'], 'scope': scope, 'job_hash': 'one-exact-job'}
            book = {'custom_answers': {key: record}}
            assert worker._scoped_custom_answers(book, {'dedupe_hash': 'another-job'}, scope) == {}
            answers.update(worker._scoped_custom_answers(book, {'dedupe_hash': 'one-exact-job'}, scope))
            assert key_for_field(field, answers) == key
            assert call('fill', field=field, value=record['value'])['verified']
            assert page.evaluate('window.commits') == 1
            filled = [{'ref': field['ref'], 'question': field['label'], 'key': key,
                       'value': record['value'], 'source': record['source']}]
            packet = {'job': {'url': URL}, 'filled': filled,
                      **review_inventory.build([field], filled, answers, key_for_field, complete=True)}
            result = _checks({'target_id': 'fixture', 'documents': {}}, helpers, packet,
                             {'application_url': URL, 'authorization_scope': 'one exact application explicitly approved in the local review portal'})
            assert result['retained'][0]['state']['selected'] == 'San Diego, California, United States'
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally: browser.close()
