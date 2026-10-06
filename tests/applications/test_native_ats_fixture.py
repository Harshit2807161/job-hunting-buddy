"""Synthetic Workable/Lever controls through the fixed CLI dispatcher."""
import os
import pytest
from jhb import config
from jhb.applications.manual_ats import ManualATSCLI
from jhb.applications.manual_runtime import application_scope,dispatch

URLS=[('workable','https://apply.workable.com/example/j/ABC1234567/apply'),
      ('lever','https://jobs.lever.co/example/11111111-2222-3333-4444-555555555555/apply')]
HTML='''<form class="application-form">
<label for=first>* First name</label><input id=first required>
<label for=last>Last name (Optional)</label><input id=last>
<fieldset><legend>Are you authorized to work?</legend><label><input type=radio name=auth value=yes required>Yes</label><label><input type=radio name=auth value=no>No</label></fieldset>
<label for=country>Country</label><select id=country required><option value="">Select...</option><option value=us>United States</option><option value=ca>Canada</option></select>
<label for=cert><input id=cert type=checkbox required>I certify these answers are accurate</label>
<div data-ui=avatar><label for=photo>Choose file</label><input id=photo type=file></div>
<div data-ui=resume><label for=resume>Replace file</label><input id=resume name=resume type=file required></div>
<label for=unknown>Employer-specific required certification</label><input id=unknown required>
<button type=submit>Submit application</button></form>
<form id=newsletter><input aria-label=Newsletter></form>
<script>window.submissions=0;document.querySelector('.application-form').onsubmit=e=>{e.preventDefault();window.submissions++}</script>'''

@pytest.mark.parametrize('board,url',URLS)
def test_native_controls_have_exact_scope_choices_receipts_and_no_submit(tmp_path,board,url):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    pdf=tmp_path/'synthetic.pdf';pdf.write_bytes(b'%PDF-1.4\nsynthetic')
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=HTML))
        page.goto(url);session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda seconds:page.wait_for_timeout(seconds*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'url':url,'targetId':'fixture'}],'switch_tab':lambda target:None,
                 'current_tab':lambda:{'targetId':'fixture'}}
        def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(url,board),**payload},helpers)
        try:
            assert ManualATSCLI(url.rsplit('/apply',1)[0],board=board).application_url==url
            call('open',url=url);fields={f['ref']:f for f in call('observe')['fields']}
            assert fields['first']['label']=='First name' and fields['first']['required']
            assert fields['last']['label']=='Last name' and not fields['last']['required']
            assert 'photo' not in fields and fields['resume']['label']=='Resume'
            assert all(f['label']!='Newsletter' for f in fields.values())
            assert fields['unknown']['required']
            assert call('fill',field=fields['first'],value='Sam')['verified']
            assert call('fill',field=fields['native-radio:auth'],value=True)['verified']
            assert call('fill',field=fields['country'],value='United States')['verified']
            assert page.locator('#country').input_value()=='us'
            assert call('fill',field=fields['cert'],value=True)['verified']
            receipt=call('fill',field=fields['resume'],value=str(pdf))
            assert receipt['upload_receipt'] and receipt['sha256']
            assert page.locator('#photo').evaluate('e=>e.files.length')==0
            with pytest.raises(ValueError,match='absent'):
                call('fill',field=fields['country'],value='Invented')
            with pytest.raises(ValueError,match='changed'):
                call('fill',field={**fields['unknown'],'label':'Country'},value='United States')
            with pytest.raises(ValueError,match='Terminal'):
                call('next',button={'ref':'123','label':'Submit application'})
            page.get_by_role('button',name='Submit application').click()
            assert page.evaluate('window.submissions')==0
            page.goto(url.replace('example/','other/'))
            with pytest.raises(ValueError,match='differs'):
                call('fill',field=fields['first'],value='Sam')
        finally:browser.close()


def test_pdf_document_answer_never_binds_a_cover_letter_textarea():
    from jhb.applications.booklet import answer
    from jhb.applications.planner import key_for_field
    records={'documents.cover_letter':answer('/synthetic/source.pdf','verified PDF')}
    assert key_for_field({'ref':'cover','label':'Cover letter','type':'textarea'},records) is None
    assert key_for_field({'ref':'cover','label':'Cover letter','type':'file'},records)=='documents.cover_letter'


