"""Synthetic local portal integration; no browser, credentials or live writes."""
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from jhb import config
from jhb.dashboard import PNG, create_app
from jhb.applications import approvals, boards, booklet, questions, queue, tracking


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    db = tmp_path / "pipeline.sqlite3"
    book = tmp_path / "private" / "answer-booklet.json"
    booklet.write_private(book, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    queue.initialize(conn); tracking.initialize(conn); conn.commit()
    app = create_app(root=tmp_path, db_path=db, book_path=book)
    client = TestClient(app, base_url="http://127.0.0.1:8030", client=("127.0.0.1", 12345))
    headers = {"Origin": "http://127.0.0.1:8030", "X-JHB-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
    yield tmp_path, conn, book, client, headers
    conn.close()


def add_job(conn, root, n=1, state="waiting_review", packet_state=None, complete=False):
    url = f"https://job-boards.greenhouse.io/example/jobs/{n}"
    job = {"dedupe_hash": boards.application_hash(url), "url": url, "company": "Synthetic Employer", "title": "Software Engineer"}
    folder = root / "private" / "applications" / job["dedupe_hash"]
    data = {"job": job, "state": packet_state or state, "submitted": False, "created_at": 1791075600,
            "missing": [], "filled": [{"ref": "name", "question": "Full Name", "key": "identity.full_name", "value": "Synthetic Candidate", "source": "Synthetic resume"}]}
    if complete:
        data["review_inventory"] = {"complete": True, "fields": [
            {"ref": "name", "question": "Full Name", "type": "text", "required": True, "status": "answered", "answer_key": "identity.full_name"},
            {"ref": "why", "question": "Why this company? Please, no AI text.", "type": "textarea", "required": False, "status": "blank", "candidate_wording_required": True}]}
    booklet.write_private(folder / "packet.json", data)
    (folder / "browser.png").write_bytes(PNG+b"synthetic image")
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,?,?,?)",
                 (job["dedupe_hash"], json.dumps(job), state, 1791075600, str(folder / "review.html")))
    conn.commit()
    return job, folder, data


def pending_question(job, book, label="Will you relocate?", ref="relocate", required=True, kind="text"):
    return questions.collect(job, {"missing": [{"question": label, "ref": ref, "required": required, "type": kind}]}, book)[0]


