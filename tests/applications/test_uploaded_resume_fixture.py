"""Synthetic Chromium/CDP upload regressions, not live Browser Use validation.

The real CLI facade and dispatcher run against an isolated fixture transport;
the installed CLI and the candidate's browser are never invoked.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os

from pypdf import PdfWriter

from jhb import config
from jhb.applications.booklet import answer
from jhb.applications.cli_browser import BrowserUseCLI
from jhb.applications.cli_runtime import dispatch
from jhb.applications.planner import deterministic_plan, key_for_field
from jhb.applications.worker import prepare


UPLOAD_HTML = '''<!doctype html><html lang="en"><title>Synthetic attached uploads</title>
<style>.file-upload{margin:25px;padding:15px;border:1px solid #ccc}button{padding:8px}</style>
<form id="application">
<div id="resume-group" class="file-upload" role="group" aria-labelledby="resume-label" aria-required="true">
<div id="resume-label" class="upload-label">Resume/CV*</div>
<div class="file-upload__filename"><p>resume.pdf</p><button type="button" onclick="removeAttachment('resume')">Remove file</button></div></div>
<div id="certificate-group" class="file-upload" role="group" aria-labelledby="certificate-label" aria-required="true">
<div id="certificate-label" class="upload-label">Professional registration certificate *</div>
<div class="file-upload__filename"><p>certificate.pdf</p><button type="button" onclick="removeAttachment('certificate')">Remove file</button></div></div>
<button type="submit">Submit application</button></form>
<script>
window.removals=[];window.uploads=[];window.uploadedDigests={};window.submissions=0;
function removeAttachment(id){
 const group=document.getElementById(id+'-group');window.removals.push(id);
 group.querySelector('.file-upload__filename').remove();
 const input=document.createElement('input');input.id=id;input.type='file';input.accept='.pdf';input.hidden=true;
 input.onchange=async()=>{
  const file=input.files[0];window.uploads.push({id,name:file.name});
  const bytes=await file.arrayBuffer();
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  window.uploadedDigests[id]=Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
  input.remove();
  const wrapper=document.createElement('div');wrapper.className='file-upload__filename';
  const name=document.createElement('p');name.textContent=file.name;wrapper.appendChild(name);
  const remove=document.createElement('button');remove.type='button';remove.textContent='Remove file';
  remove.onclick=()=>removeAttachment(id);wrapper.appendChild(remove);group.appendChild(wrapper);
 };group.appendChild(input);
}
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


@contextmanager
def attached_upload_browser():
    """Keep sync Playwright on one thread while exercising async CLI methods."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    with ThreadPoolExecutor(max_workers=1) as lane:
        def start():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 640})
            requests = []
            def serve(route):
                requests.append(route.request.url)
                route.fulfill(status=200, content_type="text/html", body=UPLOAD_HTML)
            page.route("**/*", serve)
            url = "https://job-boards.greenhouse.io/synthetic-uploads/jobs/1234"
            page.goto(url)
            session = page.context.new_cdp_session(page)
            target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            def switch(owned):
                assert owned == target
            helpers = {"cdp": lambda method, **params: session.send(method, params),
                       "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds * 1000),
                       "click_at_xy": page.mouse.click,
                       "list_tabs": lambda: [{"url": url, "targetId": target}],
                       "switch_tab": switch,
                       "current_tab": lambda: {"targetId": target, "url": page.url}}
            return pw, browser, page, helpers, target, url, requests
        pw, browser, page, helpers, target, url, requests = lane.submit(start).result()
        def call(operation, **payload):
            return lane.submit(dispatch, {"operation": operation, **payload}, helpers).result()
        async def invoke(operation, **payload):
            if operation != "open":
                payload.update(target_id=target, expected_url=url)
            return await asyncio.wrap_future(lane.submit(dispatch, {"operation": operation, **payload}, helpers))
        def inspect(expression):
            return lane.submit(page.evaluate, expression).result()
        try:
            call("open", url=url)
            yield call, invoke, inspect, url, target, requests
        finally:
            lane.submit(browser.close).result()
            lane.submit(pw.stop).result()


