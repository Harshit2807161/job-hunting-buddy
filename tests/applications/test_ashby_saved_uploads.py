"""Synthetic upload/save/remount lifecycle; no live application or submission."""
from contextlib import contextmanager
import asyncio
import copy
import hashlib
import json

import pytest

from jhb.applications import ashby_uploads, boards
from jhb.applications.live_review import observe, project
from jhb.applications.manual_runtime import application_scope, dispatch

URL = 'https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555/application'
HTML = '''<title>Synthetic saved upload</title><form class=ashby-application-form-container>
<div data-field-path=_systemfield_resume><label class=ashby-application-form-question-title for=_systemfield_resume>Resume</label>
<input id=_systemfield_resume type=file required><p class=ashby-application-form-input-file-item-name></p></div>
<button type=submit>Submit Application</button></form><script>
window.uploads=0;window.submissions=0;window.failSave=false;
const owner=document.querySelector('[data-field-path]');
window.fileProps={field:{path:'_systemfield_resume'},savedFile:null};
function bind(input){input.__reactFiberFixture={memoizedProps:window.fileProps,return:{stateNode:owner}};
input.addEventListener('change',()=>{window.uploads++;if(window.failSave)return;
window.fileProps.savedFile={__typename:'File',id:'aaaaaaaa-bbbb-cccc-dddd-'+String(window.uploads).padStart(12,'0'),filename:input.files[0].name};
owner.querySelector('p').textContent=input.files[0].name;});}
bind(document.querySelector('input'));
window.remount=()=>{const old=document.querySelector('input'),input=document.createElement('input');
for(const a of old.attributes)input.setAttribute(a.name,a.value);old.replaceWith(input);bind(input)};
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
</script>'''


@contextmanager
def fixture(tmp_path):
    from playwright.sync_api import sync_playwright
    from pypdf import PdfWriter
    pdf = tmp_path/'resume.pdf'
    writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.write(pdf)
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.route('**/*',lambda r:r.fulfill(body=HTML,content_type='text/html'))
        page.goto(URL);session=page.context.new_cdp_session(page);calls=[]
        def cdp(method,**params):calls.append(method);return session.send(method,params)
        helpers={'js':page.evaluate,'cdp':cdp,'wait':lambda seconds:page.wait_for_timeout(min(seconds,.01)*1000),
                 'current_tab':lambda:{'targetId':'fixture-tab'},'list_tabs':lambda:[{'targetId':'fixture-tab','url':URL}],
                 'switch_tab':lambda target:None,'click_at_xy':lambda x,y:page.mouse.click(x,y)}
        def call(operation,**payload):return dispatch({'operation':operation,'scope':application_scope(URL),**payload},helpers)
        call('open',url=URL);field=call('observe')['fields'][0]
        try:yield page,helpers,calls,call,field,pdf
        finally:browser.close()


def packet_for(field,pdf,result):
    return {'job':{'url':URL,'dedupe_hash':boards.application_hash(URL)},'selected_role':'sde','events':[],
            'filled':[{'ref':field['ref'],'question':field['label'],'key':'documents.resume','value':str(pdf),
                       'source':'synthetic approved PDF','upload_receipt':result['upload_receipt'],
                       'document_sha256':result['sha256'],'ashby_upload_proof':result['ashby_upload_proof']}]}


def test_verified_bytes_and_new_server_id_survive_remount_without_refill(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf));proof=uploaded['ashby_upload_proof']
        assert proof['sha256']==hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert proof['saved_file_id'].endswith('000000000001') and proof['job_identity']==list(boards.job_identity(URL))
        assert proof['field_ref']==field['ref'] and proof['document_key']=='documents.resume'
        cached=call('fill',field=field,value=str(pdf),upload_receipt=uploaded['upload_receipt'])
        assert cached['cached'] and cached['ashby_upload_proof']==proof and page.evaluate('uploads')==1
        packet=packet_for(field,pdf,uploaded)
        # A current-form approval capture must retain the durable proof even
        # while native bytes are still readable, for its subsequent audits.
        first=project(packet,observe({'target_id':'fixture-tab','expected_url':URL},helpers))
        assert first['filled'][0]['ashby_upload_proof']==proof
        page.evaluate('remount()');assert page.locator('input').evaluate('e=>e.files.length')==0
        before=page.content();calls.clear();original=copy.deepcopy(packet)
        observation=observe({'target_id':'fixture-tab','expected_url':URL},helpers)
        assert observation['controls'][0]['state']['invalid'] is True  # native valueMissing only
        result=project(packet,observation)
        assert result['filled'][0]['ashby_upload_proof']==proof and result['filled'][0]['value']==str(pdf)
        assert result['live_documents']['documents.resume']['source']['document_origin']=='retained_ashby_server_file'
        assert result['review_completeness']['answered_count']==1
        assert packet==original and page.content()==before and page.evaluate('[uploads,submissions]')==[1,0]
        assert 'DOM.setFileInputFiles' not in calls and not any(c.startswith('Input.') for c in calls)
        # Retained proof is durable through serialization and repeated checks.
        result['job']=packet['job'];result=json.loads(json.dumps(result))
        assert project(result,observation)['filled'][0]['ashby_upload_proof']==proof