def test_daily_counts_use_confirmed_receipts_and_pacific_calendar_boundaries(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = add_job(conn, root, state="submitted", packet_state="waiting_review")
    conn.execute("INSERT INTO confirmed_submissions VALUES(?,?,?,?,?,?,?)",
                 ("proof1", "greenhouse", job["url"], json.dumps(job), "2026-10-04T01:00:00+00:00", "{}", 1))
    conn.execute("INSERT INTO submission_sheet_delivery VALUES(?,?,'synced',0,NULL,NULL,NULL,1)", ("proof1", "synthetic-sink"))
    add_job(conn, root, n=2, state="submission_uncertain")
    conn.commit()
    previous = client.get("/api/v1/overview?day=2026-10-03").json()
    assert previous["summary"]["confirmed_today"] == 1
    assert previous["summary"]["confirmed_total"] == 1
    assert previous["summary"]["sheet_synced"] == 1
    assert previous["summary"]["uncertain"] == 1
    assert previous["applications"][1 if previous["applications"][0]["state"] != "submitted" else 0]["confirmed_date"] == "2026-10-03"
    assert client.get("/api/v1/overview?day=2026-10-04").json()["summary"]["confirmed_today"] == 0


@pytest.mark.parametrize("day", ["20261004", "2026-02-31", "tomorrow"])
def test_date_query_requires_real_iso_calendar_date(portal, day):
    assert portal[3].get("/api/v1/overview", params={"day": day}).status_code == 400


@pytest.mark.parametrize("host,origin,token,fetch", [
    ("127.0.0.1.evil.test", "http://127.0.0.1:8030", True, None),
    ("127.0.0.1:8030", "https://evil.test", True, None),
    ("127.0.0.1:8030", "http://127.0.0.1:8030", False, None),
    ("127.0.0.1:8030", "http://127.0.0.1:8030", True, "cross-site"),
])
def test_cross_origin_and_dns_rebinding_cannot_change_answers(portal, host, origin, token, fetch):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    q = pending_question(job, book)
    before = book.read_bytes()
    malicious = {"Host": host, "Origin": origin, "X-JHB-CSRF": headers["X-JHB-CSRF"] if token else "wrong"}
    if fetch: malicious["Sec-Fetch-Site"] = fetch
    response = client.post(f"/api/v1/questions/{q['id']}/answer", json={"value": True, "revision": q["updated_at"]}, headers=malicious)
    assert response.status_code == 403 and book.read_bytes() == before


def test_nonlocal_client_cannot_read_private_dashboard(portal):
    app = portal[3].app
    client = TestClient(app, base_url="http://127.0.0.1:8030", client=("192.0.2.10", 80))
    assert client.get("/api/v1/overview").status_code == 403


def test_only_identity_validated_private_png_can_be_served(portal):
    root, conn, book, client, headers = portal
    job, folder, _ = add_job(conn, root)
    url = f"/api/v1/applications/{job['dedupe_hash']}/screenshot"
    response = client.get(url)
    assert response.status_code == 200 and response.content.startswith(PNG)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    (folder / "browser.png").unlink()
    secret = root / "private" / "credentials.json"; secret.write_text("fixture-only private credential")
    (folder / "browser.png").symlink_to(secret)
    assert client.get(url).status_code == 404
    assert client.get("/private/credentials.json").status_code == 404
    assert client.get("/api/v1/files/.env").status_code == 404


def test_mismatched_packet_cannot_expose_another_application(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = add_job(conn, root)
    packet["job"]["url"] = "https://job-boards.greenhouse.io/example/jobs/999"
    booklet.write_private(folder / "packet.json", packet)
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}/screenshot").status_code == 404


def test_questions_save_persistently_resume_only_unblocked_fill_and_never_submit(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    q1 = pending_question(job, book)
    q2 = pending_question(job, book, "Earliest start date?", "start")
    reply = client.post(f"/api/v1/questions/{q1['id']}/answer", headers=headers,
                        json={"value": False, "revision": q1["updated_at"]})
    assert reply.status_code == 200 and reply.json()["resumed_jobs"] == []
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"
    assert booklet.load(book)["custom_answers"]["custom."+q1["id"][2:]]["value"] is False
    reply = client.post(f"/api/v1/questions/{q2['id']}/answer", headers=headers,
                        json={"value": "2027-01-01", "revision": q2["updated_at"]})
    assert reply.json()["resumed_jobs"] == [job["dedupe_hash"]]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
    assert client.post(f"/api/v1/questions/{q2['id']}/answer", headers=headers,
                       json={"value": "Changed", "revision": q2["updated_at"]}).status_code == 409


def test_paused_pipeline_accepts_answer_and_durably_queues_fill_without_starting_worker(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    q = pending_question(job, book)
    booklet.write_private(root / "private" / "pipeline-pause.json", {"paused": True})
    reply = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
                        json={"value": "Yes", "revision": q["updated_at"]})
    assert reply.status_code == 200 and reply.json()["resumed_jobs"] == [job["dedupe_hash"]]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
    assert (root / "private" / "pipeline-pause.json").exists()
    assert client.get("/api/v1/overview").json()["automation_paused"] is True


def test_secret_challenges_do_not_appear_in_pending_question_payload(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    q = pending_question(job, book)
    data = booklet.load(book)
    data["question_handoffs"][q["id"]]["question"] = "Your verification code?"
    data["answers"]["identity.email"] = booklet.answer("private-candidate@example.invalid", "Synthetic resume")
    booklet.write_private(book, data)
    response = client.get("/api/v1/overview")
    assert response.json()["questions"] == []
    assert "private-candidate@example.invalid" not in response.text


def test_legacy_packet_is_inspectable_but_never_approvable(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root)
    review = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert review["fields"][0]["answer"] == "Synthetic Candidate"
    assert review["inventory_complete"] is False and review["approval"]["can_approve"] is False
    assert client.post(f"/api/v1/applications/{job['dedupe_hash']}/approve", headers=headers,
                       json={"revision": "fake", "acknowledged_blank_refs": []}).status_code == 409


def test_complete_inventory_blank_ack_and_revision_go_through_root_approval_api(portal, monkeypatch):
    root, conn, book, client, headers = portal
    job, folder, _ = add_job(conn, root, complete=True)
    monkeypatch.setattr(approvals, "review", lambda *a, **kw: {"revision": "exact-revision", "can_approve": True,
        "packet_sha256": hashlib.sha256((folder / "packet.json").read_bytes()).hexdigest(),
        "blank_questions": [{"ref": "why", "question": "Why this company?", "required": False, "type": "textarea"}]})
    called = []
    def approve(connection, key, expected_revision, acknowledged_blank_refs, book_path):
        called.append((key, expected_revision, acknowledged_blank_refs, book_path))
        if expected_revision != "exact-revision" or acknowledged_blank_refs != ["why"]:
            raise ValueError("Unacknowledged blank")
        return {"approval_id": "synthetic-approval", "state": "approved", "job_hash": key}
    monkeypatch.setattr(approvals, "approve", approve)
    url = f"/api/v1/applications/{job['dedupe_hash']}/approve"
    assert client.post(url, headers=headers, json={"revision": "exact-revision", "acknowledged_blank_refs": []}).status_code == 409
    response = client.post(url, headers=headers, json={"revision": "exact-revision", "acknowledged_blank_refs": ["why"]})
    assert response.status_code == 200 and response.json()["state"] == "approved"
    assert called[-1] == (job["dedupe_hash"], "exact-revision", ["why"], book)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"
    review = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert review["fields"][1]["answer"] is None and review["fields"][1]["candidate_wording_required"] is True


def test_incident_preserves_confirmed_state_and_lists_unanswered_questions(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="submitted")
    booklet.write_private(root / "private" / "application-incidents" / (job["dedupe_hash"]+".json"),
        {"job_hash": job["dedupe_hash"], "state": "review_required", "summary": "Optional prompts were not answered", "blank_questions": [{"ref": "why", "question": "Why this employer?"}]})
    review = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert review["state"] == "submitted" and review["approval"]["can_approve"] is False
    assert review["incident"]["blank_questions"][0]["question"] == "Why this employer?"
    assert client.get("/api/v1/overview").json()["applications"][0]["has_incident"] is True


def test_active_questions_drop_terminal_manual_and_declined_contexts_without_erasing_history(portal):
    root, conn, book, client, headers = portal
    jobs = [add_job(conn, root, n=n, state=state)[0] for n, state in enumerate([
        "waiting_input", "submitted", "skipped", "submission_uncertain", "waiting_input", "waiting_input"], start=1)]
    for job in jobs:
        q = pending_question(job, book)
    data = booklet.load(book)
    data["manual_application_records"] = {"synthetic": {"url": jobs[4]["url"], "state": "submitted"}}
    data["job_exclusions"] = {jobs[5]["dedupe_hash"]: {"status": "verified", "source": "Synthetic user declined role"}}
    data["question_handoffs"][q["id"]]["contexts"][jobs[0]["dedupe_hash"]]["reason"] = "Two approved catalog choices are ambiguous"
    booklet.write_private(book, data)
    before = book.read_bytes()
    response = client.get("/api/v1/overview").json()
    assert response["summary"]["questions"] == 1
    assert len(response["questions"][0]["contexts"]) == 1
    assert response["questions"][0]["contexts"][0]["job_hash"] == jobs[0]["dedupe_hash"]
    assert response["questions"][0]["contexts"][0]["reason"] == "Two approved catalog choices are ambiguous"
    assert book.read_bytes() == before


def test_portal_click_creates_real_per_job_bound_approval_and_revoke_without_browser(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = add_job(conn, root, complete=True)
    resume = root / "private" / "synthetic-sde.pdf"
    resume.write_bytes(b"%PDF-1.4\nSynthetic application document")
    data = booklet.load(book)
    data["answers"]["identity.full_name"] = booklet.answer("Synthetic Candidate", "Synthetic resume")
    data["roles"]["sde"]["documents.resume"] = booklet.answer(str(resume), "Synthetic selected SDE resume")
    booklet.write_private(book, data)
    packet["filled"].append({"ref": "resume", "question": "Resume", "key": "documents.resume",
                             "value": str(resume), "source": "Synthetic selected SDE resume"})
    packet["review_inventory"]["fields"].append({"ref": "resume", "question": "Resume", "type": "file",
                                               "required": True, "status": "answered", "answer_key": "documents.resume"})
    packet["selected_role"] = "sde"
    packet["job"]["role_classes"] = "swe"
    booklet.write_private(folder / "packet.json", packet)
    url = f"/api/v1/applications/{job['dedupe_hash']}"
    view = client.get(url).json()
    assert view["approval"]["can_approve"] is True and view["resume_role"] == "sde", view["approval"]
    assert view["documents"] == [{"kind": "resume", "filename": resume.name}]
    response = client.post(url+"/approve", headers=headers,
        json={"revision": view["approval"]["revision"], "acknowledged_blank_refs": ["why"]})
    assert response.status_code == 200 and response.json()["state"] == "approved"
    record = conn.execute("SELECT state,authorization_path FROM application_approvals").fetchone()
    authority = json.loads(Path(record["authorization_path"]).read_text())
    assert authority["job_hash"] == job["dedupe_hash"] and authority["acknowledged_blank_refs"] == ["why"]
    assert authority["binding"]["selected_role"] == "sde" and authority["binding"]["document_sha256"]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
    assert client.post(url+"/revoke", headers=headers, json={}).status_code == 200
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"


def test_ready_count_requires_complete_retained_inventory_not_legacy_review_state(portal):
    root, conn, book, client, headers = portal
    add_job(conn, root)
    good, _, _ = add_job(conn, root, n=2, complete=True)
    bad, folder, packet = add_job(conn, root, n=3, complete=True)
    packet["review_inventory"]["fields"][1]["required"] = True
    booklet.write_private(folder / "packet.json", packet)
    overview = client.get("/api/v1/overview").json()
    assert overview["states"]["waiting_review"] == 3
    assert overview["summary"]["ready"] == 1 and overview["summary"]["legacy_review"] == 2
    assert [a["id"] for a in overview["applications"] if a["inventory_ready"]] == [good["dedupe_hash"]]


def test_rejected_independent_review_uses_latest_exact_job_result_reason_not_request_blob(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root)
    folder = root / "private" / "application-reviews" / job["dedupe_hash"]
    common = {"job_hash": job["dedupe_hash"], "source": "independent_application_review", "reviewer": "codex-readonly", "verdict": "reject"}
    booklet.write_private(folder / ("a"*32+"-result.json"), {**common, "reviewed_at": "2026-10-04T20:00:00Z",
        "issues": [{"field_ref": "why", "reason": "Candidate wording is missing"}]})
    booklet.write_private(folder / ("b"*32+"-result.json"), {**common, "job_hash": "9"*64, "reviewed_at": "2026-10-04T21:00:00Z",
        "issues": [{"reason": "Wrong job must not be shown"}]})
    booklet.write_private(folder / ("c"*32+"-request.json"), {"private_candidate_request": "Never expose this evidence blob"})
    response = client.get(f"/api/v1/applications/{job['dedupe_hash']}")
    assert response.json()["reviewer_issues"] == ["[why] Candidate wording is missing"]
    assert response.json()["reviewer_verdict"] == "reject"
    assert "Never expose" not in response.text and "Wrong job" not in response.text


def reviewable(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = add_job(conn, root, complete=True)
    resume = root / "private" / "synthetic-sde.pdf"
    resume.write_bytes(b"%PDF-1.4\nSynthetic fixture resume")
    data = booklet.load(book)
    data["answers"]["identity.full_name"] = booklet.answer("Synthetic Candidate", "Synthetic resume")
    data["roles"]["sde"]["documents.resume"] = booklet.answer(str(resume), "Synthetic resume")
    booklet.write_private(book, data)
    packet["filled"].append({"ref": "resume", "question": "Resume", "key": "documents.resume", "value": str(resume), "source": "Synthetic resume"})
    packet["review_inventory"]["fields"].append({"ref": "resume", "question": "Resume", "type": "file", "required": True, "status": "answered"})
    packet["job"]["role_classes"] = "swe"
    booklet.write_private(folder / "packet.json", packet)
    return job, folder, packet


def test_displayed_answers_and_approval_revision_cannot_come_from_different_packet_snapshots(portal, monkeypatch):
    root, conn, book, client, headers = portal
    job, folder, packet = reviewable(portal)
    original_review = approvals.review
    swapped = [False]
    def review(*args, **kwargs):
        if not swapped[0]:
            packet["filled"][0]["value"] = "New answer after the dashboard read"
            booklet.write_private(folder / "packet.json", packet)
            swapped[0] = True
        return original_review(*args, **kwargs)
    monkeypatch.setattr(approvals, "review", review)
    url = f"/api/v1/applications/{job['dedupe_hash']}"
    old = client.get(url).json()
    assert old["fields"][0]["answer"] == "Synthetic Candidate"
    assert old["approval"]["can_approve"] is False and old["approval"]["revision"] is None
    assert "changed during review" in old["approval"]["reason"]
    current = client.get(url).json()
    assert current["fields"][0]["answer"] == "New answer after the dashboard read"
    assert current["approval"]["can_approve"] is True
    assert current["approval"]["packet_sha256"] == hashlib.sha256((folder / "packet.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("has_approval", [False, True])
def test_explicit_optional_reply_requeues_review_draft_and_revokes_prior_approval_even_while_paused(portal, paused, has_approval):
    root, conn, book, client, headers = portal
    job, folder, packet = reviewable(portal)
    q = pending_question(job, book, "Why this company? Please, no AI text.", "why", required=False, kind="textarea")
    if has_approval:
        view = approvals.review(conn, job["dedupe_hash"], book)
        approvals.approve(conn, job["dedupe_hash"], view["revision"], ["why"], book)
    if paused:
        booklet.write_private(root / "private" / "pipeline-pause.json", {"paused": True})
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
                           json={"value": "My own synthetic candidate wording", "revision": q["updated_at"]})
    assert response.status_code == 200, response.text
    assert response.json()["resumed_jobs"] == [job["dedupe_hash"]]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "queued"
    assert booklet.load(book)["custom_answers"]["custom."+q["id"][2:]]["value"] == "My own synthetic candidate wording"
    if has_approval:
        approval = conn.execute("SELECT state,authorization_path FROM application_approvals").fetchone()
        assert approval["state"] == "revoked"
        assert json.loads(Path(approval["authorization_path"]).read_text())["enabled"] is False
    assert json.loads((folder / "packet.json").read_text())["review_inventory"]["fields"][1]["status"] == "blank"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["active", "clicked", "missing_file", "unknown_marker", "wrong_job"])
def test_candidate_edit_does_not_requeue_or_rewrite_book_for_active_or_uncertain_terminal_attempt(portal, mode):
    from jhb.applications import overnight
    root, conn, book, client, headers = portal
    job, folder, packet = reviewable(portal)
    q = pending_question(job, book, "Why this company? Please, no AI text.", "why", required=False, kind="textarea")
    overnight.initialize(conn)
    path = root / "private" / "authorized-submissions" / job["dedupe_hash"] / "attempt.json"
    evidence = {"job_hash": job["dedupe_hash"], "authorization_id": "a"*64, "runtime_click_started": mode == "clicked"}
    if mode == "unknown_marker": evidence.pop("runtime_click_started")
    if mode == "wrong_job": evidence["job_hash"] = "b"*64
    if mode != "missing_file": booklet.write_private(path, evidence)
    conn.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path) VALUES(?,?,?,?,1,1,?)",
                 (job["dedupe_hash"], "a"*64, job["url"], "in_progress" if mode == "active" else "waiting_review", str(path)))
    conn.commit()
    before = book.read_bytes()
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
                           json={"value": "My own synthetic candidate wording", "revision": q["updated_at"]})
    assert response.status_code == 409 and book.read_bytes() == before
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"


@pytest.mark.parametrize("state", ["running", "submitted", "submission_uncertain", "skipped"])
def test_candidate_reply_never_requeues_protected_application_states(portal, state):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state=state)
    q = pending_question(job, book, required=False)
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
                           json={"value": "Synthetic answer", "revision": q["updated_at"]})
    assert response.status_code == 200 and response.json()["resumed_jobs"] == []
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == state