def synthetic_resume(tmp_path):
    path = tmp_path / "chosen-role" / "resume.pdf"
    path.parent.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "Synthetic approved role resume"})
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def test_required_star_resume_without_input_is_replaced_by_fresh_cli_and_verified_by_bytes(tmp_path, monkeypatch):
    resume = synthetic_resume(tmp_path)
    answers = {"documents.resume": answer(str(resume), "Synthetic approved selected-role document")}
    with attached_upload_browser() as (call, invoke, inspect, url, target, requests):
        assert inspect("document.getElementById('resume')===null")
        snapshot = call("observe")
        field = next(f for f in snapshot["fields"] if f["label"] == "Resume/CV")
        assert field == {"ref": "uploaded:Resume/CV", "label": "Resume/CV", "type": "file", "required": True, "options": []}
        assert {"ref": field["ref"], "answer_key": "documents.resume"} in deterministic_plan(snapshot, answers)["bindings"]
        client = BrowserUseCLI()
        client.target_id, client.expected_url = target, url
        monkeypatch.setattr(client, "invoke", invoke)
        # An identical displayed basename cannot establish the selected role's PDF.
        assert client._uploads == {}
        result = asyncio.run(client.fill(field, str(resume)))
        assert result["verified"] is True
        assert result["filename"] == "resume.pdf"
        assert result["upload_receipt"]
        assert inspect("window.uploadedDigests.resume") == hashlib.sha256(resume.read_bytes()).hexdigest()
        assert inspect("window.removals") == ["resume"]
        assert inspect("window.uploads") == [{"id": "resume", "name": "resume.pdf"}]
        assert inspect("document.querySelector('#certificate-group .file-upload__filename p').textContent") == "certificate.pdf"
        assert inspect("document.getElementById('resume')===null")
        # A trusted receipt can avoid replacement within this exact client.
        assert asyncio.run(client.fill(field, str(resume)))["verified"] is True
        assert inspect("window.removals") == ["resume"]
        refreshed = next(f for f in call("observe")["fields"] if f["label"] == "Resume/CV")
        assert refreshed["required"] is True
        assert key_for_field(refreshed, answers) == "documents.resume"
        assert inspect("window.__jhbGuard") is True
        inspect("document.getElementById('application').submit()")
        assert inspect("window.submissions") == 0
        assert requests == [url]


def test_unknown_required_attached_upload_remains_observable_and_worker_hands_off(tmp_path, monkeypatch):
    resume = synthetic_resume(tmp_path)
    answers = {"documents.resume": answer(str(resume), "Synthetic approved selected-role document")}
    with attached_upload_browser() as (call, invoke, inspect, url, target, requests):
        snapshot = call("observe")
        unknown = next(f for f in snapshot["fields"] if f["label"] == "Professional registration certificate")
        assert unknown["ref"] == "uploaded:Professional registration certificate"
        assert unknown["required"] is True
        assert unknown["type"] == "file"
        assert key_for_field(unknown, answers) is None
        client = BrowserUseCLI()
        monkeypatch.setattr(client, "invoke", invoke)
        result, _ = asyncio.run(prepare(None, {"url": url, "company": "Synthetic Company"},
                                       answers, deterministic_plan, None, cli_actions=client))
        assert result["state"] == "waiting_input"
        assert len(result["missing"]) == 1
        assert result["missing"][0]["ref"] == unknown["ref"]
        assert result["missing"][0]["question"] == unknown["label"]
        assert result["missing"][0]["answer_key"] is None
        assert [f["key"] for f in result["filled"]] == ["documents.resume"]
        assert inspect("window.removals") == ["resume"]
        assert inspect("window.uploadedDigests.resume") == hashlib.sha256(resume.read_bytes()).hexdigest()
        assert inspect("document.querySelector('#certificate-group .file-upload__filename p').textContent") == "certificate.pdf"
        assert inspect("window.submissions") == 0
        assert requests == [url]
