"""Verified facts use fresh owned catalogs; broader medical facts remain unknown."""
import copy
import asyncio

import pytest

from jhb.applications import booklet, native_question_context as native
from jhb.applications.cli_browser import BrowserOperationError
from jhb.applications.cli_runtime import dispatch, option_matches
from jhb.applications.planner import key_for_field

URL = 'https://job-boards.greenhouse.io/synthetic-company/jobs/1234'


def control(label, ref='question_1', kind='combobox'):
    return {'ref': ref, 'label': label, 'type': kind, 'required': False, 'options': []}


def verified(value):
    return booklet.answer(value, {'provider': 'synthetic verified candidate response'})


@pytest.mark.parametrize('label,key,value,choices,ref', [
    ('Gender', 'disclosure.gender', 'Male', ['Female', 'Male', 'Decline to self identify'], 'gender'),
    ('Are you Hispanic/Latino?', 'disclosure.hispanic', False, ['Yes', 'No', 'Decline to self identify'], 'hispanic_ethnicity'),
    ('Veteran Status', 'disclosure.veteran', False, ['I am not a protected veteran', 'I identify as one or more classifications of a protected veteran', 'I do not wish to answer'], 'veteran_status'),
    ('Protected Veteran Status', 'disclosure.veteran', False, ['I am not a protected veteran', 'I identify as a protected veteran'], 'custom_veteran'),
])
def test_standard_verified_facts_have_actual_catalogs_in_both_observation_paths(label,key,value,choices,ref):
    answers={key:verified(value)}
    for enrich in (native.enrich_sync,):
        snapshot={'url':URL,'fields':[control(label,ref)]};calls=[]
        enrich(snapshot,{'url':URL},answers,lambda f:calls.append(f['ref']) or {'choices':choices,'type':'combobox'})
        field=snapshot['fields'][0]
        assert calls==[ref] and [o['label'] for o in field['options']]==choices
        assert key_for_field(field,answers)==key
        assert sum(option_matches(c,value,field_id=ref,field_label=label) for c in choices)==1
        assert answers[key]['value'] is value or answers[key]['value']==value
        assert all('value' not in o for o in field['options'])


def test_known_closed_catalog_failure_is_technical_without_candidate_fact_mutation():
    answers={'disclosure.gender':verified('Male')};before=copy.deepcopy(answers)
    snapshot={'url':URL,'fields':[control('Gender')]}
    with pytest.raises(BrowserOperationError) as failure:
        native.enrich_sync(snapshot,{'url':URL},answers,lambda f:{'choices':[],'type':'combobox'})
    assert failure.value.retryable and answers==before


@pytest.mark.parametrize('changes', [{'status':'needs_input'}, {'source':None}, {'value':None}])
def test_unverified_unknown_disclosure_never_triggers_probe(changes):
    record={**verified('Male'),**changes}
    native.enrich_sync({'url':URL,'fields':[control('Gender')]},{'url':URL},{'disclosure.gender':record},
                       lambda f:pytest.fail('Unverified fact must not explore dropdown'))


def test_full_disability_history_wording_does_not_reuse_current_disability_no():
    choices=['Yes, I have a disability, or have had one in the past',
             'No, I do not have a disability and have not had one in the past','I do not want to answer']
    answers={'disclosure.disability':verified(False)};field=control('Disability Status','disability_status')
    native.enrich_sync({'url':URL,'fields':[field]},{'url':URL},answers,
                       lambda f:{'choices':choices,'type':'combobox'})
    assert key_for_field(field,answers) is None
    assert [o['label'] for o in field['options']]==choices
    assert not any(option_matches(c,False,field_id='disability_status',field_label=field['label']) for c in choices)


def test_veteran_true_does_not_claim_protected_veteran_classification():
    field=control('Protected Veteran Status','veteran_status')
    field['options']=[{'label':'I am not a protected veteran'}, {'label':'I identify as a protected veteran'}]
    assert key_for_field(field,{'disclosure.veteran':verified(True)}) is None


def test_known_select_preserves_native_ids_and_excludes_only_placeholder():
    field=control('Gender','gender','select');field['options']=[{'label':'Choose','value':''},
        {'label':'Male','value':'17'}, {'label':'Female','value':'29'}]
    native.enrich_sync({'url':URL,'fields':[field]},{'url':URL},{'disclosure.gender':verified('Male')},
                       lambda f:{'choices':['Male','Female'],'type':'select'})
    assert field['options']==[{'label':'Male','value':'17'}, {'label':'Female','value':'29'}]


def test_phone_country_world_catalog_does_not_trigger_truncated_global_probe():
    fields=[control('Country','country'),control('Country','citizenship_country')]
    fields[0]['phone_country']=True;calls=[]
    native.enrich_sync({'url':URL,'fields':fields},{'url':URL},{'identity.country':verified('United States')},
                       lambda f:calls.append(f['ref']) or {'choices':['United States (+1)','Canada (+1)'],'type':'combobox'})
    assert calls==[] and all(field['options']==[] for field in fields)
    assert option_matches('United States (+1)','United States',field_id='country')