def test_proposed_wording_and_role_fit_notes_are_visible_distinct_from_final_reviewer_issues(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = add_job(conn, root, complete=True)
    packet["filled"][0]["source"] = {"kind": "grounded_narrative", "review_status": "proposed", "support": []}
    packet["role_fit"] = {"review_notes": ["The posting prefers one extra year of experience.", {"reason": "Discuss distributed systems depth in review."}]}
    booklet.write_private(folder / "packet.json", packet)
    response = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert response["fields"][0]["proposed"] is True
    assert response["fields"][1]["proposed"] is False
    assert response["role_fit_notes"] == ["The posting prefers one extra year of experience.", "Discuss distributed systems depth in review."]
    assert response["reviewer_issues"] == []


def test_stale_question_changed_between_api_read_and_locked_save_cannot_revoke_or_queue(portal, monkeypatch):
    root, conn, book, client, headers = portal
    job, _, _ = reviewable(portal)
    q = pending_question(job, book, 'Why this company?', 'why', required=False)
    view = approvals.review(conn, job['dedupe_hash'], book)
    approvals.approve(conn, job['dedupe_hash'], view['revision'], ['why'], book)
    real_answer = questions.answer
    def concurrent_change(*args, **kwargs):
        data = booklet.load(book)
        data['question_handoffs'][q['id']]['updated_at'] = 'a-new-worker-revision'
        booklet.write_private(book, data)
        return real_answer(*args, **kwargs)
    monkeypatch.setattr(questions, 'answer', concurrent_change)
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
        json={'value': 'Stale candidate answer', 'revision': q['updated_at']})
    assert response.status_code == 409
    assert conn.execute('SELECT state FROM applications').fetchone()[0] == 'waiting_review'
    assert conn.execute('SELECT state FROM application_approvals').fetchone()[0] == 'approved'
    assert 'custom.'+q['id'][2:] not in booklet.load(book)['custom_answers']


