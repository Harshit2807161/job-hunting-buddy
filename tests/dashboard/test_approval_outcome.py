"""A failed approval is visible without renewing authority or touching Chrome."""
import json
import time

from test_dashboard import portal, add_job
from jhb.applications import approvals, overnight


def test_consumed_approval_explains_no_click_and_expiry(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, complete=True)
    approvals.initialize(conn)
    overnight.initialize(conn)
    now = int(time.time())
    conn.execute("INSERT INTO application_approvals VALUES(?,?,?,?,?,?,?,?)", (
        "a" * 32, job["dedupe_hash"], "needs_review", now - 8000, now - 800,
        "r", "synthetic-authorization", json.dumps({"attempted": 1, "submitted": 0})))
    conn.execute("INSERT INTO authorized_submission_attempts "
        "(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path,result_json) "
        "VALUES(?,?,?,?,?,?,?,?)", (job["dedupe_hash"], "a", job["url"], "waiting_review", now - 7999,
        now - 7998, "synthetic-attempt", json.dumps({"reason": "An approved answer or document did not remain intact", "click_started": False})))
    conn.commit()
    detail = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    outcome = detail["approval_outcome"]
    assert outcome["click_started"] is False and outcome["expired"] is True
    assert "could not verify" in outcome["reason"] and "edits were preserved" in outcome["reason"]
    assert detail["display_state"] == "needs_review"
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "needs_review"
    # A later consumed approval cannot inherit the earlier attempt's diagnosis.
    conn.execute("INSERT INTO application_approvals VALUES(?,?,?,?,?,?,?,?)", (
        "b" * 32, job["dedupe_hash"], "needs_review", now, now + 7200, "r2", "synthetic-new",
        json.dumps({"attempted": 0, "submitted": 0, "reason": "Role-fit review requires attention"})))
    conn.commit()
    outcome = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()["approval_outcome"]
    assert outcome["click_started"] is None and not outcome["expired"]
    assert outcome["reason"] == "Role-fit review requires attention"


def test_no_failed_approval_has_no_failure_notice(portal):
    root, conn, book, client, headers = portal
    job, _, _ = add_job(conn, root, complete=True)
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()["approval_outcome"] is None
