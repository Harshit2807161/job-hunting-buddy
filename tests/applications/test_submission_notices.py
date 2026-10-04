"""Synthetic uncertain-outcome handoffs; no submission, SMTP or browser calls."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from jhb import config
from jhb.applications import booklet, notices, overnight, pipeline, queue, submission_notices


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", raising=False)
    clock = [1000]
    monkeypatch.setattr(notices.time, "time", lambda: clock[0])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    overnight.initialize(conn)
    sends = []
    monkeypatch.setattr("jhb.notify.send", lambda jobs, **kwargs: sends.append((jobs, kwargs)) or True)
    yield conn, clock, sends
    conn.close()


def uncertain(setup, *, job_id="123", clicked=True, attempt_state="uncertain", application_state="submission_uncertain", **attempt_changes):
    conn, _, _ = setup
    url = f"https://job-boards.greenhouse.io/example/jobs/{job_id}"
    job_hash = hashlib.sha256("|".join(queue.greenhouse_identity(url)).encode()).hexdigest()
    authorization_id = "a" * 64
    path = config.ROOT / "private" / "authorized-submissions" / job_hash / "attempt.json"
    job = {"dedupe_hash": job_hash, "title": "Synthetic Engineer", "company": "Example", "url": url,
           "candidate_answers": "never-email-this-synthetic-answer", "raw_error": "never-email-this-synthetic-error"}
    attempt = {"job_hash": job_hash, "authorization_id": authorization_id, "application_url": url,
               "state": attempt_state, "runtime_click_started": clicked, **attempt_changes}
    booklet.write_private(path, attempt)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,1000)",
                 (job_hash, json.dumps(job), application_state))
    conn.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,"
                 "started_at,updated_at,attempt_path,result_json) VALUES(?,?,?,?,1000,1000,?,?)",
                 (job_hash, authorization_id, url, attempt_state, str(path), json.dumps({"raw_error": "never-email-this-synthetic-error"})))
    conn.commit()
    return job, path


def test_local_diagnostic_does_not_consume_future_email_and_repeats_dedupe(setup):
    conn, _, sends = setup
    job, attempt = uncertain(setup)
    assert submission_notices.notify_uncertain(conn, send_email=False) == 0
    assert sends == []
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='application_notice_delivery'").fetchone() is None
    local = list((config.ROOT / "private" / "notifications").glob("submission-uncertain-*.json"))
    assert len(local) == 1 and local[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(local[0].read_text())["confirmed_submitted"] is False
    assert submission_notices.notify_uncertain(conn, send_email=True) == 1
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0
    assert len(sends) == 1
    jobs, kwargs = sends[0]
    assert jobs[0]["url"] == job["url"]
    assert kwargs["subject_prefix"] == "[Submission outcome review] "
    assert "Do not retry or resubmit" in kwargs["details"]
    assert "No validated submission receipt" in kwargs["details"]
    assert str(attempt.parent / "checks.json") in kwargs["details"]
    assert "never-email" not in json.dumps(sends)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
    assert conn.execute("SELECT state FROM authorized_submission_attempts").fetchone()[0] == "uncertain"


def test_smtp_failure_has_durable_category_backoff_across_new_jobs(setup, monkeypatch):
    conn, clock, _ = setup
    uncertain(setup)
    attempts = []
    success = [False]
    def send(jobs, **kwargs):
        attempts.append(jobs)
        if not success[0]:
            raise ConnectionError("synthetic transport failure containing untrusted data")
        return True
    monkeypatch.setattr("jhb.notify.send", send)
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0
    row = conn.execute("SELECT next_attempt_at,last_error FROM application_notice_channels").fetchone()
    assert row[0] == 1000 + notices.RETRY_BASE_SECONDS
    assert row[1] == "ConnectionError"
    uncertain(setup, job_id="456")
    clock[0] += 60
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0
    assert len(attempts) == 1
    clock[0] = 1000 + notices.RETRY_BASE_SECONDS
    success[0] = True
    assert submission_notices.notify_uncertain(conn, send_email=True) == 2
    assert len(attempts) == 2 and len(attempts[-1]) == 2
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0


@pytest.mark.parametrize("changes", [
    {"clicked": False}, {"clicked": None}, {"attempt_state": "waiting_input"},
    {"attempt_state": "submitted"}, {"application_state": "submitted"},
    {"application_state": "waiting_input"}, {"authorization_id": "different"},
    {"application_url": "https://job-boards.greenhouse.io/example/jobs/999"},
])
def test_no_click_question_submitted_and_mismatched_evidence_never_email(setup, changes):
    conn, _, sends = setup
    uncertain(setup, **changes)
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0
    assert sends == []


def test_confirmed_receipt_ledger_excludes_stale_uncertain_states(setup):
    conn, _, sends = setup
    job, _ = uncertain(setup)
    conn.execute("CREATE TABLE confirmed_submissions(application_url TEXT)")
    conn.execute("INSERT INTO confirmed_submissions VALUES(?)", (job["url"].replace("job-boards", "boards") + "?gh_src=synthetic",))
    conn.commit()
    assert submission_notices.notify_uncertain(conn, send_email=True) == 0
    assert sends == []


def test_notification_can_reconcile_expired_auth_without_reauthorizing_submission(setup):
    conn, _, sends = setup
    uncertain(setup)
    assert overnight.load_authorization() is None
    assert submission_notices.notify_uncertain(conn, send_email=True) == 1
    assert len(sends) == 1


def test_pipeline_hook_runs_after_overnight_reconciliation_with_email_flag(setup, monkeypatch):
    conn, _, _ = setup
    path = config.ROOT / "private" / "booklet.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    order = []
    async def drain(*args, **kwargs):
        order.append("reconcile")
        return {"attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 0, "reconciled": 0, "enabled": False}
    def handoff(*args, **kwargs):
        assert order == ["reconcile"]
        assert kwargs["send_email"] is False
        order.append("notice")
        return 0
    monkeypatch.setattr(overnight, "drain", drain)
    monkeypatch.setattr(submission_notices, "notify_uncertain", handoff)
    async def no_browser(*args, **kwargs):
        pytest.fail("An empty synthetic pipeline must not access a browser")
    result = asyncio.run(pipeline.cycle(conn, path, source_limit=1, application_limit=1,
                                       resolver=no_browser, runner=no_browser, send_email=False))
    assert result["submission_outcome_notices"] == 0
    assert order == ["reconcile", "notice"]
