"""Synthetic browser audits for preserving candidate edits at portal approval.

These exercise read-only native controls in isolated fixture Chromium, not the
candidate's browser or a real application submission.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path

import pytest

from jhb.applications import boards
from jhb.applications.browser import GUARD_SCRIPT
from jhb.applications.live_review import observe, project
from jhb.applications.manual_runtime import application_scope, dispatch

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
HTML = '''<!doctype html><title>Synthetic current form</title>
<form class=ashby-application-form-container>
<div data-field-path=_systemfield_name><label class=ashby-application-form-question-title for=name>Full Name</label><input id=name required></div>
<div data-field-path=why><label class=ashby-application-form-question-title for=why>Why this company?</label>
<div class=ashby-application-form-question-description>Please use your own wording.</div><textarea id=why required></textarea></div>
<div data-field-path=phone><label class=ashby-application-form-question-title for=phone>Phone Number</label><input type=tel id=phone required></div>
<div data-field-path=location><label class=ashby-application-form-question-title for=location>Location</label>
<input id=location role=combobox aria-expanded=false></div>
<div data-field-path=authorized><label class=ashby-application-form-question-title>Authorized to work?</label>
<button type=button class=ashby-application-form-input-yesno-option data-option=yes aria-pressed=true>Yes</button>
<button type=button class=ashby-application-form-input-yesno-option data-option=no aria-pressed=false>No</button></div>
<div data-field-path=optional><label class=ashby-application-form-question-title for=optional>Anything else?</label><textarea id=optional></textarea></div>
<div data-field-path=_systemfield_resume><label class=ashby-application-form-question-title for=_systemfield_resume>Resume</label>
<input type=file id=_systemfield_resume required></div>
<button type=submit>Submit Application</button></form>
<script>window.userEvents=0;window.submissions=0;
document.addEventListener('input',()=>window.userEvents++,true);
document.addEventListener('change',()=>window.userEvents++,true);
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};</script>'''


@contextmanager
def native_form(tmp_path, same_name=True):
    from playwright.sync_api import sync_playwright
    from pypdf import PdfWriter
    old = tmp_path/'old'/'resume.pdf'; new = tmp_path/'new'/('resume.pdf' if same_name else 'candidate-edited.pdf')
    old.parent.mkdir(); new.parent.mkdir()
    for path, width in ((old, 612), (new, 640)):
        pdf=PdfWriter();pdf.add_blank_page(width=width,height=792);pdf.write(path)
    old_sha=hashlib.sha256(old.read_bytes()).hexdigest();new_sha=hashlib.sha256(new.read_bytes()).hexdigest()
    assert old_sha != new_sha
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML));page.goto(URL)
        page.locator('#name').fill('Synthetic Candidate')
        page.locator('#why').fill('I edited this paragraph myself. The product solves a concrete problem I care about.')
        page.locator('#phone').fill('(202) 555-0142')
        page.locator('#location').fill('San Diego, California, United States')
        page.locator('#_systemfield_resume').set_input_files(str(old))
        page.evaluate("document.querySelector('#_systemfield_resume').__jhbUploadReceipt='stale-old-receipt'")
        page.locator('#_systemfield_resume').set_input_files(str(new))
        page.evaluate(GUARD_SCRIPT)
        session=page.context.new_cdp_session(page);calls=[];actions=[]
        def cdp(method,**params):
            calls.append(method)
            assert not method.startswith('Input.') and method not in {'DOM.setFileInputFiles','DOM.focus','Page.navigate','Page.reload','Target.createTarget','Target.closeTarget'}
            return session.send(method,params)
        helpers={'cdp':cdp,'js':page.evaluate,'wait':lambda seconds:page.wait_for_timeout(seconds*1000),
                 'current_tab':lambda:{'targetId':'fixture-tab'},'list_tabs':lambda:[{'targetId':'fixture-tab','url':URL}],
                 'switch_tab':lambda target:actions.append(('attach',target)),
                 'activate_tab':lambda target:pytest.fail('Read-only capture must not activate a tab'),
                 'new_tab':lambda url:pytest.fail('Read-only capture must not open a tab'),
                 'click_at_xy':lambda x,y:pytest.fail('Read-only capture must not click')}
        packet={'job':{'url':URL,'dedupe_hash':boards.application_hash(URL),'company':'Synthetic Employer'},
                'filled':[{'ref':'why','question':'Why this company?','key':'custom.old','value':'Old agent prose','source':'synthetic old packet'},
                          {'ref':'phone','question':'Phone Number','key':'identity.phone','value':'+12025550111','source':'synthetic old phone'},
                          {'ref':'_systemfield_resume','question':'Resume','key':'documents.resume','value':str(old),'source':'old resume',
                           'document_sha256':old_sha,'upload_receipt':'stale-old-receipt'}],
                'selected_role':'sde','events':[],'capture':{'target_id':'fixture-tab'}}
        try:yield page,helpers,calls,actions,packet,new,new_sha
        finally:browser.close()


def current(page):
    return page.evaluate("""()=>({html:document.querySelector('form').outerHTML,events:window.userEvents,submissions:window.submissions,
        values:[...document.querySelectorAll('input,textarea')].map(e=>({id:e.id,value:e.value,checked:e.checked,
          files:e.files?[...e.files].map(f=>({name:f.name,size:f.size,lastModified:f.lastModified})):null}))})""")


@pytest.mark.parametrize('same_name',[True,False])
def test_read_only_capture_preserves_edited_prose_phone_and_replaced_pdf(tmp_path,same_name):
    with native_form(tmp_path,same_name) as (page,helpers,calls,actions,packet,new,new_sha):
        before=current(page);original=copy.deepcopy(packet)
        observation=dispatch({'operation':'review_current','scope':application_scope(URL),
                              'target_id':'fixture-tab','expected_url':URL},helpers)
        result=project(packet,observation)
        assert current(page)==before and packet==original
        assert observation['read_only'] is True and observation['target_id']=='fixture-tab'
        values={r['ref']:r['value'] for r in result['filled']}
        assert values['why']=='I edited this paragraph myself. The product solves a concrete problem I care about.'
        assert values['phone']=='(202) 555-0142'
        assert values['location']=='San Diego, California, United States'
        assert values['ashby:authorized']=='Yes'
        assert values['_systemfield_resume']==str(new)
        assert result['live_documents']['documents.resume']['source']['sha256']==new_sha
        assert result['live_documents']['documents.resume']['source']['document_origin']=='candidate_current_upload'
        assert next(f for f in result['review_inventory']['fields'] if f['ref']=='why')['description']=='Please use your own wording.'
        assert result['optional_questions']==[{'ref':'optional','question':'Anything else?','required':False,'type':'textarea'}]
        assert all(row['source']['provider']=='local_portal_current_form_approval' for row in result['filled'])
        assert result['submitted'] is False and before['submissions']==0
        assert 'DOM.getFileInfo' in calls and 'Runtime.callFunctionOn' in calls
        assert all(action==('attach','fixture-tab') for action in actions)


def test_live_optional_blank_filled_by_candidate_is_no_longer_a_blank(tmp_path):
    with native_form(tmp_path) as (page,helpers,calls,actions,packet,new,new_sha):
        packet['optional_questions']=[{'ref':'optional','question':'Anything else?','required':False,'type':'textarea'}]
        page.locator('#optional').fill('A new candidate-written answer, absent from the old packet.')
        before=current(page)
        observation=observe({'target_id':'fixture-tab','expected_url':URL},helpers)
        result=project(packet,observation)
        assert result['optional_questions']==[] and current(page)==before
        assert next(r for r in result['filled'] if r['ref']=='optional')['value'].startswith('A new candidate-written answer')


@pytest.mark.parametrize('mutation,reason',[
    ('required_blank','required blank'),('invalid_phone','invalid field'),
    ('truncated_help','instructions are incomplete'),('duplicate_ref','identity or instructions'),
    ('other_job','another application'),('no_terminal','final submission step'),('changed_local_pdf','bytes differ')])
def test_projection_rejects_incomplete_or_changed_live_evidence_without_writing(tmp_path,mutation,reason):
    with native_form(tmp_path) as (page,helpers,calls,actions,packet,new,new_sha):
        observation=observe({'target_id':'fixture-tab','expected_url':URL},helpers)
        changed=copy.deepcopy(observation)
        why=next(x for x in changed['controls'] if x['field']['ref']=='why')
        if mutation=='required_blank':why['state']['value']=''
        elif mutation=='invalid_phone':next(x for x in changed['controls'] if x['field']['ref']=='phone')['state']['invalid']=True
        elif mutation=='truncated_help':why['field']['description_truncated']=True
        elif mutation=='duplicate_ref':changed['controls'].append(copy.deepcopy(why))
        elif mutation=='other_job':changed['url']=URL.replace('555555555555','555555555556')
        elif mutation=='no_terminal':changed['buttons']=[]
        elif mutation=='changed_local_pdf':new.write_bytes(new.read_bytes()+b'\nChanged after browser upload')
        before=current(page)
        with pytest.raises(ValueError,match=reason):project(packet,changed)
        assert current(page)==before


@pytest.mark.parametrize('change',['target','url','guard'])
def test_current_capture_rejects_wrong_target_identity_or_missing_guard_before_reading_files(tmp_path,change):
    with native_form(tmp_path) as (page,helpers,calls,actions,packet,new,new_sha):
        request={'target_id':'fixture-tab','expected_url':URL}
        if change=='target':request['target_id']='another-tab'
        elif change=='url':request['expected_url']=URL.replace('555555555555','555555555556')
        else:page.evaluate('window.__jhbGuard=false')
        before=current(page)
        with pytest.raises(ValueError,match='exact guarded application tab'):observe(request,helpers)
        assert current(page)==before and 'DOM.getFileInfo' not in calls


@pytest.mark.parametrize('changed_after_capture',[False,True])
def test_final_current_form_check_is_read_only_and_never_restores_an_old_answer(tmp_path,changed_after_capture):
    from jhb.applications.submission_runtime import _checks
    with native_form(tmp_path) as (page,helpers,calls,actions,packet,new,new_sha):
        observation=observe({'target_id':'fixture-tab','expected_url':URL},helpers)
        approved={**project(packet,observation),'job':packet['job']}
        if changed_after_capture:
            page.locator('#why').fill('Another candidate edit made after clicking approval.')
        before=current(page)
        checked=_checks({'target_id':'fixture-tab','documents':{'documents.resume':{'path':str(new),'sha256':new_sha}}},
                        helpers,approved,{'application_url':URL})
        assert current(page)==before and before['submissions']==0
        if changed_after_capture:
            assert checked['state']=='waiting_review' and checked['click_started'] is False
            assert page.locator('#why').input_value()=='Another candidate edit made after clicking approval.'
        else:
            assert 'state' not in checked and checked['button']['label']=='Submit Application'
            assert checked['double_check_count']==len(approved['filled'])


def test_current_capture_cannot_widen_its_approved_native_dispatch_scope(tmp_path):
    with native_form(tmp_path) as (page,helpers,calls,actions,packet,new,new_sha):
        different_scope=application_scope(URL.replace('555555555555','555555555556'))
        before=current(page)
        with pytest.raises(ValueError,match='scope|approved'):
            dispatch({'operation':'review_current','scope':different_scope,'target_id':'fixture-tab','expected_url':URL},helpers)
        assert current(page)==before and 'DOM.getFileInfo' not in calls
