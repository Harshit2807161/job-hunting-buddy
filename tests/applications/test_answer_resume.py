"""A saved response is a bounded outbox, never renewed submission authority."""
import copy
import json

import pytest

from jhb import config, store
from jhb.applications import answer_resume, booklet, questions, queue


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    conn = store.connect(tmp_path / "jobs.sqlite3")
    job = {"dedupe_hash": "1"*64, "source": "synthetic", "company": "Example", "title": "Software Engineer",
           "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    queue.enqueue(conn, [job])
    claimed = queue.claim(conn)
    job = claimed["job"]
    packet = {"state": "waiting_input", "job": job, "submitted": False, "filled": [], "missing": [
        {"question": "Synthetic employer availability?", "ref": "availability", "type": "radio", "required": True,
         "country_context": "united states", "choices": ["Yes", "No"]}]}
    packet_path = path.parent / "applications" / job["dedupe_hash"] / "packet.json"
    booklet.write_private(packet_path, packet)
    queue.finish(conn, job["dedupe_hash"], "waiting_input", packet_path.with_name("review.html"))
    question = questions.collect(job, packet, path)[0]
    questions.answer(question["id"], "Yes", path)  # simulate crash before queue persistence
    yield conn, path, job, packet, packet_path, question
    conn.close()


def state(conn):
    return conn.execute("SELECT state,attempts,lease_until FROM applications").fetchone()


def test_recovery_is_idempotent_and_only_resets_one_answered_handoff(context):
    conn, path, job, packet, packet_path, question = context
    original = path.read_bytes()
    assert answer_resume.recover(conn, path) == 1
    assert tuple(state(conn)) == ("queued", 0, None)
    assert answer_resume.recover(conn, path) == 0
    claim = queue.claim(conn)
    assert claim["attempts"] == 1
    assert answer_resume.recover(conn, path) == 0
    assert tuple(state(conn))[:2] == ("running", 1)
    assert path.read_bytes() == original  # recovery never rewrites candidate facts or the ledger


@pytest.mark.parametrize("protected", ["running", "waiting_review", "submission_uncertain", "submitted", "skipped", "waiting_login", "waiting_captcha"])
def test_protected_application_states_are_never_reset(context, protected):
    conn, path, *_ = context
    conn.execute("UPDATE applications SET state=?,attempts=2,lease_until=9999999999", (protected,)); conn.commit()
    before = tuple(state(conn))
    assert answer_resume.recover(conn, path) == 0
    assert tuple(state(conn)) == before


@pytest.mark.parametrize("change", ["country", "ref", "scope", "revision", "unverified", "question", "source_question", "source_revision", "wrong_job", "no_packet", "submitted_packet"])
def test_inexact_or_unverified_answer_proof_does_not_requeue(context, change):
    conn, path, job, packet, packet_path, question = context
    book = booklet.load(path)
    record = book["question_handoffs"][question["id"]]
    answer = book["custom_answers"][record["custom_answer_key"]]
    if change == "country": record["country_context"] = "canada"
    elif change == "ref": record["contexts"][job["dedupe_hash"]]["ref"] = "different"
    elif change == "scope": record["scope"]["board"] = "different"
    elif change == "revision": record["answer_revision"] = "different"
    elif change == "unverified": answer["status"] = "needs_input"
    elif change == "question": record["normalized_question"] = "different question?"
    elif change == "source_question": answer["source"]["question_id"] = "q_"+"f"*24
    elif change == "source_revision": answer["source"]["answered_at"] = "different"
    elif change == "wrong_job": packet["job"] = {**job, "dedupe_hash": "f"*64}
    elif change == "no_packet": packet_path.unlink()
    elif change == "submitted_packet": packet["submitted"] = True
    booklet.write_private(path, book)
    if change != "no_packet": booklet.write_private(packet_path, packet)
    assert answer_resume.recover(conn, path) == 0
    assert state(conn)[0] == "waiting_input"


def test_all_required_answers_must_have_current_scoped_proof(context):
    conn, path, job, packet, packet_path, question = context
    packet["missing"].append({"question": "Second required question?", "ref": "second", "required": True, "type": "radio"})
    booklet.write_private(packet_path, packet)
    second = questions.collect(job, packet, path)[0]
    assert answer_resume.recover(conn, path) == 0
    questions.answer(second["id"], "No", path)
    assert answer_resume.recover(conn, path) == 1


def test_saved_user_response_can_resume_with_remaining_worker_document_task(context):
    conn, path, job, packet, packet_path, question = context
    packet['missing'].append({'question': 'Cover Letter', 'ref': 'cover_letter',
                              'type': 'file', 'required': True})
    booklet.write_private(packet_path, packet)
    questions.collect(job, packet, path)
    assert answer_resume.recover(conn, path) == 1


def test_old_unanswered_ledger_question_does_not_block_current_saved_response(context):
    conn, path, job, packet, packet_path, question = context
    questions.collect(job, {"missing": [{"question": "Retired employer question", "ref": "old", "required": True}]}, path)
    assert answer_resume.recover(conn, path) == 1


def test_routing_worker_tasks_alone_never_triggers_answer_recovery(context):
    conn, path, job, packet, packet_path, question = context
    packet['missing'] = [{'question': 'Cover Letter', 'ref': 'cover_letter',
                          'type': 'file', 'required': True}]
    booklet.write_private(packet_path, packet)
    questions.collect(job, packet, path)
    assert answer_resume.recover(conn, path) == 0


@pytest.mark.parametrize("table,status", [("application_approvals", "approved"), ("application_approvals", "submitting"),
                                           ("authorized_submission_attempts", "in_progress"), ("authorized_submission_attempts", "uncertain"),
                                           ("authorized_submission_attempts", "submitted")])
def test_active_or_uncertain_final_authority_is_not_reset(context, table, status):
    conn, path, job, *_ = context
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(job_hash TEXT,state TEXT)")
    conn.execute(f"INSERT INTO {table}(job_hash,state) VALUES(?,?)", (job["dedupe_hash"], status)); conn.commit()
    assert answer_resume.recover(conn, path) == 0
    assert state(conn)[0] == "waiting_input"


@pytest.mark.parametrize("limit", [0, 201, True, -1, 1.5])
def test_recovery_bounds_are_explicit(context, limit):
    conn, path, *_ = context
    with pytest.raises(ValueError, match="limit"):
        answer_resume.recover(conn, path, limit=limit)


def test_explicit_candidate_exclusion_is_preserved_before_queue_resumption(context):
    conn, path, job, *_ = context
    book = booklet.load(path)
    book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "synthetic candidate decline"}}
    booklet.write_private(path, book)
    assert answer_resume.recover(conn, path) == 0
    assert state(conn)[0] == "waiting_input"
