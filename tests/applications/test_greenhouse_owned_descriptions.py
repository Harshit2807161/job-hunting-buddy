"""Owned Greenhouse help text and native catalogs, using synthetic Chromium."""
import json
import pytest

from jhb.applications import booklet, known_answers, questions, review_inventory
from jhb.applications.cli_runtime import dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks
from jhb.applications.worker import _question

URL = 'https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
AUTH = 'Are you authorized to work in the United States?'
HTML = r'''<!doctype html><html><form id=application>
<div class=field-wrapper><div class=select><div class=select__container>
<label for=question_101 id=question_101-label>U.S. WORK AUTHORIZATION<span aria-hidden=true>*</span></label>
<div class=select__value-container><div class=select__single-value></div>
<input id=question_101 role=combobox aria-required=true aria-labelledby=question_101-label aria-describedby=placeholder aria-expanded=false
 onfocus="openMenu()" onkeydown="if(event.key==='ArrowDown')openMenu()">
</div><div id=options role=listbox></div>
<div id=placeholder>Select...</div>
<div id=question_101-description class=question-description><p>Are you authorized to work in the United States?</p></div>
<div class=field-wrapper><input id=foreign><div id=foreign-description class=question-description>Unrelated foreign question</div></div>
</div></div></div>
<div class=field-wrapper><label for=question_102>HISTORY WITH SYNTHETIC COMPANY*</label><input id=question_102 required>
<div id=question_102-description class=question-description><p>Have you previously applied to a position at Synthetic Company?</p></div></div>
<button type=submit>Submit application</button></form>
<script>
window.submissions=0;window.selections=0;let timer;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function openMenu(){clearTimeout(timer);document.querySelector('#question_101').setAttribute('aria-controls','options');
 document.querySelector('#question_101').setAttribute('aria-expanded','true');
 timer=setTimeout(()=>{document.querySelector('#options').innerHTML='<div role="option" onclick="choose(\'Yes\')">Yes</div><div role="option" onclick="choose(\'No\')">No</div>'},450)}
function choose(value){window.selections++;document.querySelector('.select__single-value').textContent=value;closeMenu()}
function closeMenu(){clearTimeout(timer);document.querySelector('#options').innerHTML='';document.querySelector('#question_101').setAttribute('aria-expanded','false')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenu()});
</script></html>'''


@pytest.fixture
def browser_form():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
        page.goto(URL);session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda seconds:page.wait_for_timeout(seconds*1000),
                 'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'targetId':'fixture','url':URL}],
                 'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda target:None}
        dispatch({'operation':'open','url':URL},helpers)
        try:yield page,helpers
        finally:browser.close()


def observe(helpers):
    return dispatch({'operation':'observe'},helpers)


def test_owned_heading_description_and_delayed_native_choices_preserve_draft(browser_form,tmp_path):
    page,helpers=browser_form
    control=next(f for f in observe(helpers)['fields'] if f['ref']=='question_101')
    assert control['label']=='U.S. WORK AUTHORIZATION*'
    assert control['description']==AUTH and control['description_truncated'] is False
    descriptor=dispatch({'operation':'describe','field':control},helpers)
    assert descriptor['choices']==['Yes','No'] and descriptor['truncated'] is False
    assert page.locator('#question_101').input_value()==''
    assert page.locator('.select__single-value').inner_text()==''
    assert page.locator('#question_101').get_attribute('aria-expanded')=='false'
    assert page.evaluate('window.selections')==0 and page.evaluate('window.submissions')==0
    assert page.evaluate('window.__jhbGuard') is True
    control['options']=[{'label':s,'value':s} for s in descriptor['choices']]
    missing=_question(control,None)
    assert missing['question']==control['label'] and missing['description']==AUTH and missing['choices']==['Yes','No']
    bookpath=tmp_path/'synthetic-book.json';booklet.write_private(bookpath,{'schema_version':1,'answers':{},'roles':{'sde':{},'ml':{}},'question_handoffs':{}})
    job={'dedupe_hash':'a'*64,'url':URL,'company':'Synthetic Company','title':'Software Engineer'}
    questions.collect(job,{'state':'waiting_input','missing':[missing]},bookpath=bookpath)
    records=json.loads(bookpath.read_text())['question_handoffs']
    context=next(iter(records.values()))['contexts']['a'*64]
    assert context['description']==AUTH and context['choices']==['Yes','No']
    inventory=review_inventory.build([control],[],{},key_for_field,complete=False)['review_inventory']['fields'][0]
    assert inventory['question']==control['label'] and inventory['description']==AUTH


