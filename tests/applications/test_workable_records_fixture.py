"""Synthetic Workable saved record editors, no real accounts or applications."""
import os
import pytest
from jhb import config
from jhb.applications.manual_runtime import application_scope,dispatch

URL='https://apply.workable.com/example/j/ABC1234567/apply'
HTML='''<form data-ui=application-form><label for=name>First name</label><input id=name>
<textarea data-ui=summary id=summary>Original global summary</textarea><input name=resume type=file><button type=submit>Submit application</button></form>
<button type=button onclick="openRecord('education',null)">Add Education</button>
<button type=button onclick="openRecord('experience',null)">Add Experience</button><div id=records></div><div id=editor></div>
<script>window.saved={education:{},experience:{}};window.submissions=0;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function openRecord(kind,key){
 const record=key?saved[kind][key]:{};const fields=kind==='education'?['school','field_of_study','degree']:['company','title','summary'];
 editor.innerHTML=fields.map(name=>name==='summary'?'<textarea name=summary id=summary></textarea>':'<input id="'+name+'">').join('')+
 '<input name=start_date><input name=end_date><button type=button onclick="updateRecord()">Update</button><button type=button onclick="cancelRecord()">Cancel</button>';
 editor.dataset.kind=kind;editor.dataset.key=key||'';
 for(const e of editor.querySelectorAll('input,textarea'))e.value=record[e.id||e.name]||'';
}
function cancelRecord(){editor.innerHTML=''};
function updateRecord(){const kind=editor.dataset.kind,r={};for(const e of editor.querySelectorAll('input,textarea'))r[e.id||e.name]=e.value;
 if(window.rejectFuture&&r.end_date==='12/2027')return;
 const key=r.school||r.company;saved[kind][key]=r;editor.innerHTML='';records.innerHTML='';
 for(const kind of ['education','experience'])for(const key of Object.keys(saved[kind])){
 const b=document.createElement('button');b.type='button';b.textContent='Edit '+key;b.onclick=()=>openRecord(kind,key);records.append(b);
 }};</script>'''

@pytest.mark.parametrize('reject_future',[False,True])
def test_two_education_and_employment_records_save_and_reopen_without_summary_collision(reject_future):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    education=[{'index':0,'school':'Example Graduate School','degree':'Master of Science','major':'Computer Science','start_date':'2025-09','end_date':'2027-12','status':'verified','source':'synthetic original education'},
               {'index':1,'school':'Example College','degree':'Bachelor of Science','major':'Engineering','start_date':'2021-08','end_date':'2025-05','status':'verified','source':'synthetic original education'}]
    experience=[{'index':0,'company':'Example Employer','title':'Software Engineer','summary':'• Improved runtime by 10%.\n• Reduced latency by 15%.','start_date':'2024-06','end_date':'2024-09','current':False,'status':'verified','source':'synthetic chosen SDE resume'}]
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
        page.goto(URL);session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda seconds:page.wait_for_timeout(seconds*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'url':URL,'targetId':'fixture'}],'switch_tab':lambda target:None,'current_tab':lambda:{'targetId':'fixture'}}
        def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(URL,'workable'),**payload},helpers)
        try:
            call('open',url=URL);page.evaluate('v=>window.rejectFuture=v',reject_future)
            result=call('records',education=education,experience=experience)
            if reject_future:
                assert result['handoff']=='unsupported'
                assert page.locator('#school').input_value()=='Example Graduate School'
                assert page.evaluate('Object.keys(saved.education).length')==0
            else:
                assert result['verified'] and len(result['filled'])==15
                saved=page.evaluate('saved')
                assert len(saved['education'])==2 and len(saved['experience'])==1
                assert saved['education']['Example Graduate School']['end_date']=='12/2027'
                assert saved['education']['Example College']['end_date']=='05/2025'
                assert saved['experience']['Example Employer']['summary']==experience[0]['summary']
                assert page.locator('#editor input').count()==0
                # A fresh attempt edits exact existing records; it does not append duplicates.
                assert call('records',education=education,experience=experience)['verified']
                assert page.evaluate('Object.keys(saved.education).length')==2
                before=page.evaluate('JSON.stringify(saved)')
                assert call('records',education=education,experience=experience,check_only=True)['verified']
                assert page.evaluate('JSON.stringify(saved)')==before
            assert page.locator('textarea[data-ui=summary]').input_value()=='Original global summary'
            assert page.evaluate('window.__jhbGuard') is True and page.evaluate('window.submissions')==0
        finally:browser.close()
