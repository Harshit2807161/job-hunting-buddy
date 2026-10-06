"""Synthetic authorization, submission and receipt orchestration; no live browser."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import pytest

from jhb import config, store
from jhb.applications import booklet, overnight, queue, source_queue, tracking, worker  # noqa: F401
from jhb.eligibility import POLICY_ID, description_url


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "1")
    now = int(time.time())
    auth = {"role": "user", "status": "verified", "enabled": True,
            "content": "Keep submitting new phase 1 jobs through the night after double checks",
            "authorized_at": datetime.fromtimestamp(now-60, timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(now+3600, timezone.utc).isoformat(),
            "scope": overnight.SCOPE, "board": "greenhouse", "require_browser_double_check": True,
            "pause_unknown_answers": True, "require_receipt_before_sheet": True}
    authpath = tmp_path / "private" / overnight.AUTH_NAME
    booklet.write_private(authpath, auth)
    resume = tmp_path / "private" / "sde.pdf"
    resume.write_bytes(b"%PDF synthetic resume")
    book = {"schema_version": 1, "answers": {}, "roles": {
        "sde": {"documents.resume": booklet.answer(str(resume), "synthetic verified resume")}, "ml": {}},
        "custom_answers": {}}
    bookpath = tmp_path / "private" / "book.json"
    booklet.write_private(bookpath, book)
    db = store.connect(tmp_path / "jobs.sqlite3")
    source_queue.initialize(db)
    queue.initialize(db)
    yield db, bookpath, authpath, now
    db.close()


def candidate(setup, *, sid="1", seen=None, missing=None):
    db, bookpath, authpath, now = setup
    source = store.Job("synthetic", sid, "Example", "Software Engineer",
                       f"https://job-boards.greenhouse.io/example/jobs/{sid}", role_classes=["swe"])
    store.upsert_jobs(db, [source])
    db.execute("UPDATE jobs SET first_seen=? WHERE dedupe_hash=?", (seen or now, source.dedupe_hash))
    db.commit()
    source_queue.enqueue(db, [source])
    checked = source_queue.claim(db)
    source_queue.finish(db, checked["source_job_hash"], "resolved", board="greenhouse", application_url=source.url)
    queue.enqueue(db, [source])
    claimed = queue.claim(db)
    job = claimed["job"]
    directory = config.ROOT / "private" / "applications" / claimed["job_hash"]
    booklet.write_private(directory / "packet.json", {"job": job, "state": "waiting_review", "submitted": False,
        "missing": missing or [], "filled": [{"ref": "resume", "key": "documents.resume", "question": "Resume",
        "value": booklet.load(bookpath)["roles"]["sde"]["documents.resume"]["value"], "source": "synthetic verified resume"}]})
    text = "Build software systems. No security clearance is required."
    description = {"status": "verified", "text": text, "source_url": description_url(job),
                   "retrieved_at": now, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    booklet.write_private(directory / "eligibility.json", {"state": "eligible", "policy": POLICY_ID, "description": description})
    queue.finish(db, claimed["job_hash"], "waiting_review", directory / "review.html")
    return job


def success(job, attempt):
    receipt = Path(attempt).parent / "receipt.json"
    booklet.write_private(receipt, {"state": "submitted", "url": job["url"],
        "confirmed_at": datetime.now(timezone.utc).isoformat(), "confirmation": "Thank you for applying",
        "body": "Thank you for applying. We received your application.",
        "source": "Live Greenhouse success page after authorized overnight submission", "target_id": "synthetic-target",
        "check_count": 2, "authorization_id": json.loads(Path(attempt).read_text())["authorization_id"]})
    return {"state": "submitted", "receipt_path": str(receipt), "check_count": 2}


@pytest.mark.parametrize("change", [{"enabled": False}, {"status": "needs_input"}, {"role": "assistant"},
    {"scope": "all existing drafts"}, {"board": "ashby"}, {"pause_unknown_answers": False},
    {"content": "Do not keep submitting phase 1 jobs through the night"},
    {"expires_at": "2026-01-01T00:00:00+00:00"}, {"expires_at": "2030-01-01T00:00:00+00:00"},
    {"authorized_at": "2026-10-04T01:00:00"}])
def test_invalid_authorization_fails_closed(setup, change):
    _, _, path, _ = setup
    booklet.write_private(path, {**json.loads(path.read_text()), **change})
    assert overnight.load_authorization() is None


def test_separate_runtime_gate_is_off_by_default(setup, monkeypatch):
    monkeypatch.delenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED")
    assert overnight.load_authorization() is None


def chat_authorization(setup):
    _, _, path, _ = setup
    auth = json.loads(path.read_text())
    auth.update(scope=overnight.MULTI_SCOPE, candidate_job_policy=overnight.MULTI_JOB_POLICY,
                boards=["greenhouse", "ashby"], source="explicit_user_message", action="enable_autonomy",
                approval_mode=overnight.INDEPENDENT_MODE, require_independent_review=True,
                require_complete_inventory=True,
                content="Turn on autonomy mode and keep submitting new job applications through the night")
    return auth


def test_fresh_chat_delegation_keeps_portal_fallback_and_runtime_gate(setup, monkeypatch):
    _, _, path, _ = setup
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    booklet.write_private(path, chat_authorization(setup))
    assert overnight.load_authorization()["source"] == "explicit_user_message"
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "0")
    assert overnight.load_authorization() is None


@pytest.mark.parametrize("change", [
    {"source": "job_description"}, {"action": "approve"}, {"role": "assistant"},
    {"require_complete_inventory": False}, {"require_independent_review": False},
    {"content": "Do not turn on autonomy mode and keep submitting applications through the night"},
    {"content": "Pause autonomy mode and keep submitting applications through the night"},
    {"content": "Turn on autonomy mode through the night"},
    {"content": "Keep submitting applications through the night"},
    {"content": "Turn on autonomy mode and prepare jobs through the night"},
])
def test_chat_delegation_requires_explicit_fresh_scoped_intent(setup, change):
    _, _, path, _ = setup
    booklet.write_private(path, {**chat_authorization(setup), **change})
    assert overnight.load_authorization() is None


def test_new_user_exclusion_blocks_old_ready_draft_before_any_submit_attempt(setup):
    db, bookpath, _, _ = setup
    job = candidate(setup)
    book = booklet.load(bookpath)
    book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "Synthetic explicit user rejection",
                                                 "reason": "This exact job is unsuitable"}}
    booklet.write_private(bookpath, book)
    async def forbidden(*args, **kwargs):
        pytest.fail("Rejected job must not enter live submission dispatcher")
    result = asyncio.run(overnight.drain(db, bookpath, submitter=forbidden))
    assert result["attempted"] == 0
    assert db.execute("SELECT COUNT(*) FROM authorized_submission_attempts").fetchone()[0] == 0


def test_official_title_exclusion_blocks_legacy_ready_packet_before_terminal_dispatch(setup):
    db, bookpath, _, _ = setup
    job = candidate(setup)
    path = config.ROOT / 'private' / 'applications' / job['dedupe_hash'] / 'eligibility.json'
    evidence = json.loads(path.read_text())
    evidence['description']['title'] = 'AI/ML Engineer 1 Top Secret/SCI w/Poly'
    booklet.write_private(path, evidence)
    async def forbidden(*args, **kwargs): pytest.fail('Excluded official title reached terminal dispatcher')
    result = asyncio.run(overnight.drain(db, bookpath, submitter=forbidden))
    assert result['attempted'] == 0


@pytest.mark.parametrize("change,allowed", [(None, True), ("resume_evidence", False), ("deterministic", False), ("role", False)])
def test_modern_submit_requires_current_independent_role_fit_evidence(setup, monkeypatch, change, allowed):
    from jhb.applications import role_fit
    db, bookpath, _, _ = setup
    job = candidate(setup)
    book = booklet.load(bookpath)
    book["roles"]["sde"]["role.experience"] = booklet.answer("Synthetic backend internship", "Synthetic original resume")
    booklet.write_private(bookpath, book)
    directory = config.ROOT / "private" / "applications" / job["dedupe_hash"]
    description = json.loads((directory / "eligibility.json").read_text())["description"]
    fit = {"state": "eligible", "source": role_fit.POLICY, "selected_role": "sde", "mode": "independent_codex",
           "evidence_hash": role_fit.evidence_hash({**job, "verified_job_description": description}, book, "sde")}
    if change == "deterministic": fit["mode"] = "deterministic"
    if change == "role": fit["selected_role"] = "ml"
    if change == "resume_evidence":
        book["roles"]["sde"]["role.experience"] = booklet.answer("Different verified resume", "Synthetic changed original source")
        booklet.write_private(bookpath, book)
    booklet.write_private(directory / "role-fit.json", fit)
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    called = []
    async def submitter(job, packet, answers, *, authorization, attempt):
        called.append(job["dedupe_hash"])
        return success(job, attempt)
    result = asyncio.run(overnight.drain(db, bookpath, submitter=submitter))
    assert result["attempted"] == int(allowed)
    assert bool(called) is allowed


def test_new_job_attempt_is_durable_before_submit_and_receipt_tracks_once(setup):
    db, bookpath, _, _ = setup
    job = candidate(setup)
    calls = []
    async def submit(job, packet, manifest, *, authorization, attempt):
        row = db.execute("SELECT * FROM authorized_submission_attempts").fetchone()
        assert row["state"] == "in_progress"
        data = json.loads(Path(attempt).read_text())
        assert data["authorization_id"] == authorization["authorization_id"]
        assert hashlib.sha256(Path(packet).read_bytes()).hexdigest() == data["packet_sha256"]
        assert manifest["selected_role"] == "sde"
        calls.append(job["dedupe_hash"])
        return success(job, attempt)
    result = asyncio.run(overnight.drain(db, bookpath, submitter=submit))
    assert result["submitted"] == result["attempted"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert asyncio.run(overnight.drain(db, bookpath, submitter=submit))["attempted"] == 0
    assert calls == [job["dedupe_hash"]]


@pytest.mark.parametrize("case", ["old", "missing", "blocked", "ledger"])
def test_old_or_incomplete_or_ineligible_draft_never_reaches_submit(setup, case):
    db, bookpath, _, now = setup
    job = candidate(setup, seen=now-120 if case == "old" else now,
                    missing=[{"question": "New factual answer"}] if case == "missing" else None)
    directory = config.ROOT / "private" / "applications" / job["dedupe_hash"]
    if case == "blocked":
        data = json.loads((directory / "eligibility.json").read_text())
        data["state"] = "skipped"
        booklet.write_private(directory / "eligibility.json", data)
    if case == "ledger":
        book = booklet.load(bookpath)
        book["question_handoffs"] = {"q_test": {"status": "pending", "contexts": {
            job["dedupe_hash"]: {"required": True}}}}
        booklet.write_private(bookpath, book)
    async def forbidden(*args, **kwargs):
        pytest.fail("Unsafe job must never reach the submission client")
    result = asyncio.run(overnight.drain(db, bookpath, submitter=forbidden))
    assert result["attempted"] == 0


def test_uncertain_click_never_replays_and_never_creates_tracker_record(setup):
    db, bookpath, _, _ = setup
    candidate(setup)
    calls = []
    async def uncertain(*args, **kwargs):
        calls.append(True)
        data = json.loads(Path(kwargs["attempt"]).read_text())
        booklet.write_private(kwargs["attempt"], {**data, "runtime_click_started": True})
        raise TimeoutError("Synthetic uncertain browser transport")
    first = asyncio.run(overnight.drain(db, bookpath, submitter=uncertain))
    assert first["uncertain"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
    assert asyncio.run(overnight.drain(db, bookpath, submitter=uncertain))["attempted"] == 0
    assert calls == [True]
    tracking.initialize(db)
    assert db.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0


def test_receipt_without_two_checks_stays_uncertain(setup):
    db, bookpath, _, _ = setup
    candidate(setup)
    async def missing_checks(job, packet, manifest, *, authorization, attempt):
        result = success(job, attempt)
        receipt = Path(result["receipt_path"])
        booklet.write_private(receipt, {**json.loads(receipt.read_text()), "check_count": 1})
        return {**result, "check_count": 1}
    assert asyncio.run(overnight.drain(db, bookpath, submitter=missing_checks))["uncertain"] == 1
    assert asyncio.run(overnight.drain(db, bookpath, submitter=missing_checks))["reconciled"] == 0


def test_terminal_marker_without_receipt_does_not_record_submission(setup):
    db, bookpath, _, _ = setup
    candidate(setup)
    async def no_receipt(*args, **kwargs):
        return {"state": "submitted", "check_count": 2}
    result = asyncio.run(overnight.drain(db, bookpath, submitter=no_receipt))
    assert result["uncertain"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"


def test_positive_receipt_recovers_after_process_interruption_without_second_click(setup, monkeypatch):
    db, bookpath, authpath, _ = setup
    job = candidate(setup)
    async def interrupted(job, packet, manifest, *, authorization, attempt):
        success(job, attempt)
        raise TimeoutError("Receipt persisted but caller lost its response")
    first = asyncio.run(overnight.drain(db, bookpath, submitter=interrupted))
    assert first["uncertain"] == 1
    monkeypatch.delenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED")
    async def forbidden(*args, **kwargs):
        pytest.fail("Receipt recovery must never click")
    recovered = asyncio.run(overnight.drain(db, bookpath, submitter=forbidden))
    assert recovered["reconciled"] == 1 and recovered["enabled"] is False
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1


def test_authorization_revoked_between_jobs_prevents_next_attempt(setup):
    db, bookpath, authpath, _ = setup
    candidate(setup, sid="1")
    candidate(setup, sid="2")
    async def submit(job, packet, manifest, *, authorization, attempt):
        result = success(job, attempt)
        booklet.write_private(authpath, {**json.loads(authpath.read_text()), "enabled": False})
        return result
    result = asyncio.run(overnight.drain(db, bookpath, limit=2, submitter=submit))
    assert result["attempted"] == result["submitted"] == 1
    assert db.execute("SELECT COUNT(*) FROM applications WHERE state='waiting_review'").fetchone()[0] == 1


def test_submission_handoff_preserves_runtime_attempt_flags_and_asks_new_question(setup):
    from jhb.applications import questions
    db, bookpath, _, _ = setup
    candidate(setup)
    async def handoff(job, packet, manifest, *, authorization, attempt):
        data = json.loads(Path(attempt).read_text())
        booklet.write_private(attempt, {**data, "runtime_click_started": False, "check_count": 1})
        return {"state": "waiting_input", "missing": [{"question": "A newly revealed screening fact?",
                "ref": "new-question", "type": "text", "required": True}]}
    result = asyncio.run(overnight.drain(db, bookpath, submitter=handoff))
    assert result["handoffs"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"
    path = Path(db.execute("SELECT attempt_path FROM authorized_submission_attempts").fetchone()[0])
    assert json.loads(path.read_text())["check_count"] == 1
    assert len(questions.pending(bookpath)) == 1
    assert asyncio.run(overnight.drain(db, bookpath, submitter=handoff))["attempted"] == 0


def test_receipt_for_another_authorization_never_confirms_or_recovers(setup):
    db, bookpath, _, _ = setup
    candidate(setup)
    async def wrong_authority(job, packet, manifest, *, authorization, attempt):
        result = success(job, attempt)
        receipt = Path(result["receipt_path"])
        booklet.write_private(receipt, {**json.loads(receipt.read_text()), "authorization_id": "another-window"})
        return result
    assert asyncio.run(overnight.drain(db, bookpath, submitter=wrong_authority))["uncertain"] == 1
    assert asyncio.run(overnight.drain(db, bookpath, submitter=wrong_authority))["reconciled"] == 0
    tracking.initialize(db)
    assert db.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0


def test_postclick_auth_challenge_is_uncertain_and_keeps_runtime_marker(setup):
    db, bookpath, _, _ = setup
    candidate(setup)
    async def challenge(job, packet, manifest, *, authorization, attempt):
        data = json.loads(Path(attempt).read_text())
        booklet.write_private(attempt, {**data, "runtime_click_started": True})
        return {"state": "waiting_captcha"}
    result = asyncio.run(overnight.drain(db, bookpath, submitter=challenge))
    assert result["uncertain"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
    path = Path(db.execute("SELECT attempt_path FROM authorized_submission_attempts").fetchone()[0])
    assert json.loads(path.read_text())["runtime_click_started"] is True


def test_pipeline_records_confirmed_submission_before_review_notification(setup, monkeypatch):
    from jhb.applications import pipeline, worker
    db, bookpath, _, _ = setup
    candidate(setup)
    observed = []
    def notice(conn, **kwargs):
        observed.append(conn.execute("SELECT state FROM applications").fetchone()[0])
    monkeypatch.setattr(worker, "notify_pending", notice)
    async def submit(job, packet, manifest, *, authorization, attempt):
        return success(job, attempt)
    summary = pipeline.run_cycle(db, bookpath, submission_runner=submit)
    assert summary["authorized_submissions"]["submitted"] == 1
    assert observed == ["submitted"]


def test_explicitly_answered_fresh_packet_can_retry_preclick_handoff_once(setup):
    from jhb.applications import questions
    db, bookpath, _, _ = setup
    job = candidate(setup)
    calls = []
    async def submit(job, packet, manifest, *, authorization, attempt):
        calls.append(True)
        if len(calls) == 1:
            return {"state": "waiting_input", "missing": [{"question": "New office preference?",
                    "ref": "office", "required": True, "type": "text"}]}
        return success(job, attempt)
    assert asyncio.run(overnight.drain(db, bookpath, submitter=submit))["handoffs"] == 1
    pending = questions.pending(bookpath)[0]
    questions.answer(pending["id"], "Example office", bookpath, connection=db)
    directory = config.ROOT / "private" / "applications" / job["dedupe_hash"]
    packet_path = directory / "packet.json"
    packet = json.loads(packet_path.read_text())
    packet["filled"].append({"ref": "office", "question": "New office preference?", "key": "custom.fixture",
                             "value": "Example office", "source": "Explicit synthetic user response"})
    booklet.write_private(packet_path, packet)
    queue.finish(db, job["dedupe_hash"], "waiting_review", directory / "review.html")
    second = asyncio.run(overnight.drain(db, bookpath, submitter=submit))
    assert second["submitted"] == 1 and calls == [True, True]
    assert db.execute("SELECT attempt_count FROM authorized_submission_attempts").fetchone()[0] == 2
    assert (config.ROOT / "private" / "authorized-submissions" / job["dedupe_hash"] / "attempt-1.json").exists()


@pytest.mark.parametrize("kind", ["TimeoutError", "application_history_transport"])
def test_preclick_transport_retry_has_durable_backoff_and_three_attempt_budget(setup, kind):
    db, bookpath, _, _ = setup
    candidate(setup)
    calls = []
    async def transient(*args, **kwargs):
        calls.append(True)
        return {"state": "waiting_review", "retryable": True, "error_kind": kind}
    first = asyncio.run(overnight.drain(db, bookpath, submitter=transient))
    assert first["handoffs"] == 1
    assert asyncio.run(overnight.drain(db, bookpath, submitter=transient))["attempted"] == 0
    for _ in range(2):
        db.execute("UPDATE authorized_submission_attempts SET available_at=0")
        db.commit()
        assert asyncio.run(overnight.drain(db, bookpath, submitter=transient))["attempted"] == 1
    db.execute("UPDATE authorized_submission_attempts SET available_at=0")
    db.commit()
    assert asyncio.run(overnight.drain(db, bookpath, submitter=transient))["attempted"] == 0
    assert len(calls) == 3
    assert db.execute("SELECT attempt_count FROM authorized_submission_attempts").fetchone()[0] == 3


def test_postclick_attempt_cannot_retry_even_if_packet_changes_and_state_is_review(setup):
    db, bookpath, _, _ = setup
    job = candidate(setup)
    async def clicked(job, packet, manifest, *, authorization, attempt):
        data = json.loads(Path(attempt).read_text())
        booklet.write_private(attempt, {**data, "runtime_click_started": True})
        return {"state": "waiting_review", "retryable": True, "error_kind": "TimeoutError"}
    assert asyncio.run(overnight.drain(db, bookpath, submitter=clicked))["uncertain"] == 1
    db.execute("UPDATE applications SET state='waiting_review'")  # Deliberately corrupt state; durable marker must still protect.
    db.execute("UPDATE authorized_submission_attempts SET state='waiting_review',available_at=0")
    db.commit()
    directory = config.ROOT / "private" / "applications" / job["dedupe_hash"]
    packet = json.loads((directory / "packet.json").read_text())
    booklet.write_private(directory / "packet.json", {**packet, "created_at": "changed"})
    async def forbidden(*args, **kwargs):
        pytest.fail("A post-click attempt must never be replayed")
    assert asyncio.run(overnight.drain(db, bookpath, submitter=forbidden))["attempted"] == 0