@pytest.mark.parametrize('change',['hidden','aria_hidden','foreign_owner','duplicate_id','unowned_aria','truncate'])
def test_description_ownership_visibility_uniqueness_and_bounds(browser_form,change):
    page,helpers=browser_form
    if change=='hidden':page.locator('#question_101-description').evaluate("e=>e.style.display='none'")
    elif change=='aria_hidden':page.locator('#question_101-description').evaluate("e=>e.setAttribute('aria-hidden','true')")
    elif change=='foreign_owner':page.locator('#question_101-description').evaluate("e=>document.querySelector('#foreign').closest('.field-wrapper').append(e)")
    elif change=='duplicate_id':page.locator('#question_101-description').evaluate('e=>e.after(e.cloneNode(true))')
    elif change=='unowned_aria':
        page.locator('#question_101-description').evaluate('e=>e.remove()')
        page.locator('#question_101').evaluate("e=>e.setAttribute('aria-describedby','foreign-description')")
    else:page.locator('#question_101-description').evaluate("e=>e.textContent='X'.repeat(4097)")
    control=next(f for f in observe(helpers)['fields'] if f['ref']=='question_101')
    assert control['label']=='U.S. WORK AUTHORIZATION*'
    assert control['description']==('X'*4096 if change=='truncate' else '')
    assert control['description_truncated'] is (change=='truncate')
    assert page.evaluate('window.submissions')==0


@pytest.mark.parametrize('change',['changed','missing','truncated'])
def test_fresh_final_audit_rejects_changed_owned_description_before_any_click(browser_form,change):
    page,helpers=browser_form
    control=next(f for f in observe(helpers)['fields'] if f['ref']=='question_101')
    packet={'job':{'url':URL},'filled':[],**review_inventory.build([control],[],{},key_for_field,complete=True)}
    if change=='changed':page.locator('#question_101-description').evaluate("e=>e.textContent='Are you a United States citizen?'")
    elif change=='missing':packet['review_inventory']['fields'][0].pop('description')
    else:packet['review_inventory']['fields'][0]['description_truncated']=True
    result=_checks({'target_id':'fixture','documents':{}},helpers,packet,
                   {'application_url':URL,'authorization_scope':'one exact application explicitly approved in the local review portal'})
    assert result['state']=='waiting_review' and result['click_started'] is False
    assert page.evaluate('window.submissions')==0


def test_describe_never_borrows_foreign_options_or_changes_retained_selection(browser_form):
    page,helpers=browser_form
    page.evaluate("document.querySelector('.select__single-value').textContent='No';window.openMenu=()=>{}")
    page.evaluate("document.querySelector('#options').id='foreign-list';document.querySelector('#question_101').setAttribute('aria-controls','absent-owned-list');document.querySelector('#foreign-list').innerHTML='<div role=option>Foreign choice</div>'")
    control=next(f for f in observe(helpers)['fields'] if f['ref']=='question_101')
    descriptor=dispatch({'operation':'describe','field':control},helpers)
    assert descriptor['choices']==[]
    assert page.locator('.select__single-value').inner_text()=='No'
    assert page.evaluate('window.selections')==page.evaluate('window.submissions')==0


@pytest.mark.parametrize('change',[None,'heading_only','citizenship','truncated','foreign_country','unverified','different_heading'])
def test_authorization_heading_binds_only_exact_complete_owned_us_question(change):
    field={'ref':'question_101','label':'U.S. WORK AUTHORIZATION*','type':'combobox','required':True,
           'description':AUTH,'description_truncated':False,'options':[]}
    answers={'eligibility.authorized_us':booklet.answer(True,'synthetic explicitly verified US authorization')}
    if change=='heading_only':field['description']=''
    elif change=='citizenship':field['description']='Are you a United States citizen?'
    elif change=='truncated':field['description_truncated']=True
    elif change=='foreign_country':field['country_context']='Canada'
    elif change=='unverified':answers['eligibility.authorized_us']['status']='unknown'
    elif change=='different_heading':field['label']='EXPORT CONTROLS*'
    key=known_answers.enrich(field,{},answers)
    if change is None:
        assert key_for_field(field,answers)==key and answers[key]['value'] is True
        assert answers[key]['source']['owned_question']==AUTH
    else:assert key is None and key_for_field(field,answers) is None
