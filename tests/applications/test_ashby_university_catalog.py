"""Large native university catalog: canonical query, restoration and bound audit."""
import asyncio
import copy
import hashlib
import json
import os

import pytest

from jhb import config
from jhb.applications import approvals, boards, booklet, known_answers, native_question_context, planner, review_inventory, submission_runtime
from jhb.applications.cli_browser import BrowserOperationError
from jhb.applications.manual_runtime import application_scope, dispatch

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
QUESTION = 'Please select your current or most recent university.'
NOTE = 'If your university is not listed, please select "other."'
SCHOOL = 'Example University'
HTML = '''<form class=ashby-application-form-container><div data-field-path=university>
<div class=ashby-application-form-question-title>Please select your current or most recent university.</div>
<div class=ashby-application-form-question-description>If your university is not listed, please select "other."</div>
<input role=combobox required aria-autocomplete=list aria-expanded=false placeholder="Start typing...">
</div><button type=submit>Submit Application</button></form><div id=portal></div>
<script>
const input=document.querySelector('input');window.commits=0;window.submissions=0;window.queries=[];
window.ownership='owned';window.schools=[...Array(80)].map((_,i)=>'College '+i).concat(['Example University','Other']);
function closeMenu(){document.querySelector('#portal').innerHTML='';input.setAttribute('aria-expanded','false');input.removeAttribute('aria-controls')}
function openMenu(){
 const labels=window.schools.filter(s=>s.toLowerCase().includes(input.value.toLowerCase()));
 document.querySelector('#portal').innerHTML='<div role=listbox id=schools></div>';
 input.setAttribute('aria-expanded','true');input.setAttribute('aria-controls',window.ownership==='owned'?'schools':'missing');
 for(const label of labels){const option=document.createElement('div');option.setAttribute('role','option');option.textContent=label;
 option.onclick=()=>{input.value=label;window.commits++;closeMenu()};document.querySelector('#schools').append(option)}
}
input.addEventListener('input',()=>{window.queries.push(input.value);openMenu()});
input.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault();openMenu()}if(e.key==='Escape'){e.preventDefault();closeMenu()}});
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
</script>'''


def profile():
    return {'schema_version':1,'answers':{},'education_records':[{'school':SCHOOL,'degree':'Master of Science',
        'major':'Computer Science','start_date':'2025-09','end_date':'2030-12','expected':True,
        'status':'verified','source':'synthetic source'}],'roles':{'sde':{},'ml':{}}}


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',os.environ.get('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers')))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
            'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
            'list_tabs':lambda:[{'targetId':'fixture','url':URL}], 'current_tab':lambda:{'targetId':'fixture'},
            'switch_tab':lambda target:None,'activate_tab':lambda target:page.bring_to_front()}
        def call(op,**kw):return dispatch({'operation':op,'scope':application_scope(URL),'target_id':'fixture','expected_url':URL,**kw},helpers)
        try:
            call('open',url=URL);yield page,call,helpers
        finally:browser.close()


def enrich(call,answers):
    snapshot=call('observe')
    native_question_context.enrich_sync(snapshot,{'url':URL},answers,
        lambda field,**payload:call('describe',field=field,**payload))
    return snapshot


def test_large_owned_university_catalog_filters_beyond_fifty_and_commits(native):
    page,call,_=native;values=booklet.for_role(profile(),'sde');before=copy.deepcopy(values)
    field=call('observe')['fields'][0]
    old=call('describe',field=field)
    assert len(old['choices'])==50 and old['truncated'] and SCHOOL not in old['choices']
    snapshot=enrich(call,values);field=snapshot['fields'][0]
    assert field['options']==[{'label':SCHOOL}]
    assert page.locator('input').input_value()==''
    assert page.locator('input').get_attribute('aria-expanded')=='false'
    assert page.evaluate('[window.commits,window.submissions]')==[0,0]
    key=known_answers.enrich(field,{'url':URL},values)
    assert values[key]['value']=={'query':SCHOOL,'choice':SCHOOL}
    assert call('fill',field=field,value=values[key]['value'])['verified'] is True
    assert page.locator('input').input_value()==SCHOOL
    assert page.evaluate('[window.commits,window.submissions]')==[1,0]
    assert {k:values[k] for k in before}==before


@pytest.mark.parametrize('question,choices,expected',[
    ('Please select your graduation month',['May','December'],'December'),
    ('Please select your graduation year',['2029','2030'],'2030'),
])
def test_queryless_profile_catalog_projection_can_use_native_fill(native,question,choices,expected):
    page,call,_=native
    page.locator('.ashby-application-form-question-title').evaluate('(e,s)=>{e.textContent=s}',question)
    page.locator('.ashby-application-form-question-description').evaluate("e=>{e.textContent=''}")
    page.evaluate('(s)=>{window.schools=s}',choices)
    book=profile();book['answers']['education.expected_graduation_date']=booklet.answer('2030-12-14','synthetic explicit date')
    values=booklet.for_role(book,'sde');field=enrich(call,values)['fields'][0]
    assert page.evaluate('window.queries')==[]  # Catalog inspection remains queryless.
    key=known_answers.enrich(field,{'url':URL},values)
    assert values[key]['value']=={'query':expected,'choice':expected}
    assert call('fill',field=field,value=values[key]['value'])['verified']
    assert page.locator('input').input_value()==expected
    assert page.evaluate('[window.commits,window.submissions]')==[1,0]


