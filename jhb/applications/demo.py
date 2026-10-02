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
    """Run the real Phase 1 entry point against mocked sources, then the real worker."""
    from unittest.mock import patch
    from .worker import drain_once, notify_pending
    directory = config.ROOT / "private" / "demo"
    book_path = directory / "answer-booklet.json"
    booklet.write_private(book_path, sample_book(directory))
    conn = store.connect(directory / f"jobs-{__import__('time').time_ns()}.sqlite3")
    # Seed unrelated history so this demo exercises new-job discovery rather than seed mode.
    store.upsert_jobs(conn, [store.Job("simplify", "seed", "Demo", "Seed", "https://example.test")], mark_notified=True)
    with fixture_server() as (server, origin):
        job = store.Job("simplify", "demo-job", "Demo Company", "Software Engineer",
                        "https://job-boards.greenhouse.io/demo/jobs/1234", role_classes=["swe"])
        with patch.dict(os.environ, {"JHB_APPLICATIONS_ENABLED": "1"}), \
             patch.object(poll.simplify, "poll", return_value=([job], "fixture")), \
             patch.object(poll.jobspy_src, "poll", return_value=([], "fixture")), \
             patch.object(poll.notify, "send", return_value=True):
            summary = poll.run_once(conn)
        row = conn.execute("SELECT job_hash,job_json FROM applications").fetchone()
        actual = json.loads(row["job_json"])
        actual["url"] = origin + "/job?role=sde"
        conn.execute("UPDATE applications SET job_json=? WHERE job_hash=?", (json.dumps(actual), row["job_hash"]))
        conn.commit()
        result = drain_once(conn, book_path, planner_name=planner, demo_origin=origin,
                            headless=headless, artifacts=directory / "applications")
        notify_pending(conn, send_email=False)
        packet = conn.execute("SELECT packet FROM applications").fetchone()[0]
        print(f"Discovery queued: {summary['applications_queued']} · {result['state']}")
        print(f"Accounts created: {len(server.accounts)} · submissions: {server.submissions}")
        print(f"Review packet: {packet}")
        if result["state"] != "waiting_review" or server.submissions != 0:
            raise RuntimeError("Demo did not reach pre-submit review safely")
    conn.close()
    return 0
