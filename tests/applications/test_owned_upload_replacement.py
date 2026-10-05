"""Custom attachment replacement cannot target a neighboring resume upload."""
import hashlib
from contextlib import contextmanager

import pytest
from pypdf import PdfWriter

from jhb.applications.cli_runtime import dispatch

URL='https://job-boards.greenhouse.io/synthetic-uploads/jobs/1234'
LABEL='Portfolio or Cover Letter'
HTML='''<form id=application>
<div id=resume-group class=file-upload aria-labelledby=resume-label><div id=resume-label class=upload-label>Resume/CV*</div>
<input id=resume type=file hidden onchange="uploaded(this,false)"></div>
<div id=portfolio-group class=file-upload aria-labelledby=portfolio-label><div id=portfolio-label class=upload-label>Portfolio or Cover Letter*</div>
<input id=custom_portfolio_719 type=file hidden onchange="uploaded(this,true)"></div>
<button type=submit>Submit application</button></form><script>
window.uploads=[];window.removals=[];window.submissions=0;window.uploadedDigests={};
async function uploaded(input,removeInput){const file=input.files[0];const group=input.closest('.file-upload');
window.uploads.push(input.id);const digest=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
window.uploadedDigests[input.id]=Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
if(removeInput)input.remove();group.querySelector('.file-upload__filename')?.remove();
const wrapper=document.createElement('div');wrapper.className='file-upload__filename';const p=document.createElement('p');p.textContent=file.name;wrapper.append(p);
const remove=document.createElement('button');remove.type='button';remove.textContent='Remove file';remove.onclick=()=>{
window.removals.push(group.id);wrapper.remove();setTimeout(()=>{const next=document.createElement('input');next.type='file';next.hidden=true;next.id='custom_portfolio_719';
next.onchange=()=>uploaded(next,true);group.append(next)},450)};wrapper.append(remove);group.append(wrapper)}
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
</script>'''


def pdf(path,title):
    writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.add_metadata({'/Title':title})
    with path.open('wb') as f:writer.write(f)
    return path


@contextmanager
def browser(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chromium=pw.chromium.launch();page=chromium.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':page.mouse.click,
                 'list_tabs':lambda:[{'targetId':'fixture','url':URL}], 'current_tab':lambda:{'targetId':'fixture','url':URL},
                 'switch_tab':lambda target:None}
        dispatch({'operation':'open','url':URL},helpers)
        resume=pdf(tmp_path/'resume.pdf','Synthetic approved resume')
        page.locator('#resume').set_input_files(str(resume));page.wait_for_function("window.uploadedDigests.resume")
        try:yield page,helpers,resume
        finally:chromium.close()


def field(ref='custom_portfolio_719'):
    return {'ref':ref,'label':LABEL,'type':'file','required':False,'options':[]}


def current_resume_digest(page):
    return page.evaluate("async()=>{const bytes=await document.querySelector('#resume').files[0].arrayBuffer();const hash=await crypto.subtle.digest('SHA-256',bytes);return Array.from(new Uint8Array(hash),v=>v.toString(16).padStart(2,'0')).join('')}")


def test_custom_cover_first_upload_and_removed_input_replacement_preserve_actual_resume_bytes(tmp_path):
    first=pdf(tmp_path/'first-cover.pdf','Synthetic first cover');second=pdf(tmp_path/'second-cover.pdf','Synthetic second cover')
    with browser(tmp_path) as (page,helpers,resume):
        expected=hashlib.sha256(resume.read_bytes()).hexdigest()
        result=dispatch({'operation':'fill','field':field(),'value':str(first)},helpers)
        assert result['verified'] is True and result['filename']==first.name
        assert current_resume_digest(page)==expected
        assert page.locator('#custom_portfolio_719').count()==0
        observed=next(f for f in dispatch({'operation':'observe'},helpers)['fields'] if f['label']==LABEL)
        assert observed['ref']=='uploaded:'+LABEL
        result=dispatch({'operation':'fill','field':observed,'value':str(second)},helpers)
        assert result['verified'] is True and result['filename']==second.name
        assert page.evaluate('window.uploadedDigests.custom_portfolio_719')==hashlib.sha256(second.read_bytes()).hexdigest()
        assert current_resume_digest(page)==expected
        assert page.evaluate('window.uploads')==['resume','custom_portfolio_719','custom_portfolio_719']
        assert page.evaluate('window.removals')==['portfolio-group']
        assert page.locator('#resume-group .file-upload__filename p').inner_text()=='resume.pdf'
        assert page.evaluate('window.submissions')==0 and page.evaluate('window.__jhbGuard') is True