def test_answer_response_reports_remaining_blockers_then_paused_durable_queue(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state='waiting_input')
    first = pending_question(job, book)
    second = pending_question(job, book, 'Earliest start date?', 'start')
    booklet.write_private(root / 'private' / 'pipeline-pause.json', {'paused': True})
    response = client.post(f"/api/v1/questions/{first['id']}/answer", headers=headers,
        json={'value': False, 'revision': first['updated_at']}).json()
    assert response['applications'] == [{'job_hash': job['dedupe_hash'], 'state': 'waiting_input', 'remaining_required_questions': 1}]
    assert response['automation_paused'] is True
    response = client.post(f"/api/v1/questions/{second['id']}/answer", headers=headers,
        json={'value': '2027-01-01', 'revision': second['updated_at']}).json()
    assert response['applications'] == [{'job_hash': job['dedupe_hash'], 'state': 'queued', 'remaining_required_questions': 0}]
    assert conn.execute('SELECT attempts FROM applications').fetchone()[0] == 0


def test_review_details_include_only_questions_for_exact_application(portal):
    root, conn, book, client, _ = portal
    job, _, _ = add_job(conn, root, state='waiting_input')
    other, _, _ = add_job(conn, root, n=2, state='waiting_input')
    shared = pending_question(job, book)
    pending_question(other, book)
    unrelated = pending_question(other, book, 'Travel availability?', 'travel')
    view = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert [q['id'] for q in view['questions']] == [shared['id']]
    assert unrelated['id'] not in str(view['questions'])



