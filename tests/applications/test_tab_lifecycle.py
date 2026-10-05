"""Exact ownership, durable receipt evidence, and no blind tab closure."""
import hashlib
import base64
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from jhb import config
from jhb.applications import boards, cli_runtime, tracking
from jhb.applications.booklet import write_private
from jhb.applications.cli_browser import BrowserUseCLI, BrowserCapacityError, MARKER
from jhb.applications.tab_lifecycle import OwnedTabs, TabCapacityReached, PNG, dispatch_owned
from jhb.applications import capture

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
OTHER = 'https://jobs.ashbyhq.com/example/22222222-2222-3333-4444-555555555555/application'
SOURCE = 'https://www.linkedin.com/jobs/view/1234567890/'
MODULE = 'jhb.applications.manual_runtime'
IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC')


class Browser:
    """Official helper contracts, including new_tab's reused-blank behavior."""
    def __init__(self, url='https://mail.google.com/mail/u/0/'):
        self.tabs = {'user': {'targetId':'user','url':url,'body':'Private user page','nodes':[]}}
        self.current = 'user'
        self.created, self.closed, self.new_calls = [], [], []
        self.close_effect = True
        self.concurrent_user_tab = False

    def new(self, url='about:blank'):
        self.new_calls.append(url)
        old = self.tabs.get(self.current, {})
        if old.get('url') == 'about:blank':
            old['url'] = url
            return self.current
        if self.concurrent_user_tab:
            self.tabs['concurrent-user'] = {'targetId':'concurrent-user','url':OTHER,'body':'','nodes':[]}
        target = f'worker-{len(self.created)+1}'
        self.created.append(target)
        self.tabs[target] = {'targetId':target,'url':url,'body':'Form','nodes':[]}
        self.current = target
        return target

    def switch(self, target):
        assert target in self.tabs
        self.current = target

    def close(self, target):
        assert target is not None  # Never rely on the currently attached tab.
        self.closed.append(target)
        if self.close_effect:
            self.tabs.pop(target)

    def js(self, expression):
        row = self.tabs[self.current]
        return row['url'] if expression == 'location.href' else row['body']

    def helpers(self):
        return {'list_tabs':lambda:list(self.tabs.values()),'current_tab':lambda:dict(self.tabs[self.current]),
                'switch_tab':self.switch,'new_tab':self.new,'close_tab':self.close,
                'js':self.js,'cdp':self.cdp}

    def cdp(self, command, **payload):
        if command == 'Target.getTargetInfo':
            row = self.tabs[payload['targetId']]
            return {'targetInfo':{**row,'type':'page'}}
        return {'nodes':self.tabs[self.current]['nodes']}


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.delenv('JHB_MAX_OWNED_TABS',raising=False)
    browser = Browser()
    owner = OwnedTabs(browser.helpers(),tmp_path)
    return owner,browser,tmp_path


def complete_review(owner,browser,root,target):
    folder = root/'private'/'packets'/boards.application_hash(URL)
    image = folder/'browser.png'
    folder.mkdir(parents=True)
    temporary=folder/('.capture-'+'a'*32+'.png')
    temporary.write_bytes(IMAGE);temporary.chmod(0o600)
    browser.switch(target)
    owner.retain_review({'target_id':target,'path':str(temporary)})
    temporary.replace(image)
    created=int(time.time())
    packet = {'state':'waiting_review','submitted':False,'job':{'url':URL,'dedupe_hash':boards.application_hash(URL)},'missing':[],
              'review_inventory':{'complete':True,'fields':[]},'created_at':created,
              'capture':{'schema_version':1,'capture_id':'a'*32,'verified':True,'filename':'browser.png',
                         'job_hash':boards.application_hash(URL),'packet_created_at':created,
                         'captured_at':datetime.now(timezone.utc).isoformat(),
                         'sha256':hashlib.sha256(IMAGE).hexdigest(),'target_id':target}}
    assert capture.valid(packet,IMAGE)
    write_private(folder/'packet.json',packet)
    return folder


