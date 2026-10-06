"""Bound base facts reopen native catalogs without changing retained answers."""
import copy
import hashlib
import json
import pytest
from jhb import config
from jhb.applications import approvals, boards, booklet, known_answers, native_question_context, overnight, planner, review_inventory, submission_runtime
from jhb.applications.cli_runtime import dispatch

URL='https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
LABELS=['Will you now or in the future require visa sponsorship to work in the United States?',
        'Are you able and willing to report to the office location listed in the job description, in a hybrid capacity?']

def book():
    return {'schema_version':1,'answers':{
        'eligibility.sponsorship':booklet.answer(True,'synthetic explicit combined sponsorship'),
        'preferences.relocation':booklet.answer(True,'synthetic relocation willingness')},
        'roles':{'sde':{},'ml':{}},'workflow_preferences':{
            'office_locations':{'value':True,'source':'synthetic office willingness'}},'custom_answers':{}}


def bind(root,packet,profile,mode):
    folder=root/'private';folder.mkdir(exist_ok=True)
    bp=folder/'book.json';pp=folder/'packet.json'
    booklet.write_private(bp,profile);booklet.write_private(pp,packet)
    binding={'packet_path':str(pp),'packet_sha256':hashlib.sha256(pp.read_bytes()).hexdigest(),
        'book_path':str(bp),'facts_sha256':approvals._facts(profile,packet['job'],'sde'),'selected_role':'sde'}
    attempt={'application_url':URL,'job_hash':packet['job']['dedupe_hash'],'packet_path':str(pp),
        'packet_sha256':binding['packet_sha256']}
    request={'target_id':'fixture','documents':{}}
    if mode=='delegated':attempt['review_binding']=binding
    else:
        auth={'scope':overnight.PORTAL_SCOPE,'job_hash':attempt['job_hash'],'binding':binding}
        ap=folder/'authority.json';booklet.write_private(ap,auth)
        request['authorization_path']=str(ap);attempt['authorization_id']=hashlib.sha256(ap.read_bytes()).hexdigest()
        attempt['authorization_scope']=overnight.PORTAL_SCOPE
    return request,attempt,binding


@pytest.mark.parametrize('mode',['delegated','portal'])
@pytest.mark.parametrize('change',[None,'retained_value','choices','base_fact','forged_derivation'])
def test_native_final_audit_reopens_only_bound_catalogs_without_selecting_or_submitting(tmp_path,monkeypatch,mode,change):
    from playwright.sync_api import sync_playwright
    monkeypatch.setattr(config,'ROOT',tmp_path)
    controls=''.join(f'''<div class=select><div class=select__container><label for=question_{i}>{label}</label>
    <div class=select__value-container><div class=select__single-value>Yes</div>
    <input id=question_{i} role=combobox aria-required=true aria-expanded=false onfocus="openMenu(this)" onkeydown="if(event.key==='ArrowDown')openMenu(this)"></div>
    <div id=options_{i} role=listbox></div></div></div>''' for i,label in enumerate(LABELS))
    html='<form id=application>'+controls+'''<button type=submit>Submit application</button></form><script>
    window.catalogOpens=0;window.selections=0;window.submissions=0;window.extraChoice=false;
    document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
    function openMenu(e){window.catalogOpens++;let options=document.getElementById('options_'+e.id.split('_')[1]);
    e.setAttribute('aria-controls',options.id);e.setAttribute('aria-expanded','true');
    options.innerHTML=['Yes','No',...(window.extraChoice?['Other']:[])].map(v=>'<div role=option onclick="window.selections++">'+v+'</div>').join('')}
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){
    document.querySelectorAll('[role=listbox]').forEach(e=>e.innerHTML='');
    document.querySelectorAll('[role=combobox]').forEach(e=>e.setAttribute('aria-expanded','false'))}})</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
        session=page.context.new_cdp_session(page)
        helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
            'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':lambda x,y:page.mouse.click(x,y),
            'list_tabs':lambda:[{'targetId':'fixture','url':URL}], 'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda target:None}
        try:
            dispatch({'operation':'open','url':URL},helpers)
            snapshot=dispatch({'operation':'observe'},helpers);profile=book();job={'url':URL,'dedupe_hash':boards.application_hash(URL),'company':'Synthetic','title':'Software Engineer','role_classes':'sde'}
            catalog=booklet.for_role(profile,'sde',job=job)
            native_question_context.enrich_sync(snapshot,job,catalog,lambda f:dispatch({'operation':'describe','field':f},helpers))
            records=[]
            for f in snapshot['fields']:
                key=known_answers.enrich(f,job,catalog);assert key
                records.append({'ref':f['ref'],'question':f['label'],'key':key,'value':catalog[key]['value'],'source':catalog[key]['source']})
            packet={'job':job,'selected_role':'sde','filled':records,**review_inventory.build(snapshot['fields'],records,catalog,planner.key_for_field,complete=True)}
            packet=copy.deepcopy(packet)
            if change=='forged_derivation':
                packet['filled'][0]['source']['records']['eligibility.sponsorship']['value']=False
            request,attempt,binding=bind(tmp_path,packet,profile,mode)
            if change=='base_fact':
                profile['answers']['eligibility.sponsorship']['value']=False;booklet.write_private(tmp_path/'private/book.json',profile)
            if change=='choices':page.evaluate('window.extraChoice=true')
            if change=='retained_value':page.locator('.select__single-value').first.evaluate("e=>e.textContent='No'")
            page.evaluate('window.catalogOpens=0')
            if change in {'base_fact','forged_derivation'}:
                with pytest.raises(ValueError,match='facts'):
                    submission_runtime._checks(request,helpers,packet,attempt)
                assert page.evaluate('window.catalogOpens')==0
            else:
                result=submission_runtime._checks(request,helpers,packet,attempt)
                if change is None:
                    assert result['double_check_count']==2 and not result.get('state')
                    assert [x['state']['selected'] for x in result['retained']]==['Yes','Yes']
                else:assert result['state']=='waiting_review' and result['click_started'] is False
                assert page.evaluate('window.catalogOpens')>=2
            assert page.evaluate('window.selections')==page.evaluate('window.submissions')==0
            assert page.evaluate('window.__jhbGuard') is True
            assert page.locator('[role=listbox]').all_text_contents()==['','']
        finally:browser.close()


def test_binding_must_match_same_job_role_and_packet_before_loading_probes(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    profile=book();job={'url':URL,'dedupe_hash':boards.application_hash(URL),'company':'Synthetic','title':'Software Engineer','role_classes':'sde'}
    f={'ref':'question_0','label':LABELS[0],'type':'combobox','options':[{'label':'Yes'},{'label':'No'}]}
    catalog=booklet.for_role(profile,'sde',job=job);key=known_answers.enrich(f,job,catalog)
    packet={'job':job,'selected_role':'sde','filled':[{'key':key,'source':catalog[key]['source'],'value':'Yes'}]}
    request,attempt,binding=bind(tmp_path,packet,profile,'delegated')
    approved={key:catalog[key]}
    for changed in ['job_hash','selected_role','packet_sha256']:
        bad=copy.deepcopy(attempt)
        if changed=='job_hash':bad[changed]='a'*64
        else:bad['review_binding'][changed]='ml' if changed=='selected_role' else 'a'*64
        with pytest.raises(ValueError,match='reviewed job and role'):
            submission_runtime._reviewed_probe_answers(request,packet,bad,approved)
    assert submission_runtime._reviewed_probe_answers({},packet,{},approved)==approved
