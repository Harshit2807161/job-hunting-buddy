import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from jhb import config
from jhb.applications import approvals, boards, booklet, overnight, queue


@pytest.fixture
def draft(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "1")
    url = "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555"
    key = boards.application_hash(url)
    job = {"dedupe_hash": key, "url": url, "title": "Software Engineer", "company": "Synthetic", "role_classes": "swe"}
    resume = tmp_path / "private" / "synthetic.pdf"
    resume.parent.mkdir(); resume.write_bytes(b"%PDF-1.4\nSynthetic fixture"); resume.chmod(0o600)
    bookpath = tmp_path / "private" / "book.json"
    book = {"schema_version": 1, "answers": {"identity.full_name": booklet.answer("Synthetic Candidate", "Fixture")},
            "roles": {"sde": {"documents.resume": booklet.answer(str(resume), "Fixture chosen SDE resume")}, "ml": {}}}
    booklet.write_private(bookpath, book)
    filled = [{"ref": "resume", "question": "Resume", "key": "documents.resume", "value": str(resume), "source": "Fixture chosen SDE resume"}]
    fields = [{"ref": "resume", "question": "Resume", "type": "file", "required": True, "status": "answered"},
              {"ref": "motivation", "question": "Why this company?", "type": "textarea", "required": False, "status": "blank"}]
    packet = {"job": job, "state": "waiting_review", "submitted": False, "missing": [], "filled": filled,
              "optional_questions": [{"ref": "motivation", "question": "Why this company?"}],
              "review_inventory": {"complete": True, "fields": fields}}
    path = tmp_path / "private" / "applications" / key / "packet.json"
    booklet.write_private(path, packet)
    path.with_name("browser.png").write_bytes(b"\x89PNG\r\n\x1a\nSynthetic screenshot")
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; queue.initialize(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'waiting_review',?,?)",
                 (key, json.dumps(job), int(time.time()), str(path.with_name("review.html")))); conn.commit()
    yield conn, key, path, bookpath, resume
    conn.close()


def approve(draft):
    conn, key, path, bookpath, _ = draft
    revision = approvals.review(conn, key, bookpath)["revision"]
    result = approvals.approve(conn, key, revision, ["motivation"], bookpath)
    authpath = conn.execute("SELECT authorization_path FROM application_approvals WHERE approval_id=?", (result["approval_id"],)).fetchone()[0]
    return result, overnight.load_authorization(authpath)


def test_approval_requires_explicit_blank_acknowledgment_and_current_revision(draft):
    conn, key, _, bookpath, _ = draft
    view = approvals.review(conn, key, bookpath)
    assert view["can_approve"] and view["blank_questions"][0]["ref"] == "motivation"
    with pytest.raises(ValueError, match="each optional blank"):
        approvals.approve(conn, key, view["revision"], [], bookpath)
    with pytest.raises(ValueError, match="draft changed"):
        approvals.approve(conn, key, "outdated", ["motivation"], bookpath)
    result, auth = approve(draft)
    assert result["state"] == "approved" and auth["scope"] == overnight.PORTAL_SCOPE
    assert approvals.validate_binding(auth, draft[2]) is True
    assert approvals.review(conn, key, bookpath)["can_approve"] is False
    with pytest.raises(ValueError, match="pending approval"):
        approve(draft)


@pytest.mark.parametrize("change", ["packet", "resume", "profile", "screenshot"])
def test_any_change_to_reviewed_answers_documents_or_candidate_facts_invalidates_approval(draft, change):
    _, auth = approve(draft)
    conn, key, path, bookpath, resume = draft
    if change == "packet":
        packet = json.loads(path.read_text()); packet["extra"] = "changed"; booklet.write_private(path, packet)
    elif change == "resume": resume.write_bytes(b"%PDF-1.4\nDifferent resume")
    elif change == "screenshot": path.with_name("browser.png").write_bytes(b"\x89PNG\r\n\x1a\nDifferent screenshot")
    else:
        book = booklet.load(bookpath); book["answers"]["identity.full_name"]["value"] = "Changed Candidate"; booklet.write_private(bookpath, book)
    with pytest.raises(ValueError, match="changed"):
        approvals.validate_binding(auth, path)
    result = asyncio.run(approvals.drain(conn, bookpath))
    assert result["attempted"] == 0
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "invalidated"