def confirmed(owner,browser,root,target):
    folder = complete_review(owner,browser,root,target)
    receipt_path = root/'private'/'receipts'/'receipt.json'
    receipt = {'state':'submitted','url':URL,'observed_url':URL,'target_id':target,
               'confirmed_at':datetime.now(timezone.utc).isoformat(),'confirmation':'Thank you for applying.',
               'body':'Thank you for applying. We have received your application.',
               'source':'Live Browser Use CLI success page'}
    write_private(receipt_path,receipt)
    root.joinpath('data').mkdir()
    conn = sqlite3.connect(root/'data/jobs.sqlite3')
    conn.row_factory = sqlite3.Row
    result = tracking.record_confirmed(conn,{'url':URL,'company':'Example','title':'Engineer'},receipt_path)
    assert result['state'] == 'submitted'
    conn.close()
    browser.tabs[target]['body'] = receipt['body']
    return folder,receipt_path


def test_new_helper_target_is_owned_but_concurrent_user_target_is_not(owned):
    owner,browser,root=owned
    browser.concurrent_user_tab=True
    target=owner.new_tab(URL)
    assert list(owner.tabs)==[target] and 'concurrent-user' not in owner.tabs
    assert owner.tabs[target]['creation_proof']=='official_new_tab_returned_new_target'
    assert (root/'private/browser-tab-ledger.json').stat().st_mode & 0o777 == 0o600
    assert OwnedTabs(browser.helpers(),root).tabs == owner.tabs


def test_reused_preexisting_blank_and_exact_user_job_remain_unowned(owned):
    owner,browser,_=owned
    browser.tabs['user']['url']='about:blank'
    assert owner.new_tab(URL)=='user' and owner.tabs=={}
    assert owner.new_tab(URL)=='user' and browser.new_calls==[URL]
    assert browser.closed==[]


def test_attached_hidden_placeholder_is_not_false_creation_proof(owned):
    owner,browser,_=owned
    browser.tabs['user']['url']='about:blank'
    original=owner.helpers['list_tabs']
    calls=[]
    def filtered():
        calls.append(True)
        return [] if len(calls)==1 else original()
    owner.helpers['list_tabs']=filtered
    assert owner.new_tab(URL)=='user' and owner.tabs=={}


def test_departed_target_does_not_transfer_ownership_to_same_url_user_tab(owned):
    owner,browser,_=owned
    target=owner.new_tab(URL)
    browser.tabs.pop(target)
    browser.tabs['user']['url']=URL
    owner.refresh()
    assert owner.tabs[target]['state']=='departed'
    assert owner.new_tab(URL)=='user' and 'user' not in owner.tabs
    assert browser.closed==[]


def test_capacity_stops_before_new_target_but_allows_existing_reuse(owned,monkeypatch):
    owner,browser,_=owned
    monkeypatch.setenv('JHB_MAX_OWNED_TABS','1')
    target=owner.new_tab(URL)
    before=list(browser.new_calls)
    with pytest.raises(TabCapacityReached,match='capacity'):
        owner.new_tab(OTHER)
    assert browser.new_calls==before and browser.created==[target]
    assert owner.new_tab(URL)==target and browser.new_calls==before
    # Reusing the user's own blank cannot add an owned tab or claim that blank.
    browser.tabs['user']['url']='about:blank';browser.switch('user')
    assert owner.new_tab(OTHER)=='user' and 'user' not in owner.tabs


def test_default_pool_preserves_five_drafts_and_one_readonly_source_slot(owned):
    owner, browser, _ = owned
    drafts = [owner.new_tab(f'https://job-boards.greenhouse.io/synthetic/jobs/{n}') for n in range(1, 6)]
    with pytest.raises(TabCapacityReached):
        owner.new_tab('https://job-boards.greenhouse.io/synthetic/jobs/6')
    source = owner.new_tab(SOURCE, purpose='source_readonly')
    assert owner._count(owner.refresh()) == 6
    assert all(target in browser.tabs for target in drafts)
    assert owner.new_tab(SOURCE, purpose='source_readonly') == source
    assert owner.new_tab('https://job-boards.greenhouse.io/synthetic/jobs/1') == drafts[0]
    with pytest.raises(TabCapacityReached):
        owner.new_tab(SOURCE.replace('1234567890', '9876543210'), purpose='source_readonly')
    assert not browser.closed


def test_capacity_before_native_apply_preserves_readonly_cleanup_witness(owned):
    owner, browser, _ = owned
    for n in range(1, 6):
        owner.new_tab(f'https://job-boards.greenhouse.io/synthetic/jobs/{n}')
    source = owner.new_tab(SOURCE, purpose='source_readonly')
    owner.record_readonly_observation({'approved_url': SOURCE}, {'state': 'ambiguous', 'native_apply_required': True})
    before = dict(owner.tabs[source])
    with pytest.raises(TabCapacityReached):
        owner.before_apply_click()
    assert owner.tabs[source] == before
    assert owner.tabs[source]['readonly_observation']['native_apply_clicked'] is False
    assert not browser.closed


