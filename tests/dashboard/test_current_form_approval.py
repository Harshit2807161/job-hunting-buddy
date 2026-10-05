"""Explicit click dispatches only its current form; no live services used."""
import json
import hashlib
import time
from contextlib import contextmanager

import pytest

from test_dashboard import portal, add_job
from jhb.applications import approvals, booklet, live_review, service


@pytest.fixture
def current_form(portal, monkeypatch):
    root, conn, book, client, headers = portal
    monkeypatch.setenv("JHB_APPROVE_CURRENT_LIVE_FORM", "1")
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
    job, folder, packet = add_job(conn, root, complete=True)
    packet["job"]["role_classes"] = "swe"
    resume = root / "private" / "synthetic.pdf"
    resume.write_bytes(b"%PDF-1.4\nSynthetic resume")
    data = booklet.load(book)
    data["roles"]["sde"]["documents.resume"] = booklet.answer(str(resume), "Synthetic chosen document")
    booklet.write_private(book, data)
    packet["filled"].append({"ref": "resume", "question": "Resume", "key": "documents.resume",
                             "value": str(resume), "source": "Synthetic chosen document"})
    packet["review_inventory"]["fields"].append({"ref": "resume", "question": "Resume", "type": "file",
                                                 "required": True, "status": "answered"})
    booklet.write_private(folder / "packet.json", packet)
    events = []

    async def capture(path, *, acknowledged_blank_refs):
        assert path == str(folder / "packet.json")
        events.append(("capture", acknowledged_blank_refs))
        current = json.loads((folder / "packet.json").read_text())
        current["filled"][0]["value"] = "Candidate's manually edited name"
        current["review_inventory"]["fields"][1]["status"] = "answered"
        current["filled"].append({"ref": "why", "question": "Why this company? Please, no AI text.",
                                 "key": "custom.live_review.why", "value": "Candidate's own current answer",
                                 "source": {"provider": "local_portal_current_form_approval"}})
        current.update(review_mode=live_review.MODE, live_review={"snapshot_sha256": "e"*64},
                       live_documents=data["roles"]["sde"])
        booklet.write_private(folder / "packet.json", current)

    async def drain(connection, book_path, *, limit, job_hash):
        assert job_hash == job["dedupe_hash"] and limit == 1 and book_path == book
        authority_path = connection.execute("SELECT authorization_path FROM application_approvals WHERE job_hash=?", (job_hash,)).fetchone()[0]
        authority = json.loads(open(authority_path).read())
        assert authority["approval_mode"] == live_review.MODE
        assert authority["require_independent_review"] is True
        assert authority["acknowledged_blank_refs"] == []
        approvals.validate_binding(authority, folder / "packet.json")
        events.append(("dispatch", job_hash))
        connection.execute("UPDATE application_approvals SET state='submitted' WHERE job_hash=?", (job_hash,))
        connection.commit()
        return {"attempted": 1, "submitted": 1, "uncertain": 0, "handoffs": 0}

    monkeypatch.setattr(live_review, "capture_current", capture)
    monkeypatch.setattr(approvals, "drain", drain)
    url = f"/api/v1/applications/{job['dedupe_hash']}"
    view = client.get(url).json()
    assert view["approval"]["can_approve"], view["approval"]
    return portal, job, folder, url, view["approval"]["revision"], events


def test_click_adopts_manual_edits_and_dispatches_only_that_job_immediately(current_form):
    portal, job, folder, url, revision, events = current_form
    root, conn, book, client, headers = portal
    response = client.post(url+"/approve", headers=headers, json={"revision": revision, "acknowledged_blank_refs": []})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "submitted" and response.json()["manual_edits_preserved"]
    assert [e[0] for e in events] == ["capture", "dispatch"]
    assert json.loads((folder / "packet.json").read_text())["filled"][0]["value"] == "Candidate's manually edited name"
    # Endpoint dispatch is not a fabricated positive receipt in the database.
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0


def test_busy_submission_lane_queues_exact_approval_without_refilling(current_form, monkeypatch):
    portal, job, folder, url, revision, events = current_form
    @contextmanager
    def busy():
        yield False
    monkeypatch.setattr(service, "_approved_lock", busy)
    response = portal[3].post(url+"/approve", headers=portal[4], json={"revision": revision, "acknowledged_blank_refs": []})
    assert response.status_code == 200 and response.json()["state"] == "approved"
    assert [e[0] for e in events] == ["capture"]


