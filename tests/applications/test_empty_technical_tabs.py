"""Empty technical attempts may be parked; any draft or uncertain proof stays."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from jhb import config
from jhb.applications import approvals, boards, capture, historical, overnight, queue, tracking
from jhb.applications.booklet import write_private
from jhb.applications.tab_lifecycle import EMPTY_GREENHOUSE_FORM, OwnedTabs, dispatch_owned
from tests.applications.test_tab_lifecycle import Browser, IMAGE, MODULE

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/123456'
FLAGS = ('has_values', 'checked', 'custom_values', 'attachments', 'unknown_controls', 'unknown_frames',
         'dialogs', 'validation', 'auth_challenge', 'shadow_controls')


def observation():
    return {'schema_version': 1, 'url': URL, 'ready': True, 'known_form': True, 'control_count': 3,
            **{key: False for key in FLAGS}}


@pytest.fixture
def empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.delenv('JHB_MAX_OWNED_TABS', raising=False)
    browser = Browser(); owner = OwnedTabs(browser.helpers(), tmp_path)
    target = owner.new_tab(URL); key = boards.application_hash(URL)
    folder = tmp_path / 'private/applications' / key; folder.mkdir(parents=True)
    shot = folder/'browser.png'; shot.write_bytes(IMAGE)
    owner.tabs[target]['review_directory'] = str(folder); owner.save()
    stamp = int(time.time())
    job = {'url': URL, 'dedupe_hash': key, 'company': 'Synthetic', 'title': 'Software Engineer'}
    packet = {'job': job, 'state': 'failed', 'submitted': False, 'retryable': True, 'error_kind': 'browser_mechanics',
              'filled': [], 'created_at': stamp, 'review_inventory': {'complete': False, 'fields': []}, 'review_questions': [],
              'capture': {'schema_version': 1, 'capture_id': 'a'*32, 'verified': True, 'filename': 'browser.png',
                          'job_hash': key, 'packet_created_at': stamp, 'captured_at': datetime.now(timezone.utc).isoformat(),
                          'method': 'browser_use_cli', 'sha256': hashlib.sha256(IMAGE).hexdigest(), 'target_id': target}}
    assert capture.valid(packet, IMAGE)
    write_private(folder/'packet.json', packet)
    db = tmp_path/'data/jobs.sqlite3'; db.parent.mkdir()
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    conn.executescript(queue.SCHEMA+approvals.SCHEMA+overnight.SCHEMA+tracking.SCHEMA+historical.SCHEMA)
    conn.execute('INSERT INTO applications(job_hash,job_json,state,updated_at,packet,error_kind) VALUES(?,?,?,?,?,?)',
                 (key, json.dumps(job), 'retry', stamp, str(folder/'review.html'), 'browser_mechanics')); conn.commit()
    seen = observation()
    owner.helpers['js'] = lambda expression: dict(seen)
    browser.switch('user')
    yield owner, browser, tmp_path, target, packet, folder, conn, seen
    conn.close()


def test_empty_failed_capture_closes_only_owned_target_preserving_queue_and_packet(empty):
    owner,browser,root,target,packet,folder,conn,seen=empty
    before = tuple(conn.execute('SELECT * FROM applications').fetchone()); original = (folder/'packet.json').read_bytes()
    assert owner.cleanup_empty_technical() == [target]
    assert browser.closed == [target] and browser.current == 'user' and list(browser.tabs) == ['user']
    assert tuple(conn.execute('SELECT * FROM applications').fetchone()) == before
    assert (folder/'packet.json').read_bytes() == original
    saved = json.loads(next((root/'private/browser-empty-attempt-cleanup').glob('*.json')).read_text())
    assert saved['state'] == 'closed' and saved['packet_sha256'] == hashlib.sha256(original).hexdigest()
    assert saved['live_observation']['has_values'] is False and 'url' not in saved['live_observation']
    assert owner.cleanup_empty_technical() == []


@pytest.mark.parametrize('flag', FLAGS)
def test_any_live_value_attachment_consent_or_uncertainty_preserves_tab(empty, flag):
    owner,browser,_,target,_,_,_,seen=empty; seen[flag]=True
    assert owner.cleanup_empty_technical() == [] and target in browser.tabs and browser.closed == []


@pytest.mark.parametrize('change', ['not_ready','unknown_form','missing_flag','no_controls','wrong_url'])
def test_incomplete_live_observation_is_not_empty_proof(empty, change):
    owner,browser,_,target,_,_,_,seen=empty
    if change=='not_ready':seen['ready']=False
    elif change=='unknown_form':seen['known_form']=False
    elif change=='missing_flag':seen.pop('attachments')
    elif change=='no_controls':seen['control_count']=0
    else:seen['url']=URL.replace('123456','654321')
    assert owner.cleanup_empty_technical() == [] and browser.closed == []


@pytest.mark.parametrize('change', ['filled','answered_inventory','answered_question','invalid_inventory','not_technical',
    'not_retryable','submitted','click_started','wrong_capture','changed_image','wrong_identity','missing_capture','wrong_packet_path'])
def test_retained_or_unknown_packet_proof_blocks_cleanup(empty, change):
    owner,browser,_,target,packet,folder,conn,_=empty
    if change=='filled':packet['filled']=[{'value':'synthetic retained answer'}]
    elif change=='answered_inventory':packet['review_inventory']['fields']=[{'status':'answered'}]
    elif change=='answered_question':packet['review_questions']=[{'status':'answered'}]
    elif change=='invalid_inventory':packet.pop('review_inventory')
    elif change=='not_technical':packet['error_kind']='role_review_execution'
    elif change=='not_retryable':packet['retryable']=False
    elif change=='submitted':packet['submitted']=True
    elif change=='click_started':packet['runtime_click_started']=True
    elif change=='wrong_capture':packet['capture']['target_id']='another-tab'
    elif change=='changed_image':(folder/'browser.png').write_bytes(b'changed')
    elif change=='wrong_identity':packet['job']['url']=URL.replace('123456','654321')
    elif change=='missing_capture':packet.pop('capture')
    else:conn.execute("UPDATE applications SET packet='/elsewhere/review.html'");conn.commit()
    write_private(folder/'packet.json',packet)
    assert owner.cleanup_empty_technical() == [] and browser.closed == []


@pytest.mark.parametrize('state', ['running','queued','waiting_review','waiting_input','history_hold','submitted','submission_uncertain'])
def test_protected_queue_states_are_never_parked(empty, state):
    owner,browser,_,_,_,_,conn,_=empty
    conn.execute('UPDATE applications SET state=?',(state,));conn.commit()
    assert owner.cleanup_empty_technical() == [] and not browser.closed


@pytest.mark.parametrize('kind', ['approval','attempt','receipt','approval_file','attempt_file','history','unclaimed_popup','missing_table'])
def test_terminal_history_and_ownership_uncertainty_preserves_tab(empty, kind):
    owner,browser,root,target,packet,_,conn,_=empty;key=packet['job']['dedupe_hash']
    if kind=='approval':
        conn.execute("INSERT INTO application_approvals VALUES('a',?,'expired',1,2,'r','missing',NULL)",(key,))
    elif kind=='attempt':
        conn.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path) VALUES(?,'a',?,'failed',1,2,'missing')",(key,URL))
    elif kind=='receipt':
        conn.execute("INSERT INTO confirmed_submissions VALUES('x','greenhouse',?,?,'date','{}',1)",(URL,json.dumps(packet['job'])))
    elif kind in {'approval_file','attempt_file'}:
        part='application-approvals' if kind=='approval_file' else 'authorized-submissions'
        write_private(root/'private'/part/key/'unpersisted.json',{})
    elif kind=='history':
        conn.execute('INSERT INTO sheet_application_history VALUES(?,?,?,?,?,?,?,?,?,?)',('x','sink',2,json.dumps(['Synthetic','Software Engineer','','','','','',URL]),json.dumps(boards.job_identity(URL),separators=(',',':')),None,'synthetic','/missing','x',1))
    elif kind=='unclaimed_popup':
        browser.tabs['unknown']={'targetId':'unknown','url':URL}
        owner.unclaimed['unknown']={'state':'active'}
    else:conn.execute('DROP TABLE application_approvals')
    conn.commit()
    assert owner.cleanup_empty_technical() == [] and not browser.closed


def test_queue_claim_during_live_read_cancels_closure(empty):
    owner,browser,_,_,_,_,conn,seen=empty
    def inspect(expression):
        conn.execute("UPDATE applications SET state='running'");conn.commit();return dict(seen)
    owner.helpers['js']=inspect
    assert owner.cleanup_empty_technical() == [] and not browser.closed


def test_manual_edit_between_two_native_reads_cancels_closure(empty):
    owner,browser,_,_,_,_,_,seen=empty;calls=[]
    def inspect(expression):
        calls.append(True);return {**seen,'has_values':len(calls)>1}
    owner.helpers['js']=inspect
    assert owner.cleanup_empty_technical() == [] and len(calls)==2 and not browser.closed


def test_packet_edit_during_observation_or_late_queue_claim_cancels_closure(empty):
    owner,browser,_,_,packet,folder,conn,seen=empty;calls=[]
    def inspect(expression):
        calls.append(True)
        if len(calls)==1:
            changed={**packet,'reason':'new retained evidence'};write_private(folder/'packet.json',changed)
        return dict(seen)
    owner.helpers['js']=inspect
    assert owner.cleanup_empty_technical() == [] and not browser.closed
    write_private(folder/'packet.json',packet);calls.clear()
    def claim_late(expression):
        calls.append(True)
        if len(calls)==2:
            conn.execute("UPDATE applications SET state='running'");conn.commit()
        return dict(seen)
    owner.helpers['js']=claim_late
    assert owner.cleanup_empty_technical() == [] and not browser.closed


def test_unconfirmed_empty_close_is_not_replayed(empty):
    owner,browser,root,target,_,_,_,_=empty;browser.close_effect=False
    assert owner.cleanup_empty_technical() == [] and browser.closed == [target]
    assert owner.cleanup_empty_technical() == [] and browser.closed == [target]
    assert owner.tabs[target]['state']=='close_unconfirmed'
    assert json.loads(next((root/'private/browser-empty-attempt-cleanup').glob('*.json')).read_text())['state']=='close_unconfirmed'


def test_dispatch_cleanup_uses_same_guard_and_does_not_enter_filler(empty):
    owner,browser,root,target,_,_,_,seen=empty
    helpers=browser.helpers();helpers['js']=lambda expression:dict(seen)
    result=dispatch_owned({'operation':'cleanup_tabs'},helpers,lambda *a:pytest.fail('Cleanup entered filler'),dispatcher_name=MODULE,root=root)
    assert result['closed_targets']==[target]


@pytest.fixture(scope='module')
def synthetic_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as api:
        browser=api.chromium.launch()
        page=browser.new_page();page.route('**/*',lambda route:route.abort())
        yield page
        browser.close()


@pytest.mark.parametrize('extra,flag', [
    ('<input value="manual edit">','has_values'),
    ('<input style="display:none" value="hidden retained answer">','has_values'),
    ('<input type="checkbox" checked>','checked'),
    ('<span class="select__single-value">Synthetic choice</span>','custom_values'),
    ('<div class="file-upload__filename">Synthetic Resume.pdf</div>','attachments'),
    ('<div role="combobox">Choose</div>','unknown_controls'),
    ('<iframe src="https://www.recaptcha.net/recaptcha/api2/bframe"></iframe>','unknown_frames'),
    ('<dialog open>Candidate edit modal</dialog>','dialogs'),
    ('<input aria-invalid="true">','validation'),
    ('<input type="password">','auth_challenge'),
])
def test_native_probe_detects_real_dom_content_without_changing_it(synthetic_page,extra,flag):
    page=synthetic_page
    page.set_content('<form id="application-form" class="application--form"><input id="first_name"><input id="last_name"><input id="email">'+extra+'</form>')
    before=page.content();observed=page.evaluate(EMPTY_GREENHOUSE_FORM)
    assert observed['known_form'] is True and observed[flag] is True
    assert page.content()==before


def test_native_probe_allows_only_genuinely_empty_known_form_and_idle_badge(synthetic_page):
    page=synthetic_page
    page.set_content('<form id="application-form" class="application--form"><input id="first_name"><input id="last_name"><input id="email"></form><div class="grecaptcha-badge"><iframe src="https://www.recaptcha.net/recaptcha/api2/anchor"></iframe></div>')
    observed=page.evaluate(EMPTY_GREENHOUSE_FORM)
    assert observed['known_form'] is True and observed['control_count']==3
    assert all(observed[key] is False for key in FLAGS)


def test_hidden_native_file_selection_is_still_retained_work(synthetic_page):
    page=synthetic_page
    page.set_content('<form id="application-form" class="application--form"><input id="first_name"><input id="last_name"><input id="email"><input type="file" id="resume" style="display:none"></form>')
    page.locator('#resume').set_input_files({'name':'Synthetic Resume.txt','mimeType':'text/plain','buffer':b'synthetic fixture'})
    assert page.evaluate(EMPTY_GREENHOUSE_FORM)['attachments'] is True
    assert page.locator('#resume').evaluate('(e)=>e.files.length')==1
