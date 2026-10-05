"""Native current-form submission preserves candidate edits and attached bytes."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from jhb import config
from jhb.applications import approvals, booklet, live_review, overnight, queue
from jhb.applications.authorized_submission import AuthorizedSubmissionCLI, submit_reviewed
from jhb.applications.browser import GUARD_SCRIPT
from test_authorized_submission import ASHBY_HTML, ASHBY_URL, ashby_evidence, independent_approval, synthetic_runtime


def current_authority(tmp_path, monkeypatch, job, packet, packet_path, manifest, observation, helpers, lane):
    """Real portal binding and durable attempt, over synthetic browser values."""
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    job = {**job, "role_classes": "swe"}
    packet["job"] = job
    current = {"job": job, **live_review.project(packet, observation)}
    current["missing"] = []
    booklet.write_private(packet_path, current)
    screenshot = lane.submit(helpers["cdp"], "Page.captureScreenshot", format="png").result()["data"]
    packet_path.with_name("browser.png").write_bytes(base64.b64decode(screenshot))
    book_path = tmp_path / "private" / "book.json"
    book = {"schema_version": 1, "answers": {}, "roles": {"sde": manifest["documents"], "ml": {}}}
    booklet.write_private(book_path, book)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'waiting_review',1,?)",
                 (job["dedupe_hash"], json.dumps(job), str(packet_path.with_name("review.html"))))
    conn.commit()
    _, _, revision = approvals._draft(conn, job["dedupe_hash"], book_path)
    blanks = [field["ref"] for field in current["review_inventory"]["fields"] if field["status"] != "answered"]
    approved = approvals.approve(conn, job["dedupe_hash"], revision, blanks, book_path, current_form=True)
    auth_path = conn.execute("SELECT authorization_path FROM application_approvals WHERE approval_id=?", (approved["approval_id"],)).fetchone()[0]
    auth = overnight.load_authorization(auth_path)
    attempt_path = tmp_path / "private" / "authorized-submissions" / job["dedupe_hash"] / "attempt.json"
    booklet.write_private(attempt_path, {"job_hash": job["dedupe_hash"], "application_url": job["url"],
        "authorization_id": auth["authorization_id"], "authorization_path": auth_path,
        "started_at": datetime.now(timezone.utc).isoformat(), "state": "in_progress", "runtime_click_started": False,
        "require_independent_review": True, "packet_path": str(packet_path),
        "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()})
    conn.close()
    return job, current, overnight._manifest(job, current, book), auth, attempt_path


@pytest.mark.parametrize("change", [None, "answer_after_approval", "file_after_approval", "reviewer_reject", "discard_before_press"])
def test_current_form_native_submit_never_fills_or_reuploads(tmp_path, monkeypatch, change):
    job, old_packet, packet_path, old_manifest, _, _ = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (_, invoke, inspect, helpers, target, other_guard, lane):
        inspect(GUARD_SCRIPT)
        document = Path(old_manifest["documents"]["documents.resume"]["value"])
        obj = lane.submit(helpers["cdp"], "Runtime.evaluate", expression="document.getElementById('_systemfield_resume')", returnByValue=False).result()["result"]["objectId"]
        lane.submit(helpers["cdp"], "DOM.setFileInputFiles", files=[str(document)], objectId=obj).result()
        inspect("document.getElementById('name').value='Candidate manual edit preserved';window.beforeSubmitName=null;document.getElementById('submit').addEventListener('click',()=>window.beforeSubmitName=document.getElementById('name')?.value,true)")
        observation = lane.submit(live_review.observe, {"target_id": target, "expected_url": ASHBY_URL}, helpers).result()
        job, packet, manifest, auth, attempt_path = current_authority(tmp_path, monkeypatch, job, old_packet,
            packet_path, old_manifest, observation, helpers, lane)
        assert packet["filled"][0]["value"] == "Candidate manual edit preserved"
        operations, reviews, mutations = [], [], []
        original_cdp, original_wait = helpers["cdp"], helpers["wait"]
        def cdp(method, **params):
            if method in {"DOM.setFileInputFiles", "Input.insertText"}:
                mutations.append(method)
            return original_cdp(method, **params)
        helpers["cdp"] = cdp
        if change == "answer_after_approval":
            inspect("document.getElementById('name').value='Changed after approval'")
        if change == "file_after_approval":
            replacement = tmp_path / "replacement.pdf"; replacement.write_bytes(document.read_bytes()+b"\n% changed")
            lane.submit(original_cdp, "DOM.setFileInputFiles", files=[str(replacement)], objectId=obj).result()
        def reviewer(*args):
            reviews.append(args)
            assert args[1]["review_mode"] == live_review.MODE
            assert args[1]["filled"][0]["value"] == "Candidate manual edit preserved"
            verdict = independent_approval(*args)
            return {**verdict, "verdict": "reject"} if change == "reviewer_reject" else verdict
        def wait(seconds):
            if change == "discard_before_press" and "submit" in operations and seconds == .15:
                booklet.write_private(tmp_path / "private" / "application-discards" / (job["dedupe_hash"]+".json"),
                                      {"job_hash": job["dedupe_hash"], "state": "discarded"})
            original_wait(seconds)
        helpers["wait"] = wait
        async def recorded(operation, **payload):
            operations.append(operation)
            assert operation not in {"document", "fill", "open"}
            return await invoke(operation, **payload)
        cli = AuthorizedSubmissionCLI(); monkeypatch.setattr(cli, "invoke", recorded)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=auth, attempt=attempt_path,
                                            cli=cli, reviewer=reviewer))
        assert not mutations
        assert other_guard() is True
        if change is None:
            assert result["state"] == "submitted", result
            assert operations == ["locate", "check", "submit"]
            assert len(reviews) == 1 and result["check_count"] == 2
            assert inspect("window.submissions") == 1
            assert inspect("window.beforeSubmitName") == "Candidate manual edit preserved"
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            assert receipt["resume_sha256"] == hashlib.sha256(document.read_bytes()).hexdigest()
            assert overnight._checked_receipt({"job_hash": job["dedupe_hash"], "application_url": job["url"],
                "authorization_id": auth["authorization_id"], "attempt_path": str(attempt_path)}, receipt)
        else:
            assert result["state"] == "waiting_review", result
            assert inspect("window.submissions") == 0 and inspect("window.__jhbGuard") is True
            assert json.loads(attempt_path.read_text())["runtime_click_started"] is False