@pytest.fixture
def current_stretch(current_form, monkeypatch):
    from jhb import eligibility
    from jhb.applications import job_context, role_fit
    portal, job, folder, url, _, events = current_form
    root, conn, book, client, headers = portal
    data = booklet.load(book)
    data["roles"]["sde"]["role.experience"] = booklet.answer("Synthetic software research", "Synthetic resume")
    data["candidate_selected_jobs"] = {job["dedupe_hash"]: {
        "job_hash": job["dedupe_hash"], "url": job["url"], "content": "Apply to " + job["url"],
        "status": "verified", "role": "user", "action": "apply", "source": "Synthetic candidate message"}}
    booklet.write_private(book, data)
    text = "Research commercial software. Model training experience desired."
    description = {"status": "verified", "text": text, "source_url": job_context.description_source(job["url"]),
        "retrieved_at": time.time(), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    booklet.write_private(folder / "eligibility.json", {"state": "eligible", "policy": eligibility.POLICY_ID,
        "description": description})
    booklet.write_private(folder / "role-fit.json", {"state": "skipped", "verdict": "not_fit",
        "review_status": "complete", "source": role_fit.POLICY, "mode": "independent_codex", "selected_role": "sde",
        "reason": "Model training is not documented.", "matched_requirements": ["Software research"],
        "unsupported_core_requirements": ["Model training"], "review_notes": [],
        "evidence_hash": role_fit.evidence_hash({**job, "verified_job_description": description}, data, "sde")})
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    detail = client.get(url).json()
    assert detail["approval"]["requires_role_fit_acknowledgment"]
    return portal, job, folder, url, detail["approval"]["revision"], events


@pytest.mark.parametrize("ack", [None, False, "true", 1])
def test_stretch_role_requires_explicit_boolean_ack_before_browser_capture(current_stretch, ack):
    portal, job, folder, url, revision, events = current_stretch
    root, conn, book, client, headers = portal
    before = (folder / "packet.json").read_bytes()
    payload = {"revision": revision, "acknowledged_blank_refs": []}
    if ack is not None: payload["acknowledge_role_fit_warning"] = ack
    response = client.post(url + "/approve", headers=headers, json=payload)
    assert response.status_code == (422 if ack in ("true", 1) else 409)
    assert events == [] and (folder / "packet.json").read_bytes() == before
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0


def test_stretch_role_ack_reaches_fresh_authority_without_replacing_manual_edits(current_stretch):
    portal, job, folder, url, revision, events = current_stretch
    root, conn, book, client, headers = portal
    before = (folder / "role-fit.json").read_bytes()
    result = client.post(url + "/approve", headers=headers, json={"revision": revision,
        "acknowledged_blank_refs": [], "acknowledge_role_fit_warning": True})
    assert result.status_code == 200, result.text
    assert [event[0] for event in events] == ["capture", "dispatch"]
    authority_path = conn.execute("SELECT authorization_path FROM application_approvals").fetchone()[0]
    auth = json.loads(open(authority_path).read())
    assert auth["acknowledge_role_fit_warning"] is True
    assert auth["binding"]["candidate_selected_stretch"]["fit_verdict"] == "not_fit"
    assert (folder / "role-fit.json").read_bytes() == before
    assert json.loads((folder / "packet.json").read_text())["filled"][0]["value"] == "Candidate's manually edited name"


def test_fit_warning_change_during_current_capture_requires_new_review(current_stretch, monkeypatch):
    portal, job, folder, url, revision, events = current_stretch
    root, conn, book, client, headers = portal
    previous = live_review.capture_current
    async def changed_warning(path, *, acknowledged_blank_refs):
        await previous(path, acknowledged_blank_refs=acknowledged_blank_refs)
        fit = json.loads((folder / "role-fit.json").read_text())
        fit["review_notes"].append("New gap found since the displayed review.")
        booklet.write_private(folder / "role-fit.json", fit)
    monkeypatch.setattr(live_review, "capture_current", changed_warning)
    response = client.post(url + "/approve", headers=headers, json={"revision": revision,
        "acknowledged_blank_refs": [], "acknowledge_role_fit_warning": True})
    assert response.status_code == 409 and "role-fit warning changed" in response.json()["detail"]
    assert [event[0] for event in events] == ["capture"]
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0


@pytest.mark.parametrize("marker", ["pipeline-pause.json", "overnight-monitor/repair-pending.json"])
def test_maintenance_never_consumes_a_candidate_approval(current_form, marker):
    portal, job, folder, url, revision, events = current_form
    root, conn, book, client, headers = portal
    booklet.write_private(root / "private" / marker, {"state": "held"})
    before = (folder / "packet.json").read_bytes()
    detail = client.get(url).json()
    assert detail["approval"]["can_approve"] is False
    response = client.post(url+"/approve", headers=headers, json={"revision": revision, "acknowledged_blank_refs": []})
    assert response.status_code == 409 and "preserved" in response.json()["detail"]
    assert not events
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0
    assert (folder / "packet.json").read_bytes() == before


def test_new_blank_preview_requires_another_explicit_click(current_form, monkeypatch):
    portal, job, folder, url, revision, events = current_form
    root, conn, book, client, headers = portal
    previous_capture = live_review.capture_current
    async def new_blank(path, *, acknowledged_blank_refs):
        await previous_capture(path, acknowledged_blank_refs=acknowledged_blank_refs)
        packet = json.loads((folder / "packet.json").read_text())
        packet["review_inventory"]["fields"].append({"ref": "new-blank", "question": "Optional website",
            "type": "url", "required": False, "status": "blank"})
        packet["optional_questions"] = [{"ref": "new-blank", "question": "Optional website", "required": False, "type": "url"}]
        booklet.write_private(folder / "packet.json", packet)
        raise ValueError("Explicitly acknowledge each optional blank answer before approval")
    monkeypatch.setattr(live_review, "capture_current", new_blank)
    response = client.post(url+"/approve", headers=headers, json={"revision": revision, "acknowledged_blank_refs": []})
    assert response.status_code == 409
    assert [event[0] for event in events] == ["capture"]
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0
    refreshed = client.get(url).json()
    assert refreshed["approval"]["can_approve"] is True
    assert [item["ref"] for item in refreshed["approval"]["blank_questions"]] == ["new-blank"]


@pytest.mark.parametrize("blocked", ["maintenance", "stale_revision", "csrf"])
def test_blocked_click_does_not_read_or_mutate_browser(current_form, monkeypatch, blocked):
    portal, job, folder, url, revision, events = current_form
    if blocked == "maintenance":
        monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "0")
    response = portal[3].post(url+"/approve", headers={} if blocked == "csrf" else portal[4],
        json={"revision": "stale" if blocked == "stale_revision" else revision, "acknowledged_blank_refs": []})
    assert response.status_code in {403, 409}
    assert events == []