def test_absent_unconfirmed_source_frees_reserved_slot_without_another_close(owned):
    owner, browser, _ = owned
    for n in range(1, 6):
        owner.new_tab(f'https://job-boards.greenhouse.io/synthetic/jobs/{n}')
    source = owner.new_tab(SOURCE, purpose='source_readonly')
    owner.tabs[source]['state'] = 'close_unconfirmed'; owner.save()
    browser.tabs.pop(source); browser.switch('user')
    replacement = owner.new_tab(SOURCE.replace('1234567890', '9876543210'), purpose='source_readonly')
    assert owner.tabs[source]['state'] == 'departed'
    assert replacement != source and owner._count(owner.refresh()) == 6
    assert not browser.closed


@pytest.mark.parametrize('value',['0','13','invalid'])
def test_tab_cap_is_bounded_configuration_not_candidate_input(owned,monkeypatch,value):
    owner,_,_=owned
    monkeypatch.setenv('JHB_MAX_OWNED_TABS',value)
    with pytest.raises(ValueError,match='between 1 and 12'):
        owner.new_tab(URL)


def test_pending_draft_and_unrecognized_navigation_are_preserved(owned):
    owner,browser,_=owned
    target=owner.new_tab(URL)
    browser.tabs[target]['url']='https://accounts.google.com/'
    assert owner.cleanup()==[] and browser.closed==[]
    assert browser.tabs['user']['url'].startswith('https://mail.google.com/')


def test_positive_durable_receipt_and_retained_review_close_only_exact_owned_target(owned):
    owner,browser,root=owned
    target=owner.new_tab(URL)
    confirmed(owner,browser,root,target)
    browser.switch('user')
    assert owner.cleanup()==[target]
    assert browser.closed==[target] and set(browser.tabs)=={'user'}
    assert browser.current=='user' and owner.tabs[target]['state']=='closed'
    assert owner.cleanup()==[] and browser.closed==[target]


@pytest.mark.parametrize('problem',['missing_image','changed_image','changed_packet','failed_capture','wrong_capture_target','wrong_target','wrong_observed_url',
                                  'wrong_identity','unconfirmed_receipt','reopened_form','wrong_body','receipt_not_tracked'])
def test_incomplete_or_mismatched_evidence_never_closes_draft(owned,problem):
    owner,browser,root=owned
    target=owner.new_tab(URL)
    folder,receipt_path=confirmed(owner,browser,root,target)
    if problem=='missing_image':folder.joinpath('browser.png').unlink()
    elif problem=='changed_image':folder.joinpath('browser.png').write_bytes(PNG+b'changed')
    elif problem=='changed_packet':
        packet=json.loads(folder.joinpath('packet.json').read_text());packet['state']='waiting_input'
        write_private(folder/'packet.json',packet)
    elif problem in {'failed_capture','wrong_capture_target'}:
        packet=json.loads(folder.joinpath('packet.json').read_text())
        packet['capture']['verified' if problem=='failed_capture' else 'target_id']=False if problem=='failed_capture' else 'user'
        write_private(folder/'packet.json',packet)
    elif problem in {'wrong_target','wrong_observed_url','wrong_identity','unconfirmed_receipt'}:
        receipt=json.loads(receipt_path.read_text())
        receipt[{'wrong_target':'target_id','wrong_observed_url':'observed_url','wrong_identity':'url','unconfirmed_receipt':'state'}[problem]]={
            'wrong_target':'user','wrong_observed_url':OTHER,'wrong_identity':OTHER,'unconfirmed_receipt':'uncertain'}[problem]
        write_private(receipt_path,receipt)
        # Even updating a tracker digest cannot bypass receipt/identity/live checks.
        with sqlite3.connect(root/'data/jobs.sqlite3') as c:
            proof=json.loads(c.execute('SELECT proof_json FROM confirmed_submissions').fetchone()[0])
            proof['receipt_sha256']=hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            c.execute('UPDATE confirmed_submissions SET proof_json=?',(json.dumps(proof),))
    elif problem=='reopened_form':browser.tabs[target]['nodes']=[{'role':{'value':'button'},'name':{'value':'Submit application'}}]
    elif problem=='wrong_body':browser.tabs[target]['body']='Your application needs review.'
    elif problem=='receipt_not_tracked':
        with sqlite3.connect(root/'data/jobs.sqlite3') as c:c.execute('DELETE FROM confirmed_submissions')
    assert owner.cleanup()==[] and browser.closed==[] and target in browser.tabs


