"""Recover durable crash boundaries without authorizing any terminal replay."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path

import pytest

from jhb import config
from jhb.applications import approvals, booklet, overnight, service
from jhb.applications.application_review import snapshot_digest
from jhb.applications.authorized_submission import audit_hash
from test_approval_recovery import context, approve, attempt


def stalled(context, marker="absent"):
    conn, key, _ = context
    approve(context)
    row = conn.execute("SELECT * FROM application_approvals").fetchone()
    auth_path, auth, _ = overnight._read_private(row["authorization_path"])
    packet_path = Path(auth["binding"]["packet_path"])
    packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    auth["binding"]["packet_sha256"] = packet_sha
    auth["binding"]["document_sha256"] = {"documents.resume": "a"*64}
    booklet.write_private(auth_path, auth)
    path = None
    if marker != "absent":
        path = attempt(context, clicked=marker is True, malformed=marker is None)
        data = json.loads(path.read_text())
        data.update(application_url="https://job-boards.greenhouse.io/example/jobs/123",
                    packet_sha256=packet_sha, require_independent_review=True)
        booklet.write_private(path, data)
    conn.execute("UPDATE application_approvals SET state='submitting'"); conn.commit()
    return auth_path, path


def receipt(context, attempt_path):
    conn, key, _ = context
    data = json.loads(attempt_path.read_text())
    auth_id = data["authorization_id"]
    documents = {"documents.resume": {"sha256": "a"*64}}
    snapshot = {"fields": [{"ref": "name", "retained": True}]}
    checks = {"job_hash": key, "authorization_id": auth_id, "check_count": 2,
              "checks": [snapshot, snapshot], "documents": documents}
    booklet.write_private(attempt_path.parent / "checks.json", checks)
    review = {"verdict": "approved", "reviewer": "codex-readonly", "source": "independent_application_review",
              "issues": [], "snapshot_sha256": snapshot_digest(snapshot), "job_hash": key, "authorization_id": auth_id}
    token = {"verdict": "approved", "source": "independent_application_review", "review": review,
             "job_hash": key, "authorization_id": auth_id, "packet_sha256": data["packet_sha256"],
             "audit_sha256": audit_hash(snapshot, documents)}
    token_path = attempt_path.parent / "independent-review.json"
    booklet.write_private(token_path, token)
    proof = {"state": "submitted", "job_hash": key, "authorization_id": auth_id, "check_count": 2,
             "url": data["application_url"], "confirmed_at": datetime.now(timezone.utc).isoformat(),
             "confirmation": "Thank you for applying", "body": "Thank you for applying. We received your application.",
             "source": "Live Greenhouse success page", "target_id": "synthetic-target",
             "document_sha256": {"documents.resume": "a"*64}, "resume_sha256": "a"*64,
             "independent_review_sha256": hashlib.sha256(token_path.read_bytes()).hexdigest()}
    path = attempt_path.parent / "receipt.json"
    booklet.write_private(path, proof)
    assert overnight._checked_receipt(conn.execute("SELECT * FROM authorized_submission_attempts").fetchone(), proof)
    return path


@pytest.mark.parametrize("marker,expired,state", [("absent", False, "needs_review"), (False, False, "needs_review"),
                                                 (False, True, "expired"), (True, False, "uncertain"), (None, False, "uncertain")])
def test_crash_boundaries_never_automatically_reapprove_or_replay(context, marker, expired, state):
    conn, key, book = context
    auth_path, path = stalled(context, marker)
    originals = {p: p.read_bytes() for p in [auth_path, path] if p}
    if expired:
        conn.execute("UPDATE application_approvals SET expires_at=0"); conn.commit()
    result = approvals.recover_stalled(conn)
    assert result["recovered"] == 1 and result[state] == 1
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == state
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == ("submission_uncertain" if state == "uncertain" else "waiting_review")
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert approvals.recover_stalled(conn)["recovered"] == 0
    import asyncio
    async def forbidden(*args, **kwargs): pytest.fail("Recovery must never automatically submit")
    assert asyncio.run(approvals.drain(conn, book, submitter=forbidden))["attempted"] == 0


@pytest.mark.parametrize("change", ["missing_attempt", "corrupt_attempt", "authority_digest", "job_identity", "packet_digest"])
def test_unverifiable_or_mismatched_attempt_protects_terminal_uncertainty(context, change):
    conn, key, _ = context
    auth_path, path = stalled(context, False)
    if change == "missing_attempt": path.unlink()
    elif change == "corrupt_attempt": path.write_text("{")
    elif change == "authority_digest":
        data = json.loads(auth_path.read_text()); data["enabled"] = False; booklet.write_private(auth_path, data)
    else:
        data = json.loads(path.read_text())
        data["application_url" if change == "job_identity" else "packet_sha256"] = "https://job-boards.greenhouse.io/example/jobs/999" if change == "job_identity" else "f"*64
        booklet.write_private(path, data)
    assert approvals.recover_stalled(conn)["uncertain"] == 1
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"


def test_valid_receipt_after_click_before_database_completion_is_reconciled_once(context):
    conn, key, _ = context
    auth_path, path = stalled(context, True)
    proof = receipt(context, path)
    originals = {p: p.read_bytes() for p in [auth_path, path, proof]}
    result = approvals.recover_stalled(conn)
    assert result["submitted"] == 1 and result["uncertain"] == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert conn.execute("SELECT state FROM authorized_submission_attempts").fetchone()[0] == "submitted"
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "submitted"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert approvals.recover_stalled(conn)["submitted"] == 0


@pytest.mark.parametrize("change", ["expired", "broken_audit", "wrong_receipt", "unapproved_document", "recorder_failure"])
def test_receipt_recovery_validates_historical_proof_even_after_permission_expires(context, change):
    conn, _, _ = context
    _, path = stalled(context, True)
    proof_path = receipt(context, path)
    if change == "expired": conn.execute("UPDATE application_approvals SET expires_at=0"); conn.commit()
    elif change == "broken_audit": (path.parent / "checks.json").write_text("{}")
    elif change == "wrong_receipt":
        proof = json.loads(proof_path.read_text()); proof["authorization_id"] = "0"*64; booklet.write_private(proof_path, proof)
    elif change == "unapproved_document":
        # A self-consistent receipt/audit chain must still match the approved PDF.
        data = json.loads(path.read_text())
        auth_path = Path(data["authorization_path"])
        auth = json.loads(auth_path.read_text()); auth["binding"]["document_sha256"] = {"documents.resume": "b"*64}
        booklet.write_private(auth_path, auth)
        digest = hashlib.sha256(auth_path.read_bytes()).hexdigest()
        data["authorization_id"] = digest; booklet.write_private(path, data)
        conn.execute("UPDATE authorized_submission_attempts SET authorization_id=?", (digest,)); conn.commit()
        receipt(context, path)  # Rebuild a valid chain for a PDF absent from approval.
    def failing(*args): raise ConnectionError("Synthetic recorder interruption")
    result = approvals.recover_stalled(conn, recorder=failing if change == "recorder_failure" else None)
    assert result["submitted" if change == "expired" else "uncertain"] == 1


def test_recovery_does_not_downgrade_existing_submitted_or_touch_revoked_approval(context):
    conn, _, _ = context
    stalled(context, True)
    conn.execute("UPDATE applications SET state='submitted'")
    conn.execute("UPDATE authorized_submission_attempts SET state='submitted'"); conn.commit()
    assert approvals.recover_stalled(conn)["uncertain"] == 1
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert conn.execute("SELECT state FROM authorized_submission_attempts").fetchone()[0] == "submitted"
    conn.execute("UPDATE application_approvals SET state='revoked'"); conn.commit()
    assert approvals.recover_stalled(conn)["recovered"] == 0


def test_service_recovers_without_pending_approved_rows_only_under_both_locks(context, monkeypatch):
    conn, _, book = context
    stalled(context, False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    from jhb.applications import browser_connection
    monkeypatch.setattr(browser_connection, "available", lambda *args, **kwargs: True)
    original = approvals.recover_stalled
    calls = []
    def recover(connection):
        for name in ["approved-worker.lock", "application-worker.lock"]:
            with (config.ROOT / "private" / name).open("r+") as locked:
                with pytest.raises(BlockingIOError): fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(1)
        return original(connection)
    monkeypatch.setattr(approvals, "recover_stalled", recover)
    # Use a new connection because service owns and closes its connection.
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    from jhb import store
    result = service.once("approved", book_path=book, connector=lambda: store.connect(Path(database)))
    assert calls == [1] and result["recovered"] == 1
    assert result["reason_code"] == "approval_crash_recovered"
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "needs_review"