def test_saved_answer_queue_failure_reports_durable_save_instead_of_inviting_duplicate(portal, monkeypatch):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    question = pending_question(job, book)
    real_answer = questions.answer
    def save_then_queue_failure(*args, **kwargs):
        # The real ledger write and after_save notification happen before SQL.
        args = (*args[:3], None)
        real_answer(*args, **kwargs)
        raise sqlite3.OperationalError("Synthetic queue unavailable")
    monkeypatch.setattr(questions, "answer", save_then_queue_failure)
    response = client.post(f"/api/v1/questions/{question['id']}/answer", headers=headers,
        json={"value": False, "revision": question["updated_at"]})
    assert response.status_code == 202 and response.json()["resume_pending"] is True
    assert response.json()["affected_jobs"] == [job["dedupe_hash"]]
    assert response.json()["resumed_jobs"] == []
    assert booklet.load(book)["question_handoffs"][question["id"]]["status"] == "answered"
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"


def test_required_ledger_question_prevents_approval_of_previously_complete_inventory(portal):
    _, conn, book, client, headers = portal
    job, _, _ = reviewable(portal)
    prior = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    pending_question(job, book, "Export controls authorization?", "export", required=True)
    current = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert current["approval"]["can_approve"] is False
    assert "required questions" in current["approval"]["reason"]
    response = client.post(f"/api/v1/applications/{job['dedupe_hash']}/approve", headers=headers,
        json={"revision": prior["approval"]["revision"], "acknowledged_blank_refs": ["why"]})
    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0