@pytest.mark.parametrize('change',['server_id','filename','proof_filename','field_path','wrong_job','wrong_ref','semantic_key',
                                  'hash','receipt','missing_proof','duplicate_row','local_bytes','aria_invalid','custom_error'])
def test_restored_attachment_never_uses_filename_only_or_crosses_bindings(tmp_path,change):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf));packet=packet_for(field,pdf,uploaded);page.evaluate('remount()')
        row=packet['filled'][0]
        if change=='server_id':page.evaluate("fileProps.savedFile.id='aaaaaaaa-bbbb-cccc-dddd-999999999999'")
        elif change=='filename':page.evaluate("fileProps.savedFile.filename='same-looking.pdf'")
        elif change=='proof_filename':row['ashby_upload_proof']['filename']='different.pdf'
        elif change=='field_path':row['ashby_upload_proof']['field_path']='another-owner'
        elif change=='wrong_job':row['ashby_upload_proof']['job_identity'][1]='different-company'
        elif change=='wrong_ref':row['ashby_upload_proof']['field_ref']='other-file'
        elif change=='semantic_key':row['ashby_upload_proof']['document_key']='documents.cover_letter'
        elif change=='hash':row['ashby_upload_proof']['sha256']='0'*64
        elif change=='receipt':row['upload_receipt']='unrelated-receipt'
        elif change=='missing_proof':row.pop('ashby_upload_proof')
        elif change=='duplicate_row':packet['filled'].append(copy.deepcopy(row))
        elif change=='local_bytes':pdf.write_bytes(pdf.read_bytes()+b'\nchanged')
        elif change=='aria_invalid':page.locator('input').evaluate("e=>e.setAttribute('aria-invalid','true')")
        elif change=='custom_error':page.locator('input').evaluate("e=>e.setCustomValidity('Rejected by employer')")
        original=copy.deepcopy(packet);before=page.content()
        observation=observe({'target_id':'fixture-tab','expected_url':URL},helpers)
        with pytest.raises(ValueError):project(packet,observation)
        assert packet==original and page.content()==before and page.evaluate('[uploads,submissions]')==[1,0]


@pytest.mark.parametrize('existing',[False,True])
def test_failed_save_or_unchanged_server_id_cannot_create_durable_proof(tmp_path,existing):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        if existing:
            call('fill',field=field,value=str(pdf))
            page.locator('input').evaluate('e=>delete e.__jhbAshbyUploadProof')
        page.evaluate('window.failSave=true')
        with pytest.raises(ValueError,match='server did not acknowledge'):
            call('fill',field=field,value=str(pdf))
        assert page.evaluate('submissions')==0


def test_new_worker_reuses_verified_native_upload_without_reselecting_file(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf))
        page.evaluate('window.failSave=true')  # An unchanged selection cannot save a new server ID.
        before=page.content();calls.clear()
        retained=call('fill',field=field,value=str(pdf))  # Fresh worker has no upload_receipt cache.
        assert retained=={**uploaded,'cached':True}
        assert page.content()==before and page.evaluate('[uploads,submissions]')==[1,0]
        assert 'DOM.setFileInputFiles' not in calls and not any(c.startswith('Input.') for c in calls)


@pytest.mark.parametrize('change',['server_id','filename','proof_filename','field_path','wrong_job','wrong_ref','semantic_key',
                                  'hash','receipt','size','native_bytes','aria_invalid','guard'])
def test_new_worker_rejects_changed_attachment_binding_without_replacing_user_file(tmp_path,monkeypatch,change):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        call('fill',field=field,value=str(pdf))
        if change=='server_id':page.evaluate("fileProps.savedFile.id='aaaaaaaa-bbbb-cccc-dddd-999999999999'")
        elif change=='filename':page.evaluate("fileProps.savedFile.filename='another.pdf'")
        elif change=='proof_filename':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.filename='different.pdf'")
        elif change=='field_path':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.field_path='different-owner'")
        elif change=='wrong_job':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.job_identity[1]='different-company'")
        elif change=='wrong_ref':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.field_ref='other-file'")
        elif change=='semantic_key':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.document_key='documents.cover_letter'")
        elif change=='hash':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.sha256='0'.repeat(64)")
        elif change=='receipt':page.locator('input').evaluate("e=>e.__jhbUploadReceipt='different-receipt'")
        elif change=='size':page.locator('input').evaluate("e=>e.__jhbAshbyUploadProof.size++")
        elif change=='native_bytes':monkeypatch.setattr(ashby_uploads,'native_bytes',lambda *a:{'name':pdf.name,'size':pdf.stat().st_size,'sha256':'0'*64})
        elif change=='aria_invalid':page.locator('input').evaluate("e=>e.setAttribute('aria-invalid','true')")
        elif change=='guard':page.evaluate('window.__jhbGuard=false')
        before=page.content();calls.clear()
        with pytest.raises(ValueError):call('fill',field=field,value=str(pdf))
        assert page.content()==before and page.evaluate('[uploads,submissions]')==[1,0]
        assert 'DOM.setFileInputFiles' not in calls


