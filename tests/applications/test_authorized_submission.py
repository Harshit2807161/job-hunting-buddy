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
<input id="gender" role="combobox" aria-labelledby="gender-label" aria-required="true"></div>
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


@contextmanager
def synthetic_runtime():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    with ThreadPoolExecutor(max_workers=1) as lane:
        def start():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start(); browser = pw.chromium.launch(); page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
            page.goto(URL)
            other = browser.new_page(); other.set_content("<form><button>Submit application</button></form>"); other.evaluate(GUARD_SCRIPT)
            session = page.context.new_cdp_session(page)
            target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            def switch(owned):
                assert owned == target
            helpers = {"cdp": lambda method, **params: session.send(method, params), "js": page.evaluate,
                       "wait": lambda seconds: page.wait_for_timeout(1 if seconds == .25 else seconds*1000),
                       "click_at_xy": page.mouse.click, "list_tabs": lambda: [{"targetId": target, "url": URL}],
                       "switch_tab": switch, "current_tab": lambda: {"targetId": target, "url": page.url}}
            return pw, browser, page, other, helpers, target
        pw, browser, page, other, helpers, target = lane.submit(start).result()
        def call(request):
            return lane.submit(dispatch, request, helpers).result()
        async def invoke(operation, **payload):
            if operation != "locate":
                payload.update(target_id=target, expected_url=URL)
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
        assert inspect("window.__jhbGuard") is True
        assert other_guard() is True
        assert json.loads(attempt_path.read_text())["runtime_click_started"] is True
        # An interrupted/restarted manager can inspect the receipt but never click again.
        repeated = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path, cli=cli))
        assert repeated["state"] == "uncertain"
        assert inspect("window.submissions") == 1


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


@pytest.mark.parametrize("interruption", ["between_checks", "during_geometry", "authorization_revoked", "upload_receipt_mutated"])
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