def test_failed_optional_edit_sql_keeps_exact_durable_refill_intent_and_revoked_authority(portal, monkeypatch):
    root, conn, book, client, headers = portal
    job, folder, _ = reviewable(portal)
    q = pending_question(job, book, "Why this company?", "why", required=False)
    old_packet, binding, revision = approvals._draft(conn, job["dedupe_hash"], book)
    approvals.approve(conn, job["dedupe_hash"], revision, ["why"], book)
    real_answer = questions.answer
    def save_then_queue_failure(*args, **kwargs):
        real_answer(*(*args[:3], None), **kwargs)
        raise sqlite3.OperationalError("Synthetic database transition unavailable")
    monkeypatch.setattr(questions, "answer", save_then_queue_failure)
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
        json={"value": "Candidate's own synthetic answer", "revision": q["updated_at"]})
    assert response.status_code == 202 and response.json()["resume_pending"] is True
    record = booklet.load(book)["question_handoffs"][q["id"]]
    intent = record["candidate_edit_intents"][job["dedupe_hash"]]
    assert intent == {"provider": "local_dashboard_explicit_edit", "question_id": q["id"],
        "answer_revision": record["answer_revision"], "job_hash": job["dedupe_hash"],
        "packet_path": str(folder / "packet.json"), "packet_sha256": binding["packet_sha256"],
        "review_revision": revision, "review_binding": binding, "approval_revoked": True}
    assert approvals._digest(intent["review_binding"]) == intent["review_revision"]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"
    assert json.loads((folder / "packet.json").read_text()) == old_packet