@pytest.mark.parametrize('existing',[SCHOOL,'Other'])
def test_describe_never_retypes_retained_university(native,existing):
    page,call,_=native;page.locator('input').evaluate('(e,v)=>{e.value=v}',existing)
    values=booklet.for_role(profile(),'sde');snapshot=enrich(call,values)
    assert page.locator('input').input_value()==existing
    assert page.evaluate('[window.commits,window.submissions,window.queries.length]')==[0,0,0]
    key=known_answers.enrich(snapshot['fields'][0],{'url':URL},values)
    assert bool(key)==(existing==SCHOOL)


@pytest.mark.parametrize('problem',['missing','duplicate','unowned'])
def test_missing_ambiguous_or_foreign_catalog_cannot_invent_other(native,problem):
    page,call,_=native
    if problem=='missing':page.evaluate("window.schools=['Other']")
    if problem=='duplicate':page.evaluate("window.schools=['Example University','Example University']")
    if problem=='unowned':page.evaluate("window.ownership='unowned'")
    values=booklet.for_role(profile(),'sde')
    with pytest.raises(BrowserOperationError):enrich(call,values)
    assert page.locator('input').input_value()==''
    assert page.locator('input').get_attribute('aria-expanded')=='false'
    assert page.evaluate('[window.commits,window.submissions]')==[0,0]


@pytest.mark.parametrize('change',['original_school','unverified','clipped','different_help','different_question'])
def test_changed_facts_or_owned_context_do_not_issue_school_query(change):
    values=booklet.for_role(profile(),'sde');field={'ref':'ashby:university:control:0','label':QUESTION,
        'description':NOTE,'type':'combobox','required':True,'options':[]}
    if change=='original_school':values['standing.current_education_school']['source']['original_record']['school']='Other College'
    if change=='unverified':values['standing.current_education_school']['status']='needs_input'
    if change=='clipped':field['description_truncated']=True
    if change=='different_help':field['description']='Completed degrees only.'
    if change=='different_question':field['label']='Please select a preferred university.'
    assert known_answers.current_university_query(field,values) is None
    native_question_context.enrich_sync({'url':URL,'fields':[field]},{'url':URL},values,
        lambda *a,**kw:pytest.fail('Changed fact/context must not issue native query'))


@pytest.mark.parametrize('change',[None,'manual_value','source_fact'])
def test_native_submission_audit_keeps_retention_and_fact_binding(native,tmp_path,monkeypatch,change):
    page,call,helpers=native;book=profile();job={'url':URL,'dedupe_hash':boards.application_hash(URL),
        'company':'Synthetic','title':'Software Engineer','selected_role':'sde'}
    values=booklet.for_role(book,'sde');snapshot=enrich(call,values);field=snapshot['fields'][0]
    key=known_answers.enrich(field,job,values);record=values[key]
    assert call('fill',field=field,value=record['value'])['verified']
    rows=[{'ref':field['ref'],'question':field['label'],'key':key,'value':record['value'],'source':record['source']}]
    packet={'job':job,'selected_role':'sde','filled':rows,**review_inventory.build(snapshot['fields'],rows,values,planner.key_for_field,complete=True)}
    monkeypatch.setattr(config,'ROOT',tmp_path);pp=tmp_path/'private/packet.json';bp=tmp_path/'private/book.json'
    booklet.write_private(pp,packet);booklet.write_private(bp,book)
    binding={'packet_path':str(pp),'packet_sha256':hashlib.sha256(pp.read_bytes()).hexdigest(),
        'book_path':str(bp),'facts_sha256':approvals._facts(book,job,'sde'),'selected_role':'sde'}
    attempt={'application_url':URL,'job_hash':job['dedupe_hash'],'packet_path':str(pp),
        'packet_sha256':binding['packet_sha256'],'review_binding':binding}
    if change=='source_fact':book['education_records'][0]['school']='Different University';booklet.write_private(bp,book)
    if change=='manual_value':page.locator('input').evaluate("e=>{e.value='Other'}")
    commits=page.evaluate('window.commits');queries=page.evaluate('window.queries.length')
    if change=='source_fact':
        with pytest.raises(ValueError,match='facts'):submission_runtime._checks({'target_id':'fixture','documents':{}},helpers,packet,attempt)
    else:
        result=submission_runtime._checks({'target_id':'fixture','documents':{}},helpers,packet,attempt)
        if change is None:assert not result.get('state') and result['double_check_count']==1
        else:assert result['state']=='waiting_review' and result['click_started'] is False
    assert page.evaluate('window.commits')==commits and page.evaluate('window.queries.length')==queries
    assert page.evaluate('window.submissions')==0
    assert json.loads(pp.read_text())==packet
