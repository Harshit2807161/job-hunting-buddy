from datetime import datetime, timezone
import json

import pytest

from jhb import config
from jhb.applications import boards, booklet, pipeline, queue, source_queue, tracking
from tests.applications.test_pipeline import setup, job

ASHBY = "https://jobs.ashbyhq.com/example/19eb22cd-9540-49ed-840b-6422714413b5"


def historical(conn, url=ASHBY):
    candidate = {"url": url, "dedupe_hash": boards.application_hash(url), "company": "Synthetic Company", "title": "Software Engineer"}
    receipt = config.ROOT / "private" / "historical" / "receipt.json"
    booklet.write_private(receipt, {"state": "submitted", "url": url, "confirmed_at": datetime.now(timezone.utc).isoformat(),
                                   "confirmation": "Thank you for applying", "body": "Thank you for applying. Your application has been received.",
                                   "source": "Live Ashby success page", "target_id": "synthetic-target"})
    assert tracking.record_confirmed(conn, candidate, receipt)["state"] == "submitted"
    conn.execute("DELETE FROM applications"); conn.commit()  # Manual confirmation predates the queue.
    return candidate, receipt


def test_exact_historical_confirmation_is_restored_before_source_replay_or_filler(setup):
    conn, book = setup
    original, _ = historical(conn)
    source_queue.enqueue(conn, [job("wrapper", "https://example.test/job")])
    source = source_queue.claim(conn)
    evidence = pipeline._source_artifact(source, {"state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY})
    source_queue.finish(conn, source["source_job_hash"], "resolved", board="ashby", application_url=ASHBY, evidence_path=evidence)
    async def forbidden(*args, **kwargs): pytest.fail("A receipt-confirmed manual application was replayed")
    summary = pipeline.run_cycle(conn, book, resolver=forbidden, runner=forbidden)
    assert summary["applications_prepared"] == summary["applications_queued"] == 0
    assert summary["historical_confirmations_reconciled"] == 1
    assert conn.execute("SELECT state FROM applications WHERE job_hash=?", (original["dedupe_hash"],)).fetchone()[0] == "submitted"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert summary["pending_question_ids"] == []


def test_direct_enqueue_reconciles_application_variant_without_queueing_it(setup):
    conn, _ = setup
    historical(conn)
    assert queue.enqueue(conn, [job("rediscovered", ASHBY+"/application?ref=another")]) == 0
    assert queue.claim(conn) is None
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"


def test_direct_claim_reconciles_an_existing_stale_queued_row_before_browser(setup):
    conn, _ = setup
    candidate, _ = historical(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,'queued',0)",
                 (candidate["dedupe_hash"], json.dumps(candidate))); conn.commit()
    assert queue.claim(conn) is None
    assert conn.execute("SELECT state,attempts FROM applications").fetchone()[:] == ("submitted", 0)


@pytest.mark.parametrize("damage", ["missing", "changed", "proof_json", "wrong_job", "malformed_stored_url",
                                   "mismatched_stored_url", "mismatched_job_json_url"])
def test_unverifiable_prior_confirmation_is_a_technical_hold_never_a_new_application(setup, damage):
    conn, book = setup
    candidate, receipt = historical(conn)
    if damage == "missing": receipt.unlink()
    elif damage == "changed":
        data = json.loads(receipt.read_text()); data["body"] += " changed"; booklet.write_private(receipt, data)
    elif damage == "wrong_job":
        data = json.loads(receipt.read_text()); data["url"] = ASHBY.replace("example", "another"); booklet.write_private(receipt, data)
    elif damage == "malformed_stored_url":
        conn.execute("UPDATE confirmed_submissions SET application_url='malformed:////'"); conn.commit()
    elif damage == "mismatched_stored_url":
        conn.execute("UPDATE confirmed_submissions SET application_url=?", (ASHBY.replace("example", "another"),)); conn.commit()
    elif damage == "mismatched_job_json_url":
        conn.execute("UPDATE confirmed_submissions SET job_json=?", (json.dumps({**candidate, "url": ASHBY.replace("example", "another")}),)); conn.commit()
    else: conn.execute("UPDATE confirmed_submissions SET proof_json='{' "); conn.commit()
    assert queue.enqueue(conn, [candidate]) == 0
    async def forbidden(*args, **kwargs): pytest.fail("Damaged prior receipt must not permit reapplication")
    summary = pipeline.run_cycle(conn, book, resolver=forbidden, runner=forbidden)
    assert summary["applications_prepared"] == 0 and summary["pending_question_ids"] == []
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"


def test_damaged_archival_receipt_does_not_downgrade_existing_submitted_row(setup):
    conn, _ = setup
    candidate, receipt = historical(conn)
    tracking.record_confirmed(conn, candidate, receipt)
    receipt.unlink()
    assert tracking.restore_confirmed_applications(conn) == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"


def test_other_exact_job_is_not_blocked_by_same_employer_confirmation(setup):
    conn, _ = setup
    historical(conn)
    another = ASHBY.replace("19eb22cd", "29eb22cd")
    assert queue.enqueue(conn, [job("new-role", another)]) == 1
    claimed = queue.claim(conn)
    assert claimed["job"]["url"] == another


def test_later_valid_canonical_receipt_wins_over_damaged_exact_key_row(setup):
    conn, _ = setup
    historical(conn)
    stored = conn.execute("SELECT * FROM confirmed_submissions").fetchone()
    conn.execute("UPDATE confirmed_submissions SET application_url='malformed:////'")
    conn.execute("INSERT INTO confirmed_submissions VALUES(?,?,?,?,?,?,?)", ("synthetic-legacy-key", *tuple(stored)[1:])); conn.commit()
    proof = tracking.confirmed_application(conn, ASHBY)
    assert proof["verified"] is True
    assert queue.enqueue(conn, [job("same", ASHBY)]) == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"


def test_canonical_lookup_with_nonmatching_legacy_key_still_holds_unverifiable_proof(setup):
    conn, _ = setup
    candidate, _ = historical(conn)
    conn.execute("UPDATE confirmed_submissions SET submission_key='conflicting-legacy-key',proof_json='{}'"); conn.commit()
    assert queue.enqueue(conn, [candidate]) == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