@pytest.mark.parametrize("change", ["legacy", "required_blank", "missing_optional", "false_answer"])
def test_incomplete_or_dishonest_inventory_cannot_be_approved(draft, change):
    conn, key, path, bookpath, _ = draft
    packet = json.loads(path.read_text())
    if change == "legacy": packet.pop("review_inventory")
    elif change == "required_blank": packet["review_inventory"]["fields"][1]["required"] = True
    elif change == "missing_optional": packet["review_inventory"]["fields"].pop()
    else: packet["review_inventory"]["fields"][1]["status"] = "answered"
    booklet.write_private(path, packet)
    assert approvals.review(conn, key, bookpath)["can_approve"] is False


def test_revoke_stops_pending_authority_and_global_overnight_gate_cannot_replace_portal(draft):
    conn, key, _, bookpath, _ = draft
    _, auth = approve(draft)
    assert overnight.gate_enabled({"scope": overnight.MULTI_SCOPE}) is False
    approvals.revoke(conn, key)
    assert overnight.load_authorization(auth["authorization_path"]) is None
    assert asyncio.run(approvals.drain(conn, bookpath))["attempted"] == 0


def test_expired_approval_does_not_attempt_submission(draft):
    conn, key, _, bookpath, _ = draft
    _, auth = approve(draft)
    auth["expires_at"] = datetime.fromtimestamp(time.time()-1, timezone.utc).isoformat()
    booklet.write_private(__import__('pathlib').Path(auth["authorization_path"]), auth)
    assert asyncio.run(approvals.drain(conn, bookpath))["attempted"] == 0


@pytest.mark.parametrize("failure,code", [
    ("role_fit", "role_fit_not_eligible"), ("unsupported_fit", "role_fit_not_verified"),
    ("eligibility", "eligibility_not_verified"), ("application_state", "application_not_waiting_review"),
])
def test_zero_attempt_approval_retains_a_safe_actionable_reason(draft, monkeypatch, failure, code):
    from jhb import eligibility
    from jhb.applications import role_fit
    conn, key, path, bookpath, _ = draft
    job = json.loads(path.read_text())["job"]
    text = "Build commercial Python software."
    description = {"text": text, "source_url": job["url"], "status": "verified", "retrieved_at": time.time(),
                   "job_identity": list(boards.job_identity(job["url"])), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    booklet.write_private(path.parent / "eligibility.json", {"state": "eligible", "policy": eligibility.POLICY_ID,
        "description": description})
    fit = {"state": "skipped", "source": role_fit.POLICY, "selected_role": "sde", "mode": "independent_codex"}
    if failure == "unsupported_fit":
        fit.update(state="eligible", mode="interactive_candidate_selected_job")
    booklet.write_private(path.parent / "role-fit.json", fit)
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    if failure == "eligibility":
        booklet.write_private(path.parent / "eligibility.json", {"state": "skipped", "policy": eligibility.POLICY_ID,
            "description": description})
    approve(draft)
    if failure == "application_state":
        conn.execute("UPDATE applications SET state='skipped'");conn.commit()
    before = path.read_bytes()
    async def forbidden(*args, **kwargs):
        pytest.fail("Rejected preflight reached terminal submission")
    result = asyncio.run(approvals.drain(conn, bookpath, submitter=forbidden))
    assert result["attempted"] == 0 and result["submitted"] == 0 and result["handoffs"] == 1
    assert result["blocked"][0]["reason_code"] == code and result["blocked"][0]["reason"]
    approval = conn.execute("SELECT state,result_json FROM application_approvals").fetchone()
    assert approval["state"] == "needs_review"
    assert json.loads(approval["result_json"])["reason_code"] == code
    assert path.read_bytes() == before
    assert conn.execute("SELECT count(*) FROM authorized_submission_attempts").fetchone()[0] == 0