def test_lever_sibling_questions_checkbox_group_and_native_geocoder_commit():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    url=URLS[1][1]
    html='''<form id=application-form><input type=file name=resume>
    <div><div class="application-label"><div class=text>Languages<span class=required>✱</span></div></div>
    <div class=application-field><ul><li><label><input name=languages type=checkbox required value=English>English</label></li>
    <li><label><input name=languages type=checkbox required value=Hindi>Hindi</label></li></ul></div></div>
    <div><div class=application-label>Authorized to work?<span class=required>✱</span></div>
    <div class=application-field><label><input name=work type=radio value=yes required>Yes</label><label><input name=work type=radio value=no>No</label></div></div>
    <div><div class=application-label>Current location<span class=required>✱</span></div>
    <div class=application-field><input id=location-input name=location required oninput="load(this)">
    <input id=selected-location name=selectedLocation type=hidden><div class=dropdown-results></div></div></div>
    <label>Veteran status<select name=veteran><option>Select...</option><option value=no>I am not a protected veteran</option></select></label>
    <button type=submit>Submit application</button></form>
    <script>window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
    function load(input){document.getElementById('selected-location').value='';const root=input.parentElement.querySelector('.dropdown-results');root.innerHTML='';
    setTimeout(()=>{const e=document.createElement('div');e.style.padding='12px';e.textContent='Example City, CA, USA';e.onclick=()=>{
    input.value=e.textContent;document.getElementById('selected-location').value=window.noCommit?'':'catalog-example';root.innerHTML=''};root.append(e)},300)};</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html));page.goto(url)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda seconds:page.wait_for_timeout(seconds*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'url':url,'targetId':'fixture'}],'switch_tab':lambda target:None,'current_tab':lambda:{'targetId':'fixture'}}
        def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(url,'lever'),**payload},helpers)
        try:
            call('open',url=url);fields={f['label']:f for f in call('observe')['fields']}
            assert fields['Languages']['type']=='multiselect' and fields['Languages']['required']
            assert fields['Authorized to work?']['type']=='radio'
            assert fields['Current location']['widget']=='lever-location'
            assert fields['Veteran status']['type']=='select'
            assert call('fill',field=fields['Languages'],value=['English','Hindi'])['verified']
            assert call('fill',field=fields['Languages'],value=['English'])['verified']
            assert page.locator('input[value=English]').is_checked() and not page.locator('input[value=Hindi]').is_checked()
            assert call('fill',field=fields['Authorized to work?'],value=True)['verified']
            result=call('fill',field=fields['Current location'],value='Example City, CA')
            assert result['committed'] and page.locator('#selected-location').input_value()=='catalog-example'
            page.evaluate("document.getElementById('selected-location').value='';window.noCommit=true")
            with pytest.raises(ValueError,match='committed catalog'):
                call('fill',field=fields['Current location'],value='Example City, CA')
            assert page.evaluate('window.__jhbGuard') is True and page.evaluate('window.submissions')==0
        finally:browser.close()


def test_idless_native_name_survives_hidden_upload_metadata_insertion():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    url=URLS[1][1]
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<form><input name=resume type=file><label>Full name<input name=name required></label><label>Employer note<input name=note></label></form>'))
        page.goto(url);session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,'wait':lambda seconds:page.wait_for_timeout(seconds*1000),
                 'click_at_xy':lambda x,y:page.mouse.click(x,y),'list_tabs':lambda:[{'url':url,'targetId':'fixture'}],
                 'switch_tab':lambda target:None,'current_tab':lambda:{'targetId':'fixture'}}
        def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(url,'lever'),**payload},helpers)
        try:
            call('open',url=url);field=next(f for f in call('observe')['fields'] if f['label']=='Full name')
            assert field['ref']=='native-name:name'
            page.evaluate("const e=document.createElement('input');e.type='hidden';e.name='resumeStorageId';document.querySelector('form').prepend(e)")
            fresh=next(f for f in call('observe')['fields'] if f['label']=='Full name')
            assert fresh['ref']==field['ref'] and fresh['native_index']!=field['native_index']
            assert call('fill',field=field,value='Sam Example')['verified']
            assert page.locator('input[name=name]').input_value()=='Sam Example'
            assert page.locator('input[name=note]').input_value()==''
        finally:browser.close()


@pytest.mark.parametrize('has_id',[True,False])
def test_upload_receipt_remains_bound_when_change_handler_inserts_metadata(tmp_path,has_id):
    """A synchronous upload handler cannot shift receipt reads to another input."""
    import hashlib
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    url=URLS[1][1]
    pdf=tmp_path/'approved-synthetic.pdf';pdf.write_bytes(b'%PDF-1.4\nsynthetic approved document')
    file_id='id=resume' if has_id else ''
    html=f'''<form id=application-form>
    <label>Resume<input {file_id} name=resume type=file required></label>
    <label>Other attachment<input id=other type=file></label>
    <button type=submit>Submit application</button></form>
    <script>window.submissions=0;document.querySelector('form').onsubmit=e=>{{e.preventDefault();window.submissions++}};
    document.querySelector('[name=resume]').onchange=()=>{{const e=document.createElement('input');
    e.name='resumeStorageId';e.type='hidden';e.value='synthetic-storage';document.querySelector('form').prepend(e)}};</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
        page.goto(url);session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                 'wait':lambda seconds:page.wait_for_timeout(seconds*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
                 'list_tabs':lambda:[{'url':url,'targetId':'fixture'}],'switch_tab':lambda target:None,
                 'current_tab':lambda:{'targetId':'fixture'}}
        def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(url,'lever'),**payload},helpers)
        try:
            call('open',url=url)
            original=next(f for f in call('observe')['fields'] if f['label']=='Resume')
            result=call('fill',field=original,value=str(pdf))
            assert result['sha256']==hashlib.sha256(pdf.read_bytes()).hexdigest()
            assert result['upload_receipt']
            fresh=next(f for f in call('observe')['fields'] if f['label']=='Resume')
            assert fresh['ref']==original['ref'] and fresh['native_index']!=original['native_index']
            stored=page.locator('[name=resume]').evaluate('e=>({name:e.files[0].name,receipt:e.__jhbUploadReceipt,sha:e.__jhbUploadSha256})')
            assert stored=={'name':pdf.name,'receipt':result['upload_receipt'],'sha':result['sha256']}
            assert page.locator('#other').evaluate('e=>e.files.length')==0
            assert page.evaluate('window.submissions')==0 and page.evaluate('window.__jhbGuard') is True
        finally:browser.close()
