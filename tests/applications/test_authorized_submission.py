"""Finite-authority regression tests; all candidates and Chromium tabs synthetic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest
from pypdf import PdfWriter

from jhb import config
from jhb.applications.authorized_submission import (
    SCOPE, AuthorizedSubmissionCLI, document_manifest, load_gate, submit_reviewed,
)
from jhb.applications.booklet import answer, write_private
from jhb.applications.browser import GUARD_SCRIPT
from jhb.applications.cli_browser import BrowserOperationError
from jhb.applications.cli_runtime import dispatch as prepare_dispatch
from jhb.applications.queue import greenhouse_identity
from jhb.applications.submission_runtime import dispatch

BROWSERS = str(config.ROOT / ".local-browsers")
URL = "https://job-boards.greenhouse.io/synthetic-submission/jobs/1234"
JOB_HASH = hashlib.sha256("|".join(greenhouse_identity(URL)).encode()).hexdigest()

HTML = '''<!doctype html><html lang="en"><title>Synthetic approved application</title>
<style>input{margin:6px}.select__value-container{margin:6px}button{margin:10px;padding:8px}</style>
<form id="application">
<label for="first_name">First Name</label><input id="first_name" required value="Synthetic">
<div class="phone-input"><div class="iti">
<label for="phone">Phone</label><input id="phone" type="tel" required value="5551234567">
<label id="country-label">Country</label><div class="select__value-container"><div class="select__single-value"><div class="iti__flag iti__us"></div><span>+1</span></div>
<input id="country" role="combobox" aria-labelledby="country-label" aria-required="true"></div></div></div>
<label id="gender-label">Gender</label><div class="select__value-container"><span class="select__multi-value__label">Male</span>
<input id="gender" role="combobox" aria-labelledby="gender-label" aria-required="true" aria-controls="gender-options" aria-expanded="false" onfocus="openGender()" onkeydown="if(event.key==='ArrowDown')openGender()"><div id="gender-options" role="listbox" hidden></div></div>
<label for="privacy">I agree to the privacy policy</label><input id="privacy" type="checkbox" required checked>
<label id="school0">School</label><div class="select__value-container"><span class="select__single-value">Synthetic University</span><input id="school--0" role="combobox" aria-labelledby="school0" aria-required="true"></div>
<label id="school1">School</label><div class="select__value-container"><span class="select__single-value">Synthetic College</span><input id="school--1" role="combobox" aria-labelledby="school1" aria-required="true"></div>
<label for="end-year--0">End date year</label><input id="end-year--0" type="number" value="2027">
<label for="end-year--1">End date year</label><input id="end-year--1" type="number" value="2024">
<div class="file-upload" role="group" aria-labelledby="resume-label" aria-required="true"><div class="upload-label" id="resume-label">Resume/CV*</div>
<input id="resume" type="file" hidden onchange="uploaded(this)"></div>
<label for="optional">Optional unapproved question</label><input id="optional">
<button type="submit">Submit application</button></form>
<script>
window.submissions=0;window.trusted=false;window.mode='success';
window.genderCatalog=['Male','Female','Decline to self identify'];window.genderSelections=0;
function openGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
list.replaceChildren();for(const label of window.genderCatalog){const option=document.createElement('div');option.role='option';option.textContent=label;
option.onclick=()=>{window.genderSelections++;control.parentElement.querySelector('.select__multi-value__label').textContent=label;closeGender()};list.appendChild(option)}
list.hidden=false;control.setAttribute('aria-expanded','true')}
function closeGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
if(list)list.hidden=true;if(control)control.setAttribute('aria-expanded','false')}
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeGender()});
function uploaded(input){const file=input.files[0],group=input.parentElement;input.remove();
const wrapper=document.createElement('div');wrapper.className='file-upload__filename';
const name=document.createElement('p');name.textContent=file.name;wrapper.appendChild(name);
const remove=document.createElement('button');remove.type='button';remove.textContent='Remove file';
remove.onclick=()=>{wrapper.remove();const next=document.createElement('input');next.id='resume';next.type='file';next.hidden=true;next.onchange=()=>uploaded(next);group.appendChild(next)};
wrapper.appendChild(remove);group.appendChild(wrapper)}
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++;window.trusted=e.isTrusted;
if(window.mode==='success'){e.target.remove();document.body.append('Thank you for applying. We have received your application.')}
if(window.mode==='verify'){e.target.remove();document.body.append('Verification code: check your inbox')}
if(window.mode==='ambiguous'){document.body.append('Unable to complete this request')}
};
</script></html>'''


def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "1")
    now = datetime.now(timezone.utc)
    auth = {"role": "user", "status": "verified", "enabled": True, "scope": SCOPE, "board": "greenhouse",
            "authorized_at": (now-timedelta(minutes=5)).isoformat(), "expires_at": (now+timedelta(hours=2)).isoformat(),
            "content": "Keep submitting new Phase 1 Greenhouse jobs tonight after double checking.",
            "require_browser_double_check": True, "pause_unknown_answers": True, "require_receipt_before_sheet": True}
    auth_path = tmp_path / "private" / "authorization.json"
    write_private(auth_path, auth)
    job = {"dedupe_hash": JOB_HASH, "url": URL, "company": "Synthetic Company", "title": "Synthetic Engineer"}
    resume = tmp_path / "synthetic-sde.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792); writer.write(resume)
    values = [("first_name", "First Name", "identity.first_name", "Synthetic"),
              ("phone", "Phone", "identity.phone", "+15551234567"),
              ("country", "Country", "identity.country", "United States"),
              ("gender", "Gender", "disclosure.gender", "Male"),
              ("privacy", "I agree to the privacy policy", "consent.privacy", True),
              ("school--0", "School (education record 1)", "education.0.school", "Synthetic University"),
              ("school--1", "School (education record 2)", "education.1.school", "Synthetic College"),
              ("end-year--0", "End date year", "education.0.end_year", "2027"),
              ("end-year--1", "End date year", "education.1.end_year", "2024"),
              ("resume", "Resume/CV", "documents.resume", str(resume))]
    filled = [{"ref": ref, "question": label, "key": key, "value": value, "source": "Synthetic explicitly approved fact"}
              for ref, label, key, value in values]
    packet = {"job": job, "state": "waiting_review", "submitted": False, "missing": [], "filled": filled}
    packet_path = tmp_path / "private" / "applications" / JOB_HASH / "packet.json"
    write_private(packet_path, packet)
    attempt = {"job_hash": JOB_HASH, "application_url": URL, "authorization_id": hashlib.sha256(auth_path.read_bytes()).hexdigest(),
               "authorization_path": str(auth_path), "started_at": now.isoformat(), "state": "in_progress",
               "packet_path": str(packet_path), "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()}
    attempt_path = tmp_path / "private" / "authorized-submissions" / JOB_HASH / "attempt.json"
    write_private(attempt_path, attempt)
    manifest = {"selected_role": "sde", "documents": {"documents.resume": answer(str(resume), "Synthetic chosen SDE resume")},
                "filled": filled, "approved_phone_national": answer("5551234567", "Latest explicit split-phone formatting policy")}
    authorization = {**auth, "authorization_path": str(auth_path), "authorization_id": attempt["authorization_id"]}
    return job, packet, packet_path, manifest, authorization, attempt_path


def test_cached_prior_application_blocks_before_any_browser_submission(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    from jhb.applications import historical
    monkeypatch.setattr(historical, "cached_match", lambda current: {"disposition": "hold"})
    cli = AuthorizedSubmissionCLI()
    async def prohibited(*args, **kwargs):
        pytest.fail("Duplicate application must not access the browser")
    monkeypatch.setattr(cli, "invoke", prohibited)
    result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
    assert result["state"] == "waiting_review" and result["click_started"] is False
    assert json.loads(attempt_path.read_text()).get("runtime_click_started") is not True


@pytest.mark.parametrize("history", [
    {"state": "blocked", "match": {"disposition": "exclude"}},
    {"state": "pending", "reason": "busy"},
])
def test_fresh_manual_history_check_stops_terminal_action_after_audit(tmp_path, monkeypatch, history):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    from jhb.applications import historical
    checked = []
    def refresh(current):
        checked.append(current)
        return history
    monkeypatch.setattr(historical, "refresh_before_submit", refresh)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        cli = AuthorizedSubmissionCLI()
        monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "waiting_review" and result["click_started"] is False
        assert inspect("window.submissions") == 0
        assert checked == [job]
        assert json.loads(attempt_path.read_text()).get("runtime_click_started") is not True


@contextmanager
def synthetic_runtime(*, html=HTML, url=URL):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    with ThreadPoolExecutor(max_workers=1) as lane:
        def start():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start(); browser = pw.chromium.launch(); page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page.goto(url)
            other = browser.new_page(); other.set_content("<form><button>Submit application</button></form>"); other.evaluate(GUARD_SCRIPT)
            session = page.context.new_cdp_session(page)
            target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            def switch(owned):
                assert owned == target
            helpers = {"cdp": lambda method, **params: session.send(method, params), "js": page.evaluate,
                       "wait": lambda seconds: page.wait_for_timeout(1 if seconds == .25 else seconds*1000),
                       "click_at_xy": page.mouse.click, "list_tabs": lambda: [{"targetId": target, "url": url}],
                       "switch_tab": switch, "activate_tab": lambda owned: (switch(owned), page.bring_to_front()),
                       "current_tab": lambda: {"targetId": target, "url": page.url}}
            return pw, browser, page, other, helpers, target
        pw, browser, page, other, helpers, target = lane.submit(start).result()
        def call(request):
            return lane.submit(dispatch, request, helpers).result()
        async def invoke(operation, **payload):
            if operation != "locate":
                payload.update(target_id=target, expected_url=url)
            return await asyncio.wrap_future(lane.submit(dispatch, {"operation": operation, **payload}, helpers))
        def inspect(expression):
            return lane.submit(page.evaluate, expression).result()
        def other_guard():
            return lane.submit(other.evaluate, "window.__jhbGuard").result()
        try:
            yield call, invoke, inspect, helpers, target, other_guard, lane
        finally:
            lane.submit(browser.close).result(); lane.submit(pw.stop).result()


@pytest.mark.parametrize("invalid", ["disabled", "expired", "long_window", "unverified", "no_user", "wrong_scope", "no_content", "env_off", "consumed", "changed_packet", "wrong_job", "world_readable"])
def test_authorization_or_attempt_failure_happens_before_any_browser_call(tmp_path, monkeypatch, invalid):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    authority = json.loads(open(auth["authorization_path"]).read())
    attempt = json.loads(attempt_path.read_text())
    if invalid == "disabled": authority["enabled"] = False
    elif invalid == "expired": authority["expires_at"] = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    elif invalid == "long_window": authority["expires_at"] = (datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    elif invalid == "unverified": authority["status"] = "needs_input"
    elif invalid == "no_user": authority["role"] = "assistant"
    elif invalid == "wrong_scope": authority["scope"] = "all applications forever"
    elif invalid == "no_content": authority["content"] = "Do not submit jobs tonight"
    elif invalid == "env_off": monkeypatch.delenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED")
    elif invalid == "consumed": attempt["runtime_click_started"] = True
    elif invalid == "changed_packet": write_private(packet_path, {**packet, "missing": [{"question": "New fact"}]})
    elif invalid == "wrong_job": attempt["application_url"] = URL.replace("1234", "5678")
    write_private(type(attempt_path)(auth["authorization_path"]), authority)
    attempt["authorization_id"] = hashlib.sha256(type(attempt_path)(auth["authorization_path"]).read_bytes()).hexdigest()
    write_private(attempt_path, attempt)
    if invalid == "world_readable": attempt_path.chmod(0o644)
    cli = AuthorizedSubmissionCLI()
    async def prohibited(*args, **kwargs): pytest.fail("Browser must not run without valid authority")
    monkeypatch.setattr(cli, "invoke", prohibited)
    result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
    assert result["state"] == ("uncertain" if invalid == "consumed" else "waiting_review")


@pytest.mark.parametrize("receipt_text", ["Thank you for applying. We have received your application.",
                                          "We wanted to let you know we received your application, and we are delighted.",
                                          "Your application has been received."])
def test_authorized_fresh_cli_double_checks_then_native_submits_and_persists_receipt(tmp_path, monkeypatch, receipt_text):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("document.getElementById('application').addEventListener('submit',()=>{document.body.innerText="+json.dumps(receipt_text)+"})")
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "submitted"
        assert result["check_count"] == 2
        receipt = json.loads(type(attempt_path)(result["receipt_path"]).read_text())
        assert receipt["authorization_id"] == auth["authorization_id"]
        assert receipt["url"] == URL and receipt["check_count"] == 2
        assert receipt["confirmation"] in receipt["body"]
        assert receipt["source"] == "Live Browser Use CLI success page"
        checks = json.loads(type(attempt_path)(result["checks_path"]).read_text())
        assert len(checks["checks"]) == 2
        assert checks["checks"][0] == checks["checks"][1]
        assert checks["documents"]["documents.resume"]["sha256"] == hashlib.sha256(type(attempt_path)(manifest["documents"]["documents.resume"]["value"]).read_bytes()).hexdigest()
        assert inspect("window.submissions") == 1
        assert inspect("window.trusted") is True
        assert inspect("window.genderSelections") == 0  # Catalog inspection never chooses a disclosure.
        assert inspect("window.__jhbGuard") is True
        assert other_guard() is True
        assert json.loads(attempt_path.read_text())["runtime_click_started"] is True
        # An interrupted/restarted manager can inspect the receipt but never click again.
        repeated = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert repeated["state"] == "uncertain"
        assert inspect("window.submissions") == 1


@pytest.mark.parametrize("catalog", [[], ["Female", "Decline to self identify"]])
def test_missing_or_incompatible_owned_demographic_catalog_blocks_before_upload_and_click(tmp_path, monkeypatch, catalog):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("window.genderCatalog=" + json.dumps(catalog))
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "waiting_review"
        assert inspect("window.submissions") == 0
        assert inspect("window.genderSelections") == 0
        assert inspect("document.querySelector('#gender').parentElement.querySelector('.select__multi-value__label').textContent") == "Male"
        assert inspect("document.querySelector('#resume').files.length") == 0
        assert inspect("window.__jhbGuard") is True
        assert not json.loads(attempt_path.read_text()).get("runtime_click_started")
        assert not (attempt_path.parent / "receipt.json").exists()


@pytest.mark.parametrize("change", ["new_required", "changed_answer", "education_missing", "document_bytes", "multiple_drafts"])
def test_fresh_retained_audit_blocks_stale_or_incomplete_drafts_without_click(tmp_path, monkeypatch, change):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        if change == "new_required": inspect("document.getElementById('optional').required=true")
        elif change == "changed_answer": inspect("document.getElementById('first_name').value='Changed'")
        elif change == "education_missing": inspect("document.getElementById('school--1').closest('.select__value-container').remove()")
        elif change == "document_bytes":
            altered = type(attempt_path)(manifest["documents"]["documents.resume"]["value"]); altered.write_bytes(b"not a PDF")
        else: helpers["list_tabs"] = lambda: [{"url": URL, "targetId": target}, {"url": URL, "targetId": "ambiguous-draft"}]
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == ("waiting_input" if change == "new_required" else "waiting_review")
        if change == "new_required":
            assert result["missing"][0]["question"] == "Optional unapproved question"
        assert inspect("window.submissions") == 0
        assert not json.loads(attempt_path.read_text()).get("runtime_click_started")
        assert not (attempt_path.parent / "receipt.json").exists()
        assert other_guard() is True


@pytest.mark.parametrize("mode", ["verify", "ambiguous"])
def test_postclick_challenge_or_absent_receipt_is_uncertain_and_never_replayed(tmp_path, monkeypatch, mode):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("window.mode="+json.dumps(mode))
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "uncertain"
        assert result["click_started"] is True
        if mode == "verify": assert result["handoff"] == "waiting_login"
        assert inspect("window.submissions") == 1
        assert not (attempt_path.parent / "receipt.json").exists()
        assert inspect("window.__jhbGuard") is True and other_guard() is True


def test_identical_native_and_attached_cover_letter_packet_records_are_scoped_and_uploaded_once(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    resume = type(attempt_path)(manifest["documents"]["documents.resume"]["value"])
    cover = tmp_path / "synthetic-cover.pdf"; cover.write_bytes(resume.read_bytes())
    original = {"ref": "cover_letter", "question": "Cover Letter", "key": "documents.cover_letter",
                "value": str(cover), "source": "Synthetic approved role-specific cover letter"}
    packet["filled"].extend([original, {**original, "ref": "uploaded:Cover Letter"}])
    write_private(packet_path, packet)
    attempt = json.loads(attempt_path.read_text()); attempt["packet_sha256"] = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    write_private(attempt_path, attempt)
    manifest["filled"] = packet["filled"]
    manifest["documents"]["documents.cover_letter"] = answer(str(cover), original["source"])
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("""(()=>{const original=document.getElementById('resume').closest('.file-upload'),group=original.cloneNode(true);
        group.setAttribute('aria-labelledby','cover-label');group.setAttribute('aria-required','false');
        group.querySelector('.upload-label').id='cover-label';group.querySelector('.upload-label').textContent='Cover Letter';
        const input=group.querySelector('input');input.id='cover_letter';input.onchange=()=>uploaded(input);
        original.after(group)})()""")
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "submitted"
        checks = json.loads(type(attempt_path)(result["checks_path"]).read_text())
        assert [f["answer_key"] for f in checks["checks"][0]["fields"]].count("documents.cover_letter") == 1
        assert inspect("window.submissions") == 1


@pytest.mark.parametrize("interruption", ["between_checks", "during_geometry", "authorization_revoked", "upload_receipt_mutated", "candidate_discarded"])
def test_final_check_races_or_revocation_keep_guard_and_never_consume_attempt(tmp_path, monkeypatch, interruption):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (call, invoke, inspect, helpers, target, other_guard, lane):
        original_wait, original_cdp = helpers["wait"], helpers["cdp"]
        armed = {"value": False, "done": False}
        def interrupt():
            if armed["done"]: return
            armed["done"] = True
            if interruption == "authorization_revoked":
                authority = json.loads(type(attempt_path)(auth["authorization_path"]).read_text())
                authority["enabled"] = False; write_private(type(attempt_path)(auth["authorization_path"]), authority)
            elif interruption == "candidate_discarded":
                write_private(config.ROOT / "private" / "application-discards" / (job["dedupe_hash"] + ".json"),
                              {"source": "candidate_portal_discard", "job_hash": job["dedupe_hash"], "state": "discarded"})
            elif interruption == "upload_receipt_mutated":
                helpers["js"]("document.querySelector('.file-upload__filename p').textContent='replaced.pdf'")
            else:
                helpers["js"]("document.getElementById('first_name').value='Changed during submission'")
        def wait(seconds):
            original_wait(seconds)
            if armed["value"] and seconds == .15 and interruption != "during_geometry": interrupt()
        def cdp(method, **params):
            result = original_cdp(method, **params)
            if armed["value"] and method == "DOM.getBoxModel" and interruption == "during_geometry": interrupt()
            return result
        helpers["wait"], helpers["cdp"] = wait, cdp
        async def interrupted_invoke(operation, **payload):
            if operation == "submit": armed["value"] = True
            return await invoke(operation, **payload)
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", interrupted_invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert result["state"] == "waiting_review"
        assert armed["done"] is True
        assert inspect("window.submissions") == 0
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        assert not json.loads(attempt_path.read_text()).get("runtime_click_started")
        assert not (attempt_path.parent / "receipt.json").exists()


@pytest.mark.parametrize("error,expected_retry", [(TimeoutError("Synthetic timeout"), True),
    (ConnectionError("Synthetic disconnected transport"), True), (FileNotFoundError("Synthetic missing CLI"), True),
    (BrowserOperationError("Synthetic recoverable geometry", retryable=True), True),
    (BrowserOperationError("Synthetic unsupported mechanics", retryable=False), False),
    (ValueError("Synthetic authorization mismatch"), False), (RuntimeError("Synthetic unknown error"), False)])
@pytest.mark.parametrize("clicked", [False, True])
def test_retries_are_explicitly_limited_to_known_transport_failure_before_click(tmp_path, monkeypatch, error, expected_retry, clicked):
    job, packet, packet_path, manifest, auth, attempt_path = evidence(tmp_path, monkeypatch)
    cli = AuthorizedSubmissionCLI()
    async def fail(operation, **payload):
        if clicked:
            attempt = json.loads(attempt_path.read_text()); attempt["runtime_click_started"] = True
            write_private(attempt_path, attempt)
        raise error
    monkeypatch.setattr(cli, "invoke", fail)
    result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
    assert result["state"] == ("uncertain" if clicked else "waiting_review")
    assert result["retryable"] is (expected_retry and not clicked)
    assert result["click_started"] is clicked
    assert not (attempt_path.parent / "receipt.json").exists()


ASHBY_URL = "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555/application"
ASHBY_HTML = '''<!doctype html><html><title>Synthetic Ashby application</title>
<style>label{display:block;padding:5px}input{margin:4px}button{padding:7px;margin:6px}.hidden-choice{position:absolute;opacity:0;width:0;height:0}</style>
<div class="ashby-application-form-container" id="application"><div class="ashby-application-form-section-container">
<div data-field-path="name"><div class="ashby-application-form-question-title">Name*</div><input id="name" required value="Synthetic Candidate"></div>
<div data-field-path="location"><div class="ashby-application-form-question-title">Current Location*</div><input role="combobox" aria-expanded="false" required value="San Diego, California, United States"></div>
<div data-field-path="graduation"><div class="ashby-application-form-question-title">Graduation date*</div><input required value="2026-12-14"></div>
<div data-field-path="authorized"><div class="ashby-application-form-question-title">Authorized to work?*</div>
<label for="yes">Yes</label><input class="hidden-choice" id="yes" name="auth" type="radio" value="yes" required checked>
<label for="no">No</label><input class="hidden-choice" id="no" name="auth" type="radio" value="no" required></div>
<div data-field-path="sponsor"><div class="ashby-application-form-question-title">Need sponsorship?*</div>
<button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" aria-pressed="true">Yes</button>
<button type="button" class="ashby-application-form-input-yesno-option" data-option="no" aria-pressed="false">No</button></div>
<div data-field-path="race"><div class="ashby-application-form-question-title">Race</div>
<label for="asian">Asian</label><input class="hidden-choice" id="asian" type="checkbox" value="asian" checked>
<label for="white">White</label><input class="hidden-choice" id="white" type="checkbox" value="white"></div>
<div data-field-path="resume"><div class="ashby-application-form-question-title">Resume*</div><input id="_systemfield_resume" type="file" required hidden></div>
<div data-field-path="optional"><div class="ashby-application-form-question-title">Optional new question</div><input id="optional"></div>
</div><button type="button" id="submit">Submit application</button></div>
<button type="button">Submit application</button>
<script>window.submissions=0;window.trusted=false;window.mode='success';
window.genderCatalog=['Male','Female','Decline to self identify'];window.genderSelections=0;
function openGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
list.replaceChildren();for(const label of window.genderCatalog){const option=document.createElement('div');option.role='option';option.textContent=label;
option.onclick=()=>{window.genderSelections++;control.parentElement.querySelector('.select__multi-value__label').textContent=label;closeGender()};list.appendChild(option)}
list.hidden=false;control.setAttribute('aria-expanded','true')}
function closeGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
if(list)list.hidden=true;if(control)control.setAttribute('aria-expanded','false')}
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeGender()});
document.getElementById('submit').onclick=e=>{window.submissions++;window.trusted=e.isTrusted;
if(window.mode==='success'){document.getElementById('application').remove();document.body.append('Thank you for applying. We have received your application.')}
if(window.mode==='verify'){document.getElementById('application').remove();document.body.append('Verification code: check your inbox')}
};</script></html>'''


def ashby_evidence(tmp_path, monkeypatch):
    from jhb.applications import boards, overnight
    original = evidence(tmp_path, monkeypatch)
    job, _, _, manifest, authority, _ = original
    monkeypatch.setitem(boards.ADAPTERS["ashby"], "submit_enabled", True)
    auth = {**authority, "scope": overnight.MULTI_SCOPE, "boards": ["greenhouse", "ashby", "workable", "workday"],
            "candidate_job_policy": overnight.MULTI_JOB_POLICY, "require_independent_review": True,
            "content": "Continue applying and submit everything on all job boards for seven hours"}
    auth.pop("board", None)
    auth.pop("authorization_id", None)
    write_private(type(original[-1])(auth["authorization_path"]), auth)
    auth["authorization_id"] = hashlib.sha256(type(original[-1])(auth["authorization_path"]).read_bytes()).hexdigest()
    key = boards.application_hash(ASHBY_URL)
    job = {**job, "dedupe_hash": key, "url": ASHBY_URL}
    resume = manifest["documents"]["documents.resume"]["value"]
    fields = [("name", "Name*", "identity.full_name", "Synthetic Candidate"),
              ("ashby:location:control:0", "Current Location*", "custom.location", {"query": "San Diego", "choice": "San Diego, California, United States"}),
              ("ashby:graduation:control:0", "Graduation date*", "custom.graduation", "2026-12-14"),
              ("ashby:authorized", "Authorized to work?*", "custom.auth", True),
              ("ashby:sponsor", "Need sponsorship?*", "custom.sponsor", True),
              ("ashby:race", "Race", "disclosure.race", ["Asian"]),
              ("_systemfield_resume", "Resume*", "documents.resume", resume)]
    filled = [{"ref": ref, "question": label, "key": key, "value": value, "source": "Synthetic explicitly approved fact"}
              for ref, label, key, value in fields]
    packet = {"job": job, "state": "waiting_review", "submitted": False, "missing": [], "filled": filled}
    packet_path = tmp_path / "private" / "applications" / key / "packet.json"
    write_private(packet_path, packet)
    attempt = {"job_hash": key, "application_url": ASHBY_URL, "authorization_id": auth["authorization_id"],
               "authorization_path": auth["authorization_path"], "started_at": datetime.now(timezone.utc).isoformat(),
               "state": "in_progress", "require_independent_review": True, "runtime_click_started": False, "packet_path": str(packet_path),
               "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()}
    attempt_path = tmp_path / "private" / "authorized-submissions" / key / "attempt.json"
    write_private(attempt_path, attempt)
    write_private(tmp_path / "private" / "reviewer-booklet.json", {"synthetic": "verified profile snapshot"})
    return job, packet, packet_path, {**manifest, "filled": filled}, auth, attempt_path


def independent_approval(job, manifest, checks, auth):
    from jhb.applications.application_review import snapshot_digest
    path = config.ROOT / "private" / "reviewer-booklet.json"
    return {"verdict": "approved", "issues": [], "reviewer": "codex-readonly", "source": "independent_application_review",
            "approved_book_path": str(path), "approved_book_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "snapshot_sha256": snapshot_digest(checks), "authorization_id": auth["authorization_id"], "job_hash": job["dedupe_hash"]}


def test_ashby_grouped_native_audit_independent_review_and_one_shot_terminal_receipt(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (call, invoke, inspect, helpers, target, other_guard, lane):
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == "submitted", result
        assert result["check_count"] == 2 and inspect("window.submissions") == 1
        assert inspect("window.trusted") is True and inspect("window.__jhbGuard") is True and other_guard() is True
        receipt = json.loads(type(attempt_path)(result["receipt_path"]).read_text())
        assert receipt["board"] == "ashby" and receipt["url"] == ASHBY_URL
        assert receipt["resume_sha256"] == hashlib.sha256(type(attempt_path)(manifest["documents"]["documents.resume"]["value"]).read_bytes()).hexdigest()
        assert (attempt_path.parent / "independent-review.json").is_file()
        from jhb.applications import overnight
        record = {"job_hash": job["dedupe_hash"], "authorization_id": auth["authorization_id"],
                  "application_url": job["url"], "attempt_path": str(attempt_path)}
        assert overnight._checked_receipt(record, receipt) is True
        for changes in ({"resume_sha256": "0" * 64}, {"document_sha256": {}},
                        {"independent_review_sha256": "0" * 64}, {"check_count": 1}):
            assert overnight._checked_receipt(record, {**receipt, **changes}) is False
        repeated = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                              cli=cli, reviewer=independent_approval))
        assert repeated["state"] == "uncertain" and inspect("window.submissions") == 1


@pytest.mark.parametrize("valid_panel", [True, False])
def test_ashby_submit_sibling_requires_exact_application_panel_and_all_owned_fields(tmp_path, monkeypatch, valid_panel):
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    html = ASHBY_HTML.replace('class="ashby-application-form-container" id="application"',
                             'id="form" role="tabpanel"' if valid_panel else 'id="form" role="region"')
    html = html.replace('id="submit">', 'id="submit" class="ashby-application-form-submit-button">')
    html = html.replace("getElementById('application')", "getElementById('form')")
    with synthetic_runtime(html=html, url=ASHBY_URL) as (_, invoke, inspect, _, _, _, _):
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == ("submitted" if valid_panel else "waiting_review")
        assert inspect("window.submissions") == int(valid_panel)


@pytest.mark.parametrize("changed_question", [False, True])
def test_portal_click_binds_all_questions_and_rejects_new_optional_field_before_submit(tmp_path, monkeypatch, changed_question):
    import sqlite3
    from jhb.applications import approvals, queue, manual_runtime
    job, packet, packet_path, manifest, _, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    job["role_classes"] = "swe"
    bookpath = tmp_path / "private" / "reviewer-booklet.json"
    write_private(bookpath, {"schema_version": 1, "answers": {}, "roles": {"sde": manifest["documents"], "ml": {}}})
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (_, invoke, inspect, helpers, target, _, lane):
        scope = manual_runtime.application_scope(ASHBY_URL, "ashby")
        snapshot = lane.submit(lambda: manual_runtime.dispatch({"operation": "observe", "target_id": target,
                              "expected_url": ASHBY_URL, "scope": scope}, helpers)).result()
        answered = {r["ref"] for r in packet["filled"]}
        packet["review_inventory"] = {"complete": True, "fields": [
            {"ref": f["ref"], "question": f["label"], "type": f["type"], "required": f["required"],
             "status": "answered" if f["ref"] in answered else "blank"} for f in snapshot["fields"]]}
        write_private(packet_path, packet)
        packet_path.with_name("browser.png").write_bytes(b"\x89PNG\r\n\x1a\nSynthetic review screenshot")
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; queue.initialize(conn)
        conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'waiting_review',1,?)",
                     (job["dedupe_hash"], json.dumps(job), str(packet_path))); conn.commit()
        monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1"); monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
        view = approvals.review(conn, job["dedupe_hash"], bookpath)
        assert view["can_approve"], view
        approved = approvals.approve(conn, job["dedupe_hash"], view["revision"], [f["ref"] for f in view["blank_questions"]], bookpath)
        authpath = conn.execute("SELECT authorization_path FROM application_approvals WHERE approval_id=?", (approved["approval_id"],)).fetchone()[0]
        from jhb.applications import overnight
        auth = overnight.load_authorization(authpath)
        attempt = json.loads(attempt_path.read_text())
        attempt.update(authorization_path=authpath, authorization_id=auth["authorization_id"], authorization_scope=overnight.PORTAL_SCOPE,
                       packet_sha256=hashlib.sha256(packet_path.read_bytes()).hexdigest(), started_at=datetime.now(timezone.utc).isoformat())
        write_private(attempt_path, attempt)
        if changed_question:
            inspect("document.querySelector('.ashby-application-form-section-container').insertAdjacentHTML('beforeend','<div data-field-path=extra><div class=ashby-application-form-question-title>Another optional essay</div><textarea id=extra></textarea></div>')")
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == ("waiting_review" if changed_question else "submitted"), result
        assert inspect("window.submissions") == (0 if changed_question else 1)
        conn.close()


@pytest.mark.parametrize("change", ["review_reject", "new_required", "radio_changed", "multiselect_changed", "location_query", "receipt_changed", "changed_after_review", "book_changed_during_review", "book_changed_after_review", "review_revoked", "paused_before_press", "quarantined_before_press"])
def test_ashby_unaudited_controls_or_review_mismatch_keep_terminal_guard(tmp_path, monkeypatch, change):
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (call, invoke, inspect, helpers, target, other_guard, lane):
        if change == "new_required": inspect("document.getElementById('optional').required=true")
        elif change == "radio_changed": inspect("document.getElementById('no').checked=true")
        elif change == "multiselect_changed": inspect("document.getElementById('white').checked=true")
        elif change == "location_query": inspect("document.querySelector('[role=combobox]').setAttribute('aria-expanded','true')")
        def reviewer(*args):
            if change == "review_reject": return {"verdict": "reject"}
            if change == "changed_after_review": inspect("document.getElementById('name').value='Changed'")
            if change == "receipt_changed": inspect("delete document.getElementById('_systemfield_resume').__jhbUploadReceipt")
            approved = independent_approval(*args)
            if change == "book_changed_during_review":
                write_private(config.ROOT / "private" / "reviewer-booklet.json", {"synthetic": "different approved facts"})
            return approved
        original_wait = helpers["wait"]
        armed = {"value": False, "done": False}
        def wait(seconds):
            if change in {"paused_before_press", "quarantined_before_press"} and armed["value"] and not armed["done"] and seconds == .15:
                marker = (config.ROOT / "private" / "pipeline-pause.json" if change == "paused_before_press"
                          else config.ROOT / "private" / "overnight-monitor" / "repair-pending.json")
                write_private(marker, {"reason": "Synthetic user pause or repair quarantine"})
                armed["done"] = True
            if change == "review_revoked" and armed["value"] and not armed["done"] and seconds == .15:
                path = attempt_path.parent / "independent-review.json"
                write_private(path, {**json.loads(path.read_text()), "verdict": "reject"})
                armed["done"] = True
            original_wait(seconds)
        helpers["wait"] = wait
        async def wrapped(operation, **payload):
            if operation == "submit":
                armed["value"] = True
                if change == "book_changed_after_review":
                    write_private(config.ROOT / "private" / "reviewer-booklet.json", {"synthetic": "changed after approval"})
            return await invoke(operation, **payload)
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", wrapped)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=reviewer))
        assert result["state"] in {"waiting_input", "waiting_review"}, result
        assert inspect("window.submissions") == 0 and inspect("window.__jhbGuard") is True and other_guard() is True
        assert json.loads(attempt_path.read_text())["runtime_click_started"] is False


def test_pause_after_terminal_click_preserves_positive_receipt_and_reconciliation(tmp_path, monkeypatch):
    from jhb.applications import overnight
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (_, invoke, inspect, helpers, _, other_guard, _):
        original_wait = helpers["wait"]
        paused = [False]
        def wait(seconds):
            if seconds == .25 and not paused[0]:
                assert json.loads(attempt_path.read_text())["runtime_click_started"] is True
                write_private(config.ROOT / "private" / "pipeline-pause.json", {"reason": "Synthetic pause after click"})
                paused[0] = True
            original_wait(seconds)
        helpers["wait"] = wait
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert paused[0] and result["state"] == "submitted" and inspect("window.submissions") == 1
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        proof = json.loads(type(attempt_path)(result["receipt_path"]).read_text())
        row = {"job_hash": job["dedupe_hash"], "authorization_id": auth["authorization_id"],
               "application_url": job["url"], "attempt_path": str(attempt_path)}
        assert overnight._checked_receipt(row, proof) is True


@pytest.mark.parametrize("mode", ["verify", "ambiguous"])
def test_ashby_postclick_verification_or_no_receipt_is_held_uncertain(tmp_path, monkeypatch, mode):
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("window.mode="+json.dumps(mode))
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == "uncertain" and result["click_started"] is True
        assert inspect("window.submissions") == 1 and inspect("window.__jhbGuard") is True and other_guard() is True
        assert not (attempt_path.parent / "receipt.json").exists()
        again = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                           cli=cli, reviewer=independent_approval))
        assert again["state"] == "uncertain" and inspect("window.submissions") == 1


NATIVE_URLS = [("workable", "https://apply.workable.com/example/j/ABC1234567/apply"),
               ("lever", "https://jobs.lever.co/example/11111111-2222-3333-4444-555555555555/apply")]
NATIVE_HTML = '''<!doctype html><html><title>Synthetic native application</title>
<style>label{display:block;padding:5px}input,select{margin:4px}button{padding:8px;margin:8px}.hidden-choice{position:absolute;opacity:0;width:0;height:0}</style>
<form class="application-form" id="application">
<label for="fullname">Full Name</label><input id="fullname" required value="Synthetic Candidate">
<label for="email">Email</label><input id="email" type="email" required value="synthetic@example.invalid">
<label for="phone">Phone</label><input id="phone" type="tel" value="+15551234567">
<fieldset><legend>Authorized to work?</legend><label><input class="hidden-choice" name="auth" type="radio" value="yes" required checked>Yes</label>
<label><input class="hidden-choice" name="auth" type="radio" value="no">No</label></fieldset>
<label for="country">Country</label><select id="country" required><option value="">Select...</option><option value="us" selected>United States</option><option value="ca">Canada</option></select>
<label for="cert"><input id="cert" type="checkbox" required checked>I certify these answers are accurate</label>
<div data-ui="resume"><label for="resume">Replace file</label><input id="resume" name="resume" type="file" required hidden></div>
<label for="optional">Optional new question</label><input id="optional">
<button type="submit">Submit application</button></form>
<form id="newsletter"><input aria-label="Newsletter"><button>Submit application</button></form>
<script>window.submissions=0;window.trusted=false;window.mode='success';
window.genderCatalog=['Male','Female','Decline to self identify'];window.genderSelections=0;
function openGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
list.replaceChildren();for(const label of window.genderCatalog){const option=document.createElement('div');option.role='option';option.textContent=label;
option.onclick=()=>{window.genderSelections++;control.parentElement.querySelector('.select__multi-value__label').textContent=label;closeGender()};list.appendChild(option)}
list.hidden=false;control.setAttribute('aria-expanded','true')}
function closeGender(){const control=document.getElementById('gender'),list=document.getElementById('gender-options');
if(list)list.hidden=true;if(control)control.setAttribute('aria-expanded','false')}
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeGender()});
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++;window.trusted=e.isTrusted;
if(window.mode==='success'){e.target.remove();document.body.append('Your application has been submitted successfully.')}
if(window.mode==='verify'){e.target.remove();document.body.append('Verification code: check your inbox')}
};</script></html>'''


def native_evidence(tmp_path, monkeypatch, board, url):
    from jhb.applications import boards
    job, _, _, manifest, auth, _ = ashby_evidence(tmp_path, monkeypatch)
    monkeypatch.setitem(boards.ADAPTERS[board], "submit_enabled", True)
    raw = json.loads(type(tmp_path)(auth["authorization_path"]).read_text())
    raw["boards"] = sorted(set(raw["boards"] + [board]))
    write_private(type(tmp_path)(auth["authorization_path"]), raw)
    auth = {**raw, "authorization_id": hashlib.sha256(type(tmp_path)(auth["authorization_path"]).read_bytes()).hexdigest()}
    key = boards.application_hash(url)
    job = {**job, "dedupe_hash": key, "url": url}
    resume = manifest["documents"]["documents.resume"]["value"]
    fields = [("fullname", "Full Name", "identity.full_name", "Synthetic Candidate"),
              ("email", "Email", "identity.email", "synthetic@example.invalid"),
              ("phone", "Phone", "identity.phone", "+15551234567"),
              ("native-radio:auth", "Authorized to work?", "custom.auth", True),
              ("country", "Country", "identity.country", "United States"),
              ("cert", "I certify these answers are accurate", "custom.cert", True),
              ("resume", "Resume", "documents.resume", resume)]
    filled = [{"ref": ref, "question": label, "key": answer_key, "value": value, "source": "Synthetic approved fact"}
              for ref, label, answer_key, value in fields]
    packet = {"job": job, "state": "waiting_review", "submitted": False, "missing": [], "filled": filled}
    packet_path = tmp_path / "private" / "applications" / key / "packet.json"
    write_private(packet_path, packet)
    attempt = {"job_hash": key, "application_url": url, "authorization_id": auth["authorization_id"],
               "authorization_path": auth["authorization_path"], "started_at": datetime.now(timezone.utc).isoformat(),
               "state": "in_progress", "require_independent_review": True, "runtime_click_started": False,
               "packet_path": str(packet_path), "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()}
    attempt_path = tmp_path / "private" / "authorized-submissions" / key / "attempt.json"
    write_private(attempt_path, attempt)
    return job, packet, packet_path, {**manifest, "filled": filled}, auth, attempt_path


@pytest.mark.parametrize("board,url", NATIVE_URLS)
def test_flat_native_workable_lever_double_checks_reviewer_and_exact_form_receipt(tmp_path, monkeypatch, board, url):
    job, packet, packet_path, manifest, auth, attempt_path = native_evidence(tmp_path, monkeypatch, board, url)
    with synthetic_runtime(html=NATIVE_HTML, url=url) as (call, invoke, inspect, helpers, target, other_guard, lane):
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == "submitted", result
        assert inspect("window.submissions") == 1 and inspect("window.trusted") is True
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        receipt = json.loads(type(attempt_path)(result["receipt_path"]).read_text())
        from jhb.applications import overnight
        record = {"job_hash": job["dedupe_hash"], "authorization_id": auth["authorization_id"],
                  "application_url": job["url"], "attempt_path": str(attempt_path)}
        assert receipt["board"] == board and overnight._checked_receipt(record, receipt) is True
        assert receipt["check_count"] == 2 and receipt["resume_sha256"]


@pytest.mark.parametrize("board,url", NATIVE_URLS)
@pytest.mark.parametrize("change", ["new_required", "radio_changed", "document_receipt", "saved_record", "postclick_verification"])
def test_native_terminal_holds_unknown_saved_records_changed_values_and_uncertain_outcomes(tmp_path, monkeypatch, board, url, change):
    job, packet, packet_path, manifest, auth, attempt_path = native_evidence(tmp_path, monkeypatch, board, url)
    if change == "saved_record":
        packet["filled"].append({"ref": "workable:education:0:school", "question": "Education school",
                                "key": "education.0.school", "value": "Synthetic University", "source": "Verified original record"})
        write_private(packet_path, packet)
        current = json.loads(attempt_path.read_text())
        write_private(attempt_path, {**current, "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()})
        manifest["filled"] = packet["filled"]
    with synthetic_runtime(html=NATIVE_HTML, url=url) as (call, invoke, inspect, helpers, target, other_guard, lane):
        if change == "new_required": inspect("document.getElementById('optional').required=true")
        elif change == "radio_changed": inspect("document.querySelector('input[name=auth][value=no]').checked=true")
        elif change == "postclick_verification": inspect("window.mode='verify'")
        def reviewer(*args):
            approved = independent_approval(*args)
            if change == "document_receipt": inspect("delete document.getElementById('resume').__jhbUploadReceipt")
            return approved
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=reviewer))
        expected = change == "postclick_verification"
        assert result["state"] in ({"uncertain"} if expected else {"waiting_review", "waiting_input"}), result
        assert inspect("window.submissions") == int(expected)
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        assert json.loads(attempt_path.read_text())["runtime_click_started"] is expected


@pytest.mark.parametrize("token,display,submitted", [
    ("opaque-location-id", "San Diego, California, United States", True),
    ("", "San Diego, California, United States", False),
    ("opaque-location-id", "Austin, Texas, United States", False),
])
def test_lever_location_requires_committed_token_and_approved_display_label(tmp_path, monkeypatch, token, display, submitted):
    board, url = NATIVE_URLS[1]
    job, packet, packet_path, manifest, auth, attempt_path = native_evidence(tmp_path, monkeypatch, board, url)
    packet["filled"].append({"ref": "location-input", "question": "Current Location", "key": "custom.location",
                             "value": "San Diego, CA", "source": "Synthetic approved city"})
    write_private(packet_path, packet)
    manifest["filled"] = packet["filled"]
    write_private(attempt_path, {**json.loads(attempt_path.read_text()),
                                "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()})
    location_html = ('<div class="application-label">Current Location</div><div class="application-field">'
                     '<input id="location-input" name="location" required>'
                     '<input id="selected-location" name="selectedLocation" type="hidden"></div>')
    html = NATIVE_HTML.replace('<label for="optional">', location_html+'<label for="optional">')
    with synthetic_runtime(html=html, url=url) as (call, invoke, inspect, helpers, target, other_guard, lane):
        inspect("document.getElementById('location-input').value="+json.dumps(display))
        inspect("document.getElementById('selected-location').value="+json.dumps(token))
        def reviewer(*args):
            location = next(r for r in args[2]["retained"] if r["ref"] == "location-input")
            assert location["state"]["locationToken"] == token
            assert location["state"]["selected"] == display
            return independent_approval(*args)
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=reviewer))
        assert result["state"] == ("submitted" if submitted else "waiting_review"), result
        assert inspect("window.submissions") == int(submitted)
        assert inspect("window.__jhbGuard") is True and other_guard() is True


def test_lever_stable_named_control_remains_owned_after_upload_inserts_metadata(tmp_path, monkeypatch):
    board, url = NATIVE_URLS[1]
    job, packet, packet_path, manifest, auth, attempt_path = native_evidence(tmp_path, monkeypatch, board, url)
    next(r for r in packet["filled"] if r["ref"] == "phone")["ref"] = "native-name:phone"
    write_private(packet_path, packet)
    manifest["filled"] = packet["filled"]
    write_private(attempt_path, {**json.loads(attempt_path.read_text()),
                                "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()})
    html = NATIVE_HTML.replace('<label for="phone">Phone</label><input id="phone" type="tel" value="+15551234567">',
                               '<label>Phone<input name="phone" type="tel" value="+15551234567"></label>')
    with synthetic_runtime(html=html, url=url) as (call, invoke, inspect, helpers, target, other_guard, lane):
        async def upload_then_metadata(operation, **payload):
            result = await invoke(operation, **payload)
            if operation == "document":
                inspect("(()=>{const e=document.createElement('input');e.type='hidden';e.name='uploadMetadata';document.getElementById('application').prepend(e)})()")
            return result
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", upload_then_metadata)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=independent_approval))
        assert result["state"] == "submitted", result
        assert inspect("window.submissions") == 1 and inspect("window.trusted") is True
        assert inspect("window.__jhbGuard") is True and other_guard() is True