def test_uncertain_close_is_not_blindly_replayed(owned):
    owner,browser,root=owned
    target=owner.new_tab(URL);confirmed(owner,browser,root,target)
    browser.close_effect=False
    assert owner.cleanup()==[] and browser.closed==[target]
    assert owner.tabs[target]['state']=='close_unconfirmed'
    assert owner.cleanup()==[] and browser.closed==[target]


def test_source_close_requires_observed_separate_exact_destination_and_keeps_popup_unowned(owned):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source')
    destination=browser.new(URL)
    result={'state':'destination','application_url':URL,'evidence':[{'url':URL,'operation':'observed_apply_destination'}]}
    assert destination not in owner.tabs
    assert owner.cleanup_source({'approved_url':SOURCE},result)==[source]
    assert destination in browser.tabs and browser.current==destination and browser.closed==[source]


@pytest.mark.parametrize('problem',['same_target','unobserved','changed_destination','login'])
def test_transient_source_is_preserved_without_actual_external_destination(owned,problem):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source')
    result={'state':'destination','application_url':URL,'evidence':[{'url':URL,'operation':'observed_apply_destination'}]}
    if problem=='same_target':browser.tabs[source]['url']=URL
    else:browser.new(URL)
    if problem=='unobserved':result['evidence']=[]
    elif problem=='changed_destination':browser.tabs[browser.current]['url']=OTHER
    elif problem=='login':result['state']='blocked'
    assert owner.cleanup_source({'approved_url':SOURCE},result)==[] and browser.closed==[]


def test_native_linkedin_popup_requires_exact_opener_before_absence_and_expected_job(owned):
    owner,browser,root=owned
    source=owner.new_tab(SOURCE,purpose='source')
    before=set(browser.tabs)
    destination=browser.new(URL);browser.tabs[destination]['openerId']=source
    result={'state':'destination','application_url':URL,'tab_navigation':{
        'native_apply_clicked':True,'source_target_id':source,'source_url':SOURCE,
        'destination_target_id':destination,'before_target_ids':sorted(before),
        'expected_identity':list(boards.job_identity(URL))}}
    owner.record_popup({'approved_url':SOURCE},result,before)
    assert owner.tabs[destination]['purpose']=='application'
    assert owner.tabs[destination]['creation_proof']=='native_linkedin_apply_opener'
    assert OwnedTabs(browser.helpers(),root).tabs[destination]==owner.tabs[destination]
    assert owner._count(owner.refresh())==2


@pytest.mark.parametrize('problem',['wrong_opener','missing_opener','present_before','wrong_expected_job'])
def test_unproven_popup_is_preserved_and_blocks_more_source_creation(owned,problem):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source');before=set(browser.tabs)
    destination=browser.new(URL);browser.tabs[destination]['openerId']=source
    nav={'native_apply_clicked':True,'source_target_id':source,'source_url':SOURCE,
         'destination_target_id':destination,'before_target_ids':sorted(before),
         'expected_identity':list(boards.job_identity(URL))}
    if problem=='wrong_opener':browser.tabs[destination]['openerId']='user'
    elif problem=='missing_opener':browser.tabs[destination].pop('openerId')
    elif problem=='present_before':nav['before_target_ids'].append(destination)
    else:nav['expected_identity']=list(boards.job_identity(OTHER))
    owner.record_popup({'approved_url':SOURCE},{'state':'destination','application_url':URL,'tab_navigation':nav},before)
    assert destination not in owner.tabs and owner.unclaimed[destination]['state']=='active'
    with pytest.raises(TabCapacityReached):owner.new_tab('https://www.linkedin.com/jobs/view/9876543210/',purpose='source')
    assert browser.closed==[] and destination in browser.tabs
    browser.tabs.pop(destination)
    owner.refresh()
    assert owner.unclaimed[destination]['state']=='departed'


def test_owned_source_same_target_navigation_becomes_preserved_application(owned):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source');before=set(browser.tabs)
    browser.tabs[source]['url']=URL
    nav={'native_apply_clicked':True,'source_target_id':source,'source_url':SOURCE,
         'destination_target_id':source,'before_target_ids':sorted(before),'expected_identity':list(boards.job_identity(URL))}
    owner.record_popup({'approved_url':SOURCE},{'state':'destination','application_url':URL,'tab_navigation':nav},before)
    assert owner.tabs[source]['purpose']=='application' and owner.tabs[source]['job_identity']==list(boards.job_identity(URL))
    assert browser.closed==[]