def test_ordinary_answer_has_no_review_edit_intent(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    q = pending_question(job, book)
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
        json={"value": False, "revision": q["updated_at"]})
    assert response.status_code == 200
    assert not booklet.load(book)["question_handoffs"][q["id"]].get("candidate_edit_intents")



def test_ready_count_excludes_new_required_ledger_question_without_mislabeling_legacy(portal):
    _, _, book, client, _ = portal
    job, _, _ = reviewable(portal)
    assert client.get("/api/v1/overview").json()["summary"]["ready"] == 1
    pending_question(job, book, "Current work authorization?", "authorization", required=True)
    view = client.get("/api/v1/overview").json()
    assert view["summary"]["ready"] == 0 and view["summary"]["legacy_review"] == 0
    row = next(a for a in view["applications"] if a["id"] == job["dedupe_hash"])
    assert row["inventory_verified"] is True and row["inventory_ready"] is False
    assert row["pending_required_questions"] == 1


def test_screenshot_endpoint_never_returns_new_image_under_old_review_digest(portal):
    root, conn, _, client, _ = portal
    job, folder, packet = add_job(conn, root)
    url = f"/api/v1/applications/{job['dedupe_hash']}"
    before = client.get(url).json()["screenshot"]
    assert before["available"] is True
    image = client.get(url+"/screenshot?revision="+before["revision"])
    assert image.status_code == 200 and image.headers["cache-control"] == "no-store"
    (folder / "browser.png").write_bytes(PNG+b"A different synthetic review image")
    assert client.get(url+"/screenshot?revision="+before["revision"]).status_code == 404
    after = client.get(url).json()["screenshot"]
    assert after["revision"] != before["revision"]
    assert client.get(url+"/screenshot?revision="+after["revision"]).content == (folder / "browser.png").read_bytes()
    assert client.get(url+"/screenshot?revision=../../private").status_code == 404



