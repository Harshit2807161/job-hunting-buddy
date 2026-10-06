"""Explicit public-guided answers require fresh exact native catalog evidence."""
import asyncio
import copy
import hashlib
import json

import pytest

from jhb.applications import booklet, native_question_context as native, question_metadata, review_inventory, worker
from jhb.applications.cli_browser import BrowserOperationError
from jhb.applications.cli_runtime import dispatch
from jhb.applications.planner import deterministic_plan, key_for_field
from jhb.applications.submission_runtime import _checks

URL='https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
LABEL='HISTORY WITH SYNTHETIC COMPANY*'
DESCRIPTION='Have you previously applied to a position at Synthetic Company?'


def field():
    return {'ref':'question_101','label':LABEL,'type':'combobox','required':True,
            'description':DESCRIPTION,'description_truncated':False,'options':[]}


def record(control=None):
    control=control or field()
    proof={'source':'official_public_question_metadata','field_ref':control['ref'],'country_context':'',
           'description_sha256':question_metadata.description_digest(control['description']),
           'choices_sha256':hashlib.sha256(json.dumps(['Yes','No'],ensure_ascii=False).encode()).hexdigest(),
           'required':True,'observed_type':'combobox','metadata_sha256':'b'*64}
    return {**booklet.answer('No',{'provider':'explicit user question response','question_id':'q_'+'a'*24,
               'public_question_metadata_proofs':[proof]}),'user_override':True,'question':LABEL}


def test_context_probe_uses_only_actual_native_catalog_and_whitespace_equivalent_question():
    control=field();control['description']='Have you previously applied to a position at\n\nSynthetic Company?'
    answers={'custom.history':record()};calls=[]
    snapshot={'url':URL,'fields':[control,{'ref':'other','label':'Unanswered question','type':'combobox','required':True}]}
    assert key_for_field(control,answers) is None
    native.enrich_sync(snapshot,{'url':URL},answers,lambda f:calls.append(f['ref']) or {'choices':['Yes','No'],'type':'combobox','truncated':False})
    assert calls==['question_101'] and key_for_field(control,answers)=='custom.history'
    assert control['native_question_catalog']['source']=='owned_native_dropdown'
    assert answers['custom.history']['source']['public_question_metadata_proofs'][0]['source']=='official_public_question_metadata'


@pytest.mark.parametrize('change',['description','country','ref','type','required','unverified','missing_override','label','other_board'])
def test_other_native_context_changes_never_trigger_probe_or_reuse(change):
    control=field();item=record();url=URL
    if change=='description':control['description']='Have you previously been employed by Synthetic Company?'
    elif change=='country':control['country_context']='Canada'
    elif change=='ref':control['ref']='question_102'
    elif change=='type':control['type']='select'
    elif change=='required':control['required']=False
    elif change=='unverified':item['status']='needs_input'
    elif change=='missing_override':item.pop('user_override')
    elif change=='label':control['label']='A different question'
    else:url='https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555'
    snapshot={'url':url,'fields':[control]}
    native.enrich_sync(snapshot,{'url':url},{'custom.history':item},lambda f:pytest.fail('Unexpected dropdown exploration'))
    assert control['options']==[]


@pytest.mark.parametrize('descriptor',[{'choices':[],'type':'combobox'}, {'choices':['Yes','No'],'type':'combobox','truncated':True},
    {'choices':['Yes','Yes'],'type':'combobox'}, {'choices':['Yes','No'],'type':'select'}])
def test_empty_truncated_or_invalid_native_catalog_is_mechanical_not_candidate_question(descriptor):
    snapshot={'url':URL,'fields':[field()]}
    with pytest.raises(BrowserOperationError) as failure:
        native.enrich_sync(snapshot,{'url':URL},{'custom.history':record()},lambda f:descriptor)
    assert failure.value.retryable and snapshot['fields'][0]['options']==[]


def test_changed_native_choices_fail_strict_binding_without_using_public_choices():
    snapshot={'url':URL,'fields':[field()]};answers={'custom.history':record()}
    native.enrich_sync(snapshot,{'url':URL},answers,lambda f:{'choices':['Yes','No','Other'],'type':'combobox'})
    assert [o['label'] for o in snapshot['fields'][0]['options']]==['Yes','No','Other']
    assert key_for_field(snapshot['fields'][0],answers) is None