def test_real_chromium_delayed_standard_gender_catalog_retains_selection_and_guard():
    from playwright.sync_api import sync_playwright
    html='''<form id=application><div class=select><div class=select__container>
    <label for=gender>Gender</label><div class=select__value-container><div class=select__single-value>Male</div>
    <input id=gender role=combobox aria-expanded=false onfocus="openMenu()" onkeydown="if(event.key==='ArrowDown')openMenu()"></div>
    <div id=options role=listbox></div></div></div><button type=submit>Submit application</button></form>
    <script>window.selections=0;window.submissions=0;let timer;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
    function openMenu(){clearTimeout(timer);gender.setAttribute('aria-controls','options');gender.setAttribute('aria-expanded','true');
    timer=setTimeout(()=>options.innerHTML='<div role=option onclick="window.selections++">Female</div><div role=option onclick="window.selections++">Male</div>',450)}
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){clearTimeout(timer);options.innerHTML='';gender.setAttribute('aria-expanded','false')}})</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'targetId':'fixture','url':URL}], 'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda t:None}
        try:
            dispatch({'operation':'open','url':URL},helpers)
            snapshot=dispatch({'operation':'observe'},helpers)
            native.enrich_sync(snapshot,{'url':URL},{'disclosure.gender':verified('Male')},
                              lambda f:dispatch({'operation':'describe','field':f},helpers))
            assert key_for_field(snapshot['fields'][0],{'disclosure.gender':verified('Male')})=='disclosure.gender'
            assert page.locator('.select__single-value').inner_text()=='Male'
            assert page.locator('#gender').input_value()==''
            assert page.locator('#options').inner_text()==''
            assert page.evaluate('window.selections')==page.evaluate('window.submissions')==0
            assert page.evaluate('window.__jhbGuard') is True
            from jhb.applications import review_inventory
            from jhb.applications.submission_runtime import _checks
            record=verified('Male');field=snapshot['fields'][0]
            row={'ref':field['ref'],'question':field['label'],'key':'disclosure.gender',
                 'value':'Male','source':record['source']}
            packet={'job':{'url':URL},'filled':[row],**review_inventory.build(snapshot['fields'],[row],
                {'disclosure.gender':record},key_for_field,complete=True)}
            check=_checks({'target_id':'fixture','documents':{}},helpers,packet,
                {'application_url':URL,'authorization_scope':'one exact application explicitly approved in the local review portal'})
            assert check.get('state') is None and len(check['retained'])==1
            assert page.evaluate('window.selections')==page.evaluate('window.submissions')==0
            assert page.locator('.select__single-value').inner_text()=='Male'
        finally:browser.close()


def test_real_phone_country_ownership_is_separate_from_contact_or_citizenship_country():
    from playwright.sync_api import sync_playwright
    html='''<form id=application><fieldset class=phone-input><legend>Phone</legend>
    <div class=phone-input__country><label for=country>Country</label><input id=country role=combobox></div>
    <div class=iti><label for=phone>Phone</label><input id=phone type=tel></div></fieldset>
    <label for=residence>Country</label><select id=residence><option>United States</option></select>
    <label for=citizenship>Country of citizenship</label><input id=citizenship role=combobox></form>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'targetId':'fixture','url':URL}], 'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda t:None}
        try:
            dispatch({'operation':'open','url':URL},helpers)
            fields={f['ref']:f for f in dispatch({'operation':'observe'},helpers)['fields']}
            assert fields['country']['phone_country'] is True
            assert fields['phone']['separate_phone_country'] is True
            assert 'phone_country' not in fields['residence'] and 'phone_country' not in fields['citizenship']
            assert key_for_field(fields['citizenship'],{'identity.country':verified('United States')}) is None
        finally:browser.close()


def test_async_observer_uses_same_known_catalog_rules_as_final_audit():
    async def describe(field):
        return {'choices':['Yes','No'],'type':'combobox'}
    field=control('Are you Hispanic/Latino?','hispanic_ethnicity')
    answers={'disclosure.hispanic':verified(False)}
    asyncio.run(native.enrich_async({'url':URL,'fields':[field]},{'url':URL},answers,describe))
    assert key_for_field(field,answers)=='disclosure.hispanic'
    assert field['native_question_catalog']['source']=='owned_native_dropdown'


@pytest.mark.parametrize('label,key,value,description', [
    ('Protected Veteran Status','disclosure.veteran',True,''),
    ('Gender','disclosure.gender','Male','What was your sex assigned at birth?'),
    ('Disability Status','disclosure.disability',False,'Have you ever had a disability?'),
])
def test_approved_current_fact_does_not_answer_broader_scope_even_literal_options(label,key,value,description):
    field=control(label);field['description']=description
    field['options']=[{'label':'Male'},{'label':'Female'}] if key=='disclosure.gender' else [{'label':'Yes'},{'label':'No'}]
    assert key_for_field(field,{key:verified(value)}) is None


def test_plain_country_with_citizenship_owned_help_does_not_use_contact_country():
    field=control('Country','citizenship_country');field['description']='Select your country of citizenship.'
    field['options']=[{'label':'United States'}, {'label':'Canada'}]
    assert key_for_field(field,{'identity.country':verified('United States')}) is None