def test_unavailable_booklet_keeps_saved_review_readable_but_never_ready(portal):
    _, _, book, client, _ = portal
    job, _, _ = reviewable(portal)
    book.unlink()
    overview = client.get("/api/v1/overview").json()
    assert overview["booklet_available"] is False and overview["summary"]["ready"] == 0
    response = client.get(f"/api/v1/applications/{job['dedupe_hash']}")
    assert response.status_code == 200
    assert response.json()["fields"] and response.json()["approval"]["can_approve"] is False



def test_help_text_change_keeps_question_identity_but_rejects_old_answer_version(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    item = {"ref": "restriction", "question": "Any employment restrictions?", "type": "radio", "required": True,
            "description": "California applicants must choose N/A.", "description_truncated": False}
    before = questions.collect(job, {"missing": [item]}, book)[0]
    updated = questions.collect(job, {"missing": [{**item, "description": "Only California residents should choose N/A."}]}, book)[0]
    assert before["id"] == updated["id"] and before["updated_at"] != updated["updated_at"]
    response = client.post(f"/api/v1/questions/{before['id']}/answer", headers=headers,
        json={"value": "No", "revision": before["updated_at"]})
    assert response.status_code == 409
    pending = client.get("/api/v1/overview").json()["questions"][0]
    assert pending["question"] == item["question"]
    assert pending["contexts"][0]["description"] == "Only California residents should choose N/A."
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"


def test_answer_description_proof_includes_only_displayed_active_contexts(portal):
    root, conn, book, client, headers = portal
    live, _, _ = add_job(conn, root, state="waiting_input")
    closed, _, _ = add_job(conn, root, n=2, state="submitted")
    label = "Any employment restrictions?"
    questions.collect(closed, {"missing": [{"ref": "restriction", "question": label, "required": True,
        "description": "Historical closed-job guidance."}]}, book)
    q = questions.collect(live, {"missing": [{"ref": "restriction", "question": label, "required": True,
        "description": "Current visible guidance."}]}, book)[0]
    response = client.post(f"/api/v1/questions/{q['id']}/answer", headers=headers,
        json={"value": "N/A", "revision": q["updated_at"]})
    assert response.status_code == 200
    source = booklet.load(book)["custom_answers"]["custom."+q["id"][2:]]["source"]
    proof = {"owned_description_sha256": hashlib.sha256(b"Current visible guidance.").hexdigest(),
             "owned_description_truncated": False, "field_ref": "restriction", "country_context": ""}
    assert source["owned_description_proofs"] == [proof]
    assert source["owned_description_sha256"] == proof["owned_description_sha256"]


def test_review_help_text_is_bounded_and_keeps_original_question_label(portal):
    root, conn, _, client, _ = portal
    job, folder, packet = add_job(conn, root, complete=True)
    field = packet["review_inventory"]["fields"][1]
    field["description"] = "<script>should remain literal text</script>"+"x"*5000
    booklet.write_private(folder / "packet.json", packet)
    view = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()["fields"][1]
    assert view["question"] == field["question"]
    assert view["description"].startswith("<script>") and len(view["description"]) == 4096
    assert view["description_truncated"] is True