def test_wrong_job_and_probe_budget_stop_before_any_catalog_action():
    snapshot={'url':URL.replace('/1234','/1235'),'fields':[field()]}
    with pytest.raises(BrowserOperationError,match='outside the owned job'):
        native.enrich_sync(snapshot,{'url':URL},{'custom.history':record()},lambda f:pytest.fail('Unexpected probe'))
    snapshot={'url':URL,'fields':[field() for _ in range(native.MAX_PROBES+1)]}
    with pytest.raises(BrowserOperationError,match='bounded budget'):
        native.enrich_sync(snapshot,{'url':URL},{'custom.history':record()},lambda f:pytest.fail('Unexpected probe'))


def test_worker_retains_actual_catalog_and_user_override_without_terminal_action():
    class CLI:
        blocked_requests=0
        def __init__(self):self.probes=[];self.fills=[]
        def allowed_url(self,url):return url==URL
        async def open(self,url):assert url==URL
        async def observe(self):return {'url':URL,'fields':[field()],'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def describe(self,f):self.probes.append(f['ref']);return {'choices':['Yes','No'],'type':'combobox'}
        async def fill(self,f,value):self.fills.append((f['ref'],value));return {'verified':True}
        async def click_next(self,*args):pytest.fail('Unexpected terminal click')
    class Planner:
        def __call__(self,snapshot,answers):return deterministic_plan(snapshot,answers)
    cli=CLI();answers={'custom.history':record()}
    result,_=asyncio.run(worker.prepare(None,{'url':URL},answers,Planner(),None,cli_actions=cli))
    assert result['state']=='waiting_review' and cli.fills==[('question_101','No')]
    assert len(cli.probes)==2 and result['filled'][0]['user_override'] is True
    assert result['review_inventory']['fields'][0]['choices']==['Yes','No']
    assert result['review_inventory']['complete'] is True


@pytest.mark.parametrize('prior_failure', [None, {'operation': 'fill', 'kind': 'browser_mechanics'}])
def test_worker_catalog_rejection_keeps_exact_field_diagnostic_without_candidate_answer(prior_failure):
    from jhb.applications import attempt_feedback, boards
    class CLI:
        last_failure = prior_failure
        blocked_requests = 0
        def allowed_url(self, url): return url == URL
        async def open(self, url): pass
        async def observe(self): return {'url': URL, 'fields': [field()], 'buttons': []}
        async def describe(self, control): return {'choices': [], 'type': 'combobox'}
        async def fill(self, *args): pytest.fail('Catalog rejection must precede input')
    cli = CLI(); job = {'url': URL, 'dedupe_hash': boards.application_hash(URL)}
    with pytest.raises(BrowserOperationError) as failure:
        asyncio.run(worker.prepare(None, job, {'custom.history': record()},
                                   deterministic_plan, None, cli_actions=cli))
    result = worker.failure_result(failure.value, cli, job=job)
    assert result['failure_context'] == {'operation': 'describe', 'field_ref': 'question_101', 'field_type': 'combobox'}
    assert result['events'][-1]['mechanical_error'] == 'Approved-answer native dropdown catalog is unavailable'
    assert result['events'][-1]['operation'] == 'describe'
    assert result['filled'] == [] and not result.get('missing')
    feedback = attempt_feedback.build(job, result, attempt_token='native-catalog-rejection')
    assert feedback['operation'] == 'describe'
    assert LABEL not in json.dumps(feedback) and 'field_ref' not in feedback


@pytest.mark.parametrize('change',[None,'choices','description','country','missing_override','unrelated_question'])
def test_real_closed_native_catalog_reopened_for_final_audit_with_no_selection_or_submit(change):
    from playwright.sync_api import sync_playwright
    html='''<form id=application><div class=field-wrapper><div class=select><div class=select__container>
    <label for=question_101>HISTORY WITH SYNTHETIC COMPANY*</label><div class=select__value-container>
    <div class=select__single-value>No</div><input id=question_101 role=combobox aria-required=true aria-expanded=false
    onfocus="openOptions()" onkeydown="if(event.key==='ArrowDown')openOptions()"></div><div id=options role=listbox></div>
    <div id=question_101-description class=question-description>DESCRIPTION</div></div></div></div>
    <button type=submit>Submit application</button></form><script>
    window.catalogOpens=0;window.selections=0;window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
    function openOptions(){window.catalogOpens++;document.querySelector('input').setAttribute('aria-controls','options');document.querySelector('input').setAttribute('aria-expanded','true');
    document.querySelector('#options').innerHTML='<div role=option onclick="window.selections++">Yes</div><div role=option onclick="window.selections++">No</div>'}
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelector('#options').innerHTML='';document.querySelector('input').setAttribute('aria-expanded','false')}})
    </script>'''.replace('DESCRIPTION',DESCRIPTION)
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,'wait':lambda s:page.wait_for_timeout(s*1000),
            'click_at_xy':lambda x,y:page.mouse.click(x,y),'list_tabs':lambda:[{'targetId':'fixture','url':URL}],
            'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda target:None}
        try:
            dispatch({'operation':'open','url':URL},helpers)
            if change=='unrelated_question':page.evaluate("document.querySelector('form').insertAdjacentHTML('beforeend','<label for=other>Other optional question</label><input id=other>')")
            snapshot=dispatch({'operation':'observe'},helpers);answers={'custom.history':record()}
            native.enrich_sync(snapshot,{'url':URL},answers,lambda f:dispatch({'operation':'describe','field':f},helpers))
            control=snapshot['fields'][0];item=answers['custom.history']
            row={'ref':control['ref'],'question':control['label'],'key':'custom.history','value':'No','source':item['source'],'user_override':True}
            inventory=review_inventory.build(snapshot['fields'],[row],answers,key_for_field,complete=True)
            packet={'job':{'url':URL},'filled':[row],**inventory}
            page.evaluate('window.catalogOpens=0')
            if change=='unrelated_question':page.locator('label[for=other]').evaluate("e=>e.textContent='A newly changed optional question'")
            elif change=='choices':page.evaluate("window.openOptions=()=>{document.querySelector('input').setAttribute('aria-controls','options');document.querySelector('#options').innerHTML='<div role=option>Yes</div><div role=option>No</div><div role=option>Other</div>'}")
            elif change=='description':page.locator('#question_101-description').evaluate("e=>e.textContent='Have you previously been employed by Synthetic Company?'")
            elif change=='country':row['source']['public_question_metadata_proofs'][0]['country_context']='canada'
            elif change=='missing_override':row.pop('user_override')
            result=_checks({'target_id':'fixture','documents':{}},helpers,packet,{'application_url':URL,
                'authorization_scope':'one exact application explicitly approved in the local review portal'})
            if change is None:assert len(result['retained'])==1 and result.get('state') is None
            else:assert result['state']=='waiting_review'
            if change=='unrelated_question':assert page.evaluate('window.catalogOpens')==0
            assert page.locator('.select__single-value').inner_text()=='No'
            assert page.locator('#question_101').input_value()=='' and page.locator('#options').inner_text()==''
            assert page.evaluate('window.submissions')==page.evaluate('window.selections')==0
            assert page.evaluate('window.__jhbGuard') is True
        finally:browser.close()


def test_select_keeps_native_numeric_values_and_discards_only_empty_placeholder():
    control={**field(),'type':'select','options':[{'label':'Select...','value':'','disabled':False},
        {'label':'Yes','value':'71','disabled':False},{'label':'No','value':'72','disabled':False}]}
    item=record(control);item['source']['public_question_metadata_proofs'][0]['observed_type']='select'
    native.enrich_sync({'url':URL,'fields':[control]},{'url':URL},{'custom.history':item},
                      lambda f:{'choices':['Yes','No'],'type':'select'})
    assert control['options']==[{'label':'Yes','value':'71','disabled':False},{'label':'No','value':'72','disabled':False}]
    assert key_for_field(control,{'custom.history':item})=='custom.history'