def test_new_approved_document_still_uses_upload_and_new_server_identity(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        original=call('fill',field=field,value=str(pdf))
        role_dir=tmp_path/'another-role';role_dir.mkdir()
        replacement=role_dir/pdf.name
        replacement.write_bytes(pdf.read_bytes()+b'\n% revised synthetic document')
        calls.clear();updated=call('fill',field=field,value=str(replacement))
        assert updated['sha256']!=original['sha256']
        assert updated['ashby_upload_proof']['saved_file_id']!=original['ashby_upload_proof']['saved_file_id']
        assert 'DOM.setFileInputFiles' in calls and page.evaluate('[uploads,submissions]')==[2,0]


def test_same_path_changed_bytes_cannot_reuse_old_receipt_when_server_id_does_not_change(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf))
        pdf.write_bytes(pdf.read_bytes()+b'\n% changed in place')
        with pytest.raises(ValueError,match='server did not acknowledge'):
            call('fill',field=field,value=str(pdf))
        assert uploaded['sha256']!=hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert page.evaluate('submissions')==0


def test_native_hash_mismatch_cannot_be_bound_to_server_id(tmp_path,monkeypatch):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        monkeypatch.setattr(ashby_uploads,'native_bytes',lambda *args:{'name':pdf.name,'size':pdf.stat().st_size,'sha256':'0'*64})
        with pytest.raises(ValueError,match='uploaded bytes differ'):
            call('fill',field=field,value=str(pdf))
        assert page.locator('input').evaluate('e=>e.__jhbAshbyUploadProof||null') is None


def test_same_named_server_replacement_invalidates_even_cached_native_receipt(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf))
        page.evaluate("fileProps.savedFile.id='aaaaaaaa-bbbb-cccc-dddd-999999999999'")
        with pytest.raises(ValueError,match='saved attachment changed'):
            call('fill',field=field,value=str(pdf),upload_receipt=uploaded['upload_receipt'])
        assert page.evaluate('[uploads,submissions]')==[1,0]


def test_saved_file_reads_current_react_branch_not_stale_alternate(tmp_path):
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        call('fill',field=field,value=str(pdf))
        page.evaluate("""()=>{
          const input=document.querySelector('input'),old=input.__reactFiberFixture;
          const shared={},oldRoot={stateNode:shared},currentRoot={stateNode:shared};shared.current=currentRoot;
          old.return.return=oldRoot;
          const props={field:{path:'_systemfield_resume'},savedFile:{...fileProps.savedFile,id:'aaaaaaaa-bbbb-cccc-dddd-888888888888'}};
          old.alternate={memoizedProps:props,return:{stateNode:owner,return:currentRoot}};
        }""")
        assert ashby_uploads.saved_file(helpers,field)['saved_file']['id'].endswith('888888888888')


@pytest.mark.parametrize('mismatch',[False,True])
def test_worker_persists_only_the_successful_uploads_exact_document_binding(tmp_path,mismatch):
    from jhb.applications import booklet
    from jhb.applications.worker import prepare
    from jhb.applications.planner import deterministic_plan
    with fixture(tmp_path) as (page,helpers,calls,call,field,pdf):
        uploaded=call('fill',field=field,value=str(pdf))
    if mismatch:uploaded['ashby_upload_proof']['document_key']='documents.cover_letter'
    class FileForm:
        blocked_requests=0
        def allowed_url(self,url):return True
        async def open(self,url):pass
        async def observe(self):return {'fields':[field],'buttons':[{'ref':'submit','label':'Submit Application'}]}
        async def fill(self,field,value):return uploaded
    operation=prepare(None,{'url':URL},{'documents.resume':booklet.answer(str(pdf),'synthetic verified source')},
                      deterministic_plan,None,cli_actions=FileForm())
    if mismatch:
        with pytest.raises(ValueError,match='field repair') as failure:asyncio.run(operation)
        assert 'proof differs' in str(failure.value.__cause__)
    else:
        result,_=asyncio.run(operation)
        persisted=json.loads(json.dumps(result))['filled'][0]
        assert persisted['ashby_upload_proof']==uploaded['ashby_upload_proof']
        assert persisted['document_sha256']==uploaded['sha256']