def test_late_related_popup_blocks_second_native_apply_click_without_claiming_it(owned):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source')
    owner.before_apply_click()
    popup=browser.new(URL);browser.tabs[popup]['openerId']=source;browser.switch(source)
    with pytest.raises(TabCapacityReached):owner.before_apply_click()
    assert popup not in owner.tabs and owner.unclaimed[popup]['state']=='active'
    assert browser.closed==[]


@pytest.mark.parametrize('opener', [True, False])
def test_dispatch_failure_after_native_source_click_retains_popup_and_blocks_more_creation(owned,opener):
    _,browser,root=owned
    failure=RuntimeError('Synthetic wait_for_load failure after native action')
    def runtime(request,helpers):
        source=helpers['new_tab'](SOURCE)
        helpers['jhb_before_apply_click']()
        destination=browser.new(URL)
        if opener:browser.tabs[destination]['openerId']=source
        helpers['jhb_after_apply_click']()
        raise failure
    with pytest.raises(RuntimeError) as caught:
        dispatch_owned({'operation':'resolve','approved_url':SOURCE},browser.helpers(),runtime,
                       dispatcher_name='jhb.applications.linkedin_runtime',root=root)
    assert caught.value is failure
    owner=OwnedTabs(browser.helpers(),root)
    destination=browser.created[-1]
    assert destination not in owner.tabs and owner.unclaimed[destination]['state']=='active'
    with pytest.raises(TabCapacityReached):
        owner.new_tab('https://www.linkedin.com/jobs/view/9876543210/',purpose='source')
    assert browser.closed==[] and 'user' in browser.tabs and destination in browser.tabs


def test_unrelated_popup_with_different_known_opener_is_preserved_without_false_source_backpressure(owned):
    owner,browser,_=owned
    source=owner.new_tab(SOURCE,purpose='source')
    owner.before_apply_click()
    destination=browser.new(URL);browser.tabs[destination]['openerId']='user'
    owner.after_apply_click();owner.observe_unclaimed_popups()
    assert destination not in owner.tabs and destination not in owner.unclaimed
    assert browser.closed==[]


def test_cleanup_dispatch_has_fixed_namespace_and_never_invokes_application_dispatch(owned):
    _,browser,root=owned
    def forbidden(*args):raise AssertionError('Cleanup cannot execute filling or final clicks')
    result=dispatch_owned({'operation':'cleanup_tabs'},browser.helpers(),forbidden,dispatcher_name=MODULE,root=root)
    assert result['closed_targets']==[]
    with pytest.raises(ValueError,match='Unsupported browser dispatcher'):
        dispatch_owned({'operation':'cleanup_tabs'},browser.helpers(),forbidden,dispatcher_name='unknown',root=root)


def test_generated_cli_script_actually_wraps_new_tab_and_capacity_is_distinct(monkeypatch,tmp_path):
    monkeypatch.setattr('jhb.applications.cli_browser.ROOT',tmp_path)
    monkeypatch.setenv('BU_CDP_URL','http://127.0.0.1:12345')
    monkeypatch.setenv('JHB_MAX_OWNED_TABS','1')
    browser=Browser()
    def runtime(request,helpers):
        target=helpers['new_tab'](request['url'])
        return {'target_id':target,'url':request['url']}
    monkeypatch.setattr(cli_runtime,'dispatch',runtime)
    def run(self,script,env,deadline,cancelled):
        capture=io.StringIO();before=list(sys.path)
        try:
            with redirect_stdout(capture):exec(script,browser.helpers())
        finally:sys.path[:]=before
        return subprocess.CompletedProcess(['browser-use'],0,capture.getvalue(),'')
    monkeypatch.setattr(BrowserUseCLI,'_run',run)
    client=BrowserUseCLI()
    client.call('open',url=URL)
    ledger=json.loads((tmp_path/'private/browser-tab-ledger.json').read_text())
    assert list(ledger['tabs'])==browser.created
    with pytest.raises(BrowserCapacityError) as error:client.call('open',url=OTHER)
    assert error.value.condition=='browser_capacity' and error.value.mutation_started is False
    assert client.last_failure['kind']=='browser_capacity'
    assert browser.created==['worker-1'] and browser.closed==[]
