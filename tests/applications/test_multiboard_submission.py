"""Synthetic finite scope/import/terminal invariants across recognized boards."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time

import pytest

from jhb import config
from jhb.applications import boards, booklet, overnight, queue, tracking
from jhb.eligibility import POLICY_ID

ASHBY = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"
WORKABLE = "https://apply.workable.com/example/j/A1B2C3D4/apply"
GH = "https://job-boards.greenhouse.io/example/jobs/123"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "1")
    monkeypatch.setitem(boards.ADAPTERS["ashby"], "submit_enabled", True)
    monkeypatch.setitem(boards.ADAPTERS["workable"], "submit_enabled", False)
    now = int(time.time())
    auth = {"role": "user", "status": "verified", "enabled": True, "scope": overnight.MULTI_SCOPE,
            "boards": ["greenhouse", "ashby", "workable", "workday", "lever", "smartrecruiters", "icims"],
            "candidate_job_policy": overnight.MULTI_JOB_POLICY,
            "content": "Continue applying and submit everything on all job boards for seven hours",
            "authorized_at": datetime.fromtimestamp(now-60, timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(now+3600, timezone.utc).isoformat(),
            "require_browser_double_check": True, "require_independent_review": True,
            "pause_unknown_answers": True, "require_receipt_before_sheet": True}
    auth_path = tmp_path / "private" / overnight.AUTH_NAME
    booklet.write_private(auth_path, auth)
    resume = tmp_path / "private" / "synthetic-sde.pdf"
    resume.write_bytes(b"%PDF- synthetic test document")
    book_path = tmp_path / "private" / "booklet.json"
    booklet.write_private(book_path, {"schema_version": 1, "answers": {}, "custom_answers": {},
        "roles": {"sde": {"documents.resume": booklet.answer(str(resume), "Synthetic approved SDE resume")}, "ml": {}}})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    overnight.initialize(conn)
    yield conn, book_path, auth_path, now
    conn.close()


def manual_packet(setup, *, url=ASHBY, missing=None, eligibility=True):
    conn, book_path, _, now = setup
    key = boards.application_hash(url)
    job = {"dedupe_hash": key, "source": "explicit_manual", "title": "Software Engineer", "company": "Example", "url": url,
           "role_classes": "swe"}
    path = config.ROOT / "private" / "manual-drafts" / key / "packet.json"
    booklet.write_private(path, {"job": job, "state": "waiting_review", "submitted": False, "missing": missing or [],
        "filled": [{"ref": "resume", "question": "Resume", "key": "documents.resume",
                    "value": booklet.load(book_path)["roles"]["sde"]["documents.resume"]["value"],
                    "source": "Synthetic approved SDE resume"}]})
    if eligibility:
        from jhb.applications.job_context import description_source
        text = "Build software. Security clearance is not required."
        description = {"status": "verified", "text": text, "source_url": description_source(url),
                       "job_identity": list(boards.job_identity(url)), "retrieved_at": now,
                       "sha256": hashlib.sha256(text.encode()).hexdigest()}
        booklet.write_private(path.parent / "eligibility.json", {"state": "eligible", "policy": POLICY_ID, "description": description})
    return job, path


@pytest.mark.parametrize("changes", [
    {"boards": []}, {"boards": ["ashby", "anything"]}, {"boards": ["ashby", "ashby"]},
    {"candidate_job_policy": "submit anything without a draft"}, {"require_independent_review": False},
    {"content": "Apply to one named job"}, {"content": "Do not submit everything on all job boards"},
    {"pause_unknown_answers": False}, {"require_receipt_before_sheet": False},
])
def test_multi_board_authority_is_explicit_and_preserves_guards(setup, changes):
    _, _, path, _ = setup
    booklet.write_private(path, {**json.loads(path.read_text()), **changes})
    assert overnight.load_authorization() is None


@pytest.mark.parametrize("change,valid", [({}, True), ({"require_complete_inventory": False}, False),
    ({"require_independent_review": False}, False), ({"content": "Keep submitting applications throughout the night"}, False),
    ({"content": "Remove the final approval step and use a subagent; do not submit applications"}, False)])
def test_explicit_delegated_final_review_still_requires_finite_complete_evidence(setup, change, valid):
    _, _, path, _ = setup
    value = {**json.loads(path.read_text()), "approval_mode": overnight.INDEPENDENT_MODE,
             "require_complete_inventory": True,
             "content": "Keep working on submitting applications throughout the night. Remove the final approval step, instead use a new subagent that does doublechecking."}
    booklet.write_private(path, {**value, **change})
    assert (overnight.load_authorization() is not None) is valid


def test_delegating_review_never_makes_a_legacy_incomplete_draft_submittable(setup):
    conn, book_path, path, _ = setup
    job, packet = manual_packet(setup)
    overnight.register_manual_draft(conn, packet)
    booklet.write_private(path, {**json.loads(path.read_text()), "approval_mode": overnight.INDEPENDENT_MODE,
                                "require_complete_inventory": True})
    async def forbidden(*args, **kwargs):
        pytest.fail("Incomplete inventory reached the browser under delegated review")
    assert asyncio.run(overnight.drain(conn, book_path, submitter=forbidden))["attempted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM authorized_submission_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("url", [ASHBY, GH])
def test_explicit_finite_scope_accepts_verified_existing_manual_draft_without_phase1_row(setup, url):
    conn, book_path, _, _ = setup
    job, packet = manual_packet(setup, url=url)
    assert overnight.register_manual_draft(conn, packet) == 1
    assert overnight.register_manual_draft(conn, packet) == 0
    calls = []
    async def audit_only(job, packet_path, manifest, *, authorization, attempt):
        calls.append(job["url"])
        assert manifest["selected_role"] == "sde"
        assert authorization["require_independent_review"] is True
        assert json.loads(attempt.read_text())["runtime_click_started"] is False
        return {"state": "waiting_review", "reason": "Synthetic audit only; no terminal click"}
    result = asyncio.run(overnight.drain(conn, book_path, submitter=audit_only))
    assert result["attempted"] == 1 and result["submitted"] == 0 and calls == [url]
    # A nonretryable review handoff is not an automatic second submit attempt.
    assert asyncio.run(overnight.drain(conn, book_path, submitter=audit_only))["attempted"] == 0


@pytest.mark.parametrize("reason", ["disabled_adapter", "no_eligibility", "pending_question", "outside_boards", "confirmed"])
def test_manual_scope_never_bypasses_missing_proof_or_terminal_capability(setup, reason):
    conn, book_path, auth_path, _ = setup
    job, path = manual_packet(setup, url=WORKABLE if reason == "disabled_adapter" else ASHBY,
                              eligibility=reason != "no_eligibility")
    overnight.register_manual_draft(conn, path)
    if reason == "pending_question":
        book = booklet.load(book_path)
        book["question_handoffs"] = {"question": {"status": "pending", "contexts": {
            job["dedupe_hash"]: {"required": True, "resolved": False}}}}
        booklet.write_private(book_path, book)
    elif reason == "outside_boards":
        booklet.write_private(auth_path, {**json.loads(auth_path.read_text()), "boards": ["greenhouse"]})
    elif reason == "confirmed":
        tracking.initialize(conn)
        conn.execute("INSERT INTO confirmed_submissions VALUES(?,?,?,?,?,?,?)", ("synthetic-confirmed", "ashby", job["url"], "{}", "2026-01-01T00:00:00+00:00", "{}", 1000))
        conn.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Unapproved manual scope must not reach a terminal controller")
    result = asyncio.run(overnight.drain(conn, book_path, submitter=forbidden))
    assert result["attempted"] == 0


@pytest.mark.parametrize("state", ["submitted", "submission_uncertain", "running"])
def test_manual_import_does_not_replace_terminal_or_owned_queue_state(setup, state):
    conn, _, _, _ = setup
    job, path = manual_packet(setup)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,1000)",
                 (job["dedupe_hash"], json.dumps(job), state))
    conn.commit()
    assert overnight.register_manual_draft(conn, path) == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == state


def test_non_greenhouse_confirmed_receipt_also_closes_canonical_application_queue(setup):
    conn, _, _, _ = setup
    job, _ = manual_packet(setup)
    receipt = config.ROOT / "private" / "submitted.json"
    booklet.write_private(receipt, {"state": "submitted", "url": job["url"], "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "confirmation": "Your application was successfully submitted", "body": "Your application was successfully submitted",
        "source": "Live Browser Use CLI success page", "target_id": "synthetic-owned-tab"})
    assert tracking.record_confirmed(conn, job, receipt)["state"] == "submitted"
    row = conn.execute("SELECT job_hash,state FROM applications").fetchone()
    assert row["job_hash"] == boards.application_hash(job["url"]) and row["state"] == "submitted"
    assert overnight.register_manual_draft(conn, manual_packet(setup)[1]) == 0


@pytest.mark.parametrize("clicked", [False, True])
def test_renewed_authority_rearms_only_no_click_review_without_resetting_budget(setup, clicked):
    conn, book_path, auth_path, _ = setup
    job, path = manual_packet(setup)
    overnight.register_manual_draft(conn, path)
    calls = []
    async def audit_only(job, packet_path, manifest, *, authorization, attempt):
        calls.append(True)
        if clicked:
            booklet.write_private(attempt, {**json.loads(attempt.read_text()), "runtime_click_started": True})
        return {"state": "waiting_review", "reason": "Synthetic no-retry review"}
    assert asyncio.run(overnight.drain(conn, book_path, submitter=audit_only))["attempted"] == 1
    if clicked:
        conn.execute("UPDATE applications SET state='waiting_review'")  # Deliberately stale controller.
        conn.commit()
    authority = json.loads(auth_path.read_text())
    booklet.write_private(auth_path, {**authority, "content": authority["content"] + ". Renewed explicit authorization."})
    result = asyncio.run(overnight.drain(conn, book_path, submitter=audit_only))
    assert result["attempted"] == (0 if clicked else 1)
    assert len(calls) == (1 if clicked else 2)
    if not clicked:
        conn.execute("UPDATE authorized_submission_attempts SET attempt_count=3")
        conn.commit()
        booklet.write_private(auth_path, {**authority, "content": authority["content"] + ". Another explicit renewal."})
        assert asyncio.run(overnight.drain(conn, book_path, submitter=audit_only))["attempted"] == 0
