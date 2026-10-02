"""Synthetic Greenhouse-style fixture; no requests leave localhost."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .. import config, poll, store
from . import booklet, queue

FORM = r'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Greenhouse-style demo application</title>
<style>body{font:17px system-ui;background:#f4f8f5;margin:0;color:#173b29}main{background:white;max-width:720px;margin:36px auto;padding:34px;border-radius:18px}label{display:block;margin:18px 0 7px}input:not([type=radio]):not([type=checkbox]),textarea,select{width:95%;padding:10px;border:1px solid #aaa;border-radius:5px}button{padding:12px 24px;margin-top:20px;border:0;border-radius:6px;background:#087f46;color:white;font-size:16px}fieldset label{display:inline-block;margin-right:18px}pre{white-space:pre-wrap}small{color:#557568}</style>
<main><small>LOCAL FIXTURE · no real employer · no application will be sent</small>
<h1 id="heading">Create account</h1><div id="content"></div></main>
<script>
const params=new URLSearchParams(location.search), role=params.get('role')||'sde';
window.demoAnswers={}; window.demoSubmitted=0;
const content=document.getElementById('content'),heading=document.getElementById('heading');
function show(step){
 if(params.get('captcha')==='1'&&!window.captchaSolved){heading.textContent='Verification';content.innerHTML='<div data-jhb-captcha>Complete CAPTCHA to continue</div><button type="button" onclick="window.captchaSolved=true;show(0)">I completed the challenge</button>';return;}
 if(step===0){
  if(params.get('auth')==='skip'){show(1);return;}
  heading.textContent='Create account';content.innerHTML='<form action="/signup" id="signup"><label for="account-email">Email</label><input id="account-email" type="email" required><label for="password">Password</label><input id="password" type="password" required><button type="submit">Create account</button></form>';
  document.getElementById('signup').onsubmit=async e=>{e.preventDefault();const response=await fetch('/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('account-email').value,password:document.getElementById('password').value})});if(response.ok)show(1);else heading.textContent='Sign-in needs attention';};return;
 }
 if(step===1){heading.textContent=(role==='ml'?'Machine Learning Engineer':'Software Engineer')+' · Demo Company';
 content.innerHTML='<form id="contact"><label for="first">First name</label><input id="first" required><label for="last">Last name</label><input id="last" required><label for="email">Email address</label><input id="email" type="email" required><label for="phone">Phone number</label><input id="phone" type="tel" required><label for="resume">Resume</label><input id="resume" type="file" accept="application/pdf" required><button type="button" id="next">Next</button></form>';
 document.getElementById('next').onclick=()=>{const f=document.getElementById('contact');if(!f.reportValidity())return;['first','last','email','phone'].forEach(id=>demoAnswers[id]=document.getElementById(id).value);demoAnswers.resume=document.getElementById('resume').files[0].name;show(2);};return;
 }
 if(step===2){heading.textContent='Screening and experience';content.innerHTML='<form id="screening"><label for="skills">Technical skills</label><textarea id="skills" required></textarea><label for="experience">Work experience</label><textarea id="experience" required></textarea><fieldset><legend>Are you legally authorized to work in the United States?</legend><label><input type="radio" name="authorization" value="Yes" required>Yes</label><label><input type="radio" name="authorization" value="No">No</label></fieldset><label for="sponsor">Will you now or in the future require sponsorship?</label><select id="sponsor" required><option value="">Choose</option><option>Yes</option><option>No</option></select><label for="gender">Gender</label><select id="gender"><option value="">Choose</option><option>Woman</option><option>Man</option><option>Prefer not to answer</option></select><label><input id="truth" type="checkbox" required>I certify that the information provided is accurate</label><div id="custom"></div><button type="button" id="review">Review application</button></form>';
 if(params.get('unknown')==='1')document.getElementById('custom').innerHTML='<label for="unknown">Explain your experience with our proprietary engine</label><textarea id="unknown" required></textarea>';
 document.getElementById('review').onclick=()=>{const f=document.getElementById('screening');if(!f.reportValidity())return;['skills','experience','sponsor','gender'].forEach(id=>demoAnswers[id]=document.getElementById(id).value);demoAnswers.authorization=document.querySelector('input[name=authorization]:checked').value;demoAnswers.truth=document.getElementById('truth').checked;show(3);};return;
 }
 heading.textContent='Review your application';content.innerHTML='<pre id="summary"></pre><form action="/submit" id="final"><button type="submit">Submit application</button></form>';
 document.getElementById('summary').textContent=JSON.stringify(demoAnswers,null,2);
 document.getElementById('final').onsubmit=async e=>{e.preventDefault();await fetch('/submit',{method:'POST',body:JSON.stringify(demoAnswers)});window.demoSubmitted++;};
}
show(0);
</script></html>'''


@contextmanager
def fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if urlsplit(self.path).path == "/job":
                body = FORM.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/signup":
                data = json.loads(body)
                self.server.accounts[data["email"]] = hashlib.sha256(data["password"].encode()).hexdigest()
                self.send_response(200)
                self.send_header("Set-Cookie", "demo-auth=1; HttpOnly; SameSite=Strict; Path=/")
            elif self.path == "/submit":
                self.server.submissions += 1
                self.send_response(200)
            else: self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{}')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.accounts, server.submissions = {}, 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def sample_book(directory: Path):
    from pypdf import PdfWriter
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "demo-resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(pdf)
    answers = {key: booklet.answer() for key in booklet.ALIASES if not key.startswith(("role.", "documents."))}
    facts = {"identity.first_name": "Sam", "identity.last_name": "Casey", "identity.full_name": "Sam Casey",
             "identity.email": "sam@example.test", "identity.phone": "+1 555 010 1234",
             "eligibility.authorized_us": True, "eligibility.sponsorship": False,
             "consent.truthfulness": True, "disclosure.gender": "Prefer not to answer"}
    answers.update({key: booklet.answer(value, "synthetic fixture; not candidate data") for key, value in facts.items()})
    roles = {role: {"documents.resume": booklet.answer(str(pdf), "synthetic PDF"),
                    "role.skills": booklet.answer("Python, PostgreSQL, React" if role == "sde" else "Python, PyTorch, TensorFlow", "synthetic fixture"),
                    "role.experience": booklet.answer("Built a demo service." if role == "sde" else "Trained a demo classifier.", "synthetic fixture")}
             for role in ["sde", "ml"]}
    return {"schema_version": 1, "answers": answers, "roles": roles, "sources": [], "custom_answers": {}}


def run_demo(*, planner="deterministic", headless=True):
    """Exercise the entire durable pipeline with synthetic jobs and local forms.

    Source resolution is injected fixture evidence, not live MCP validation.
    Actual guarded browser preparation uses isolated Playwright fixture contexts.
    """
    from unittest.mock import patch
    from playwright.async_api import async_playwright
    from . import pipeline, questions
    from .credentials import CredentialStore
    from .planner import CodexPlanner, deterministic_plan
    from .worker import prepare, role_for_job, write_packet

    directory = config.ROOT / "private" / "demo" / f"run-{__import__('time').time_ns()}"
    book_path = directory / "answer-booklet.json"
    booklet.write_private(book_path, sample_book(directory))
    conn = store.connect(directory / "jobs.sqlite3")
    store.upsert_jobs(conn, [store.Job("simplify", "seed", "History", "Seed", "https://example.test/seed")], mark_notified=True)
    discovered = [
        store.Job("simplify", "direct", "Demo Company", "Software Engineer",
                  "https://job-boards.greenhouse.io/demo/jobs/1234", role_classes=["swe"]),
        store.Job("jobspy:linkedin", "wrapper", "Demo Company", "Machine Learning Engineer",
                  "https://www.linkedin.com/jobs/view/5678", role_classes=["ml"]),
        store.Job("simplify", "other", "Other Demo Company", "Software Engineer",
                  "https://jobs.lever.co/other-demo/abc", role_classes=["swe"]),
    ]

    async def fixture_resolver(candidate, **kwargs):
        source_url = candidate["url"]
        if "lever.co" in source_url:
            return {"state": "not_greenhouse", "board_type": "lever", "source_url": source_url,
                    "application_url": None, "evidence": [{"kind": "fixture", "url": source_url}]}
        application_url = ("https://job-boards.greenhouse.io/demo/jobs/5678"
                           if "linkedin.com" in source_url else source_url)
        return {"state": "greenhouse", "board_type": "greenhouse", "source_url": source_url,
                "application_url": application_url,
                "evidence": [{"kind": "injected_fixture_redirect", "url": application_url}]}

    try:
        with fixture_server() as (server, origin):
            async def fixture_runner(candidate, book, **kwargs):
                role = role_for_job(candidate)
                answers = booklet.for_role(book, role)
                identity = queue.greenhouse_identity(candidate["url"])
                answers.update({key: item for key, item in book.get("custom_answers", {}).items()
                                if not item.get("scope") or item["scope"] == {"region": identity[0], "board": identity[1]}})
                artifact = directory / "applications" / candidate["dedupe_hash"]
                selected_planner = CodexPlanner(artifact) if planner == "codex" else deterministic_plan
                local_job = {**candidate, "url": origin + f"/job?role={role}&auth=skip&unknown=1"}
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=headless)
                    context = await browser.new_context(service_workers="block", viewport={"width": 1280, "height": 960})
                    try:
                        page = await context.new_page()
                        result, _ = await prepare(page, local_job, answers, selected_planner,
                                                  CredentialStore(directory / "demo-vault.json"), demo_origin=origin)
                        return result, await write_packet(page, artifact, candidate, result)
                    finally:
                        await context.close()
                        await browser.close()

            with patch.object(poll.simplify, "poll", return_value=(discovered[:1] + discovered[2:], "fixture")), \
                 patch.object(poll.jobspy_src, "poll", return_value=(discovered[1:2], "fixture")), \
                 patch.object(poll.notify, "send", return_value=True):
                discovery = poll.run_once(conn)
                first = pipeline.run_cycle(conn, book_path, resolver=fixture_resolver, runner=fixture_runner,
                                           planner_name=planner)
                pending = questions.pending(book_path)
                if len(pending) != 1 or len(pending[0]["contexts"]) != 2:
                    raise RuntimeError("Fixture did not preserve a deduplicated question for both applications")
                # This value is synthetic test input, never an inferred candidate answer.
                affected = questions.answer(pending[0]["id"], "Built a synthetic adapter for the demo engine.",
                                            book_path, connection=conn)
                resumed = pipeline.run_cycle(conn, book_path, resolver=fixture_resolver, runner=fixture_runner,
                                             planner_name=planner)
            states = [row[0] for row in conn.execute("SELECT state FROM applications")]
            if states != ["waiting_review"] * 2 or server.submissions != 0 or len(affected) != 2:
                raise RuntimeError("Fixture pipeline did not stop safely at pre-submit review")
            print("SYNTHETIC FIXTURE VALIDATION: source resolution is injected; browser filling uses local Playwright.")
            print(f"Phase 1 source checks queued: {discovery['sources_queued']} · boards: {json.dumps(first['boards'], sort_keys=True)}")
            print(f"Greenhouse applications: {first['applications_queued']} · new questions: {len(pending)} · resumed: {len(affected)}")
            print(f"Review-ready: {resumed['applications_prepared']} · final submissions: {server.submissions}")
            print(f"Private fixture artifacts: {directory}")
    finally:
        conn.close()
    return 0