@pytest.mark.parametrize('mutation',['duplicate','missing'])
def test_missing_or_ambiguous_owned_container_fails_before_any_attachment_removal(tmp_path,mutation):
    cover=pdf(tmp_path/'cover.pdf','Synthetic cover')
    with browser(tmp_path) as (page,helpers,resume):
        dispatch({'operation':'fill','field':field(),'value':str(cover)},helpers)
        if mutation=='duplicate':page.locator('#portfolio-group').evaluate("e=>{const clone=e.cloneNode(true);clone.id='duplicate-group';e.after(clone)}")
        else:page.locator('#portfolio-group').evaluate('e=>e.remove()')
        with pytest.raises(ValueError,match='container is unavailable or ambiguous'):
            dispatch({'operation':'fill','field':field('uploaded:'+LABEL),'value':str(cover)},helpers)
        assert page.evaluate('window.removals')==[] and page.evaluate('window.uploads')==['resume','custom_portfolio_719']
        assert current_resume_digest(page)==hashlib.sha256(resume.read_bytes()).hexdigest()
        assert page.evaluate('window.submissions')==0


def test_initial_upload_ref_cannot_escape_exact_custom_document_container(tmp_path):
    cover=pdf(tmp_path/'cover.pdf','Synthetic cover')
    with browser(tmp_path) as (page,helpers,resume):
        with pytest.raises(ValueError,match='Upload input is unavailable'):
            dispatch({'operation':'fill','field':field('resume'),'value':str(cover)},helpers)
        assert page.evaluate('window.uploads')==['resume'] and page.evaluate('window.removals')==[]
        assert current_resume_digest(page)==hashlib.sha256(resume.read_bytes()).hexdigest()


def test_ambiguous_file_inputs_inside_owner_fail_before_removal(tmp_path):
    cover=pdf(tmp_path/'cover.pdf','Synthetic cover')
    with browser(tmp_path) as (page,helpers,resume):
        dispatch({'operation':'fill','field':field(),'value':str(cover)},helpers)
        page.locator('#portfolio-group').evaluate("e=>{for(const id of ['first-file','second-file']){const i=document.createElement('input');i.type='file';i.hidden=true;i.id=id;e.append(i)}}")
        with pytest.raises(ValueError,match='Owned upload input is unavailable or ambiguous'):
            dispatch({'operation':'fill','field':field('uploaded:'+LABEL),'value':str(cover)},helpers)
        assert page.evaluate('window.removals')==[]
        assert current_resume_digest(page)==hashlib.sha256(resume.read_bytes()).hexdigest()


def test_owner_disappearing_after_remove_never_uses_remaining_resume_input(tmp_path):
    cover=pdf(tmp_path/'cover.pdf','Synthetic cover')
    with browser(tmp_path) as (page,helpers,resume):
        dispatch({'operation':'fill','field':field(),'value':str(cover)},helpers)
        page.locator('#portfolio-group button').evaluate("e=>e.onclick=()=>{window.removals.push('portfolio-group');document.querySelector('#portfolio-group').remove()}")
        with pytest.raises(ValueError,match='Upload input is unavailable'):
            dispatch({'operation':'fill','field':field('uploaded:'+LABEL),'value':str(cover)},helpers)
        assert page.evaluate('window.removals')==['portfolio-group']
        assert page.evaluate('window.uploads')==['resume','custom_portfolio_719']
        assert current_resume_digest(page)==hashlib.sha256(resume.read_bytes()).hexdigest()
        assert page.evaluate('window.submissions')==0
