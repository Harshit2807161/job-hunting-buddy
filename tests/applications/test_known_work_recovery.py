"""Known facts resume work, while the ledger remains an immutable history."""
import json

import pytest

from jhb import config, store
from jhb.applications import answer_resume, booklet, questions, queue


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "roles": {"sde": {}, "ml": {}}, "custom_answers": {},
        "answers": {"disclosure.gender": booklet.answer("Female", "synthetic candidate fact")}})
    conn = store.connect(tmp_path / "jobs.sqlite3")
    queue.enqueue(conn, [{"dedupe_hash": "a" * 64, "source": "synthetic", "company": "Example",
        "title": "Software Engineer", "url": "https://job-boards.greenhouse.io/example/jobs/123"}])
    job = queue.claim(conn)["job"]
    packet = {"state": "waiting_input", "job": job, "submitted": False, "filled": [], "missing": [
        {"question": "Gender", "ref": "gender", "required": True, "type": "combobox", "choices": ["Female", "Male"]},
        {"question": "Who is your personal reference?", "ref": "reference", "required": True, "type": "text"}]}
    packet_path = path.parent / "applications" / job["dedupe_hash"] / "packet.json"
    booklet.write_private(packet_path, packet)
    queue.finish(conn, job["dedupe_hash"], "waiting_input", packet_path.with_name("review.html"))
    questions.collect(job, packet, path)
    yield conn, path, job, packet, packet_path
    conn.close()


def test_known_work_resumes_without_user_reply_even_with_one_unknown(work):
    conn, path, job, *_ = work
    before = path.read_bytes()
    assert len(questions.pending(path, connection=conn)) == 1
    assert answer_resume.recover_agent_work(conn, path) == 1
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "queued"
    assert questions.pending(path, connection=conn) == []
    assert answer_resume.recover_agent_work(conn, path) == 0
    assert path.read_bytes() == before


def test_failed_widget_does_not_loop_on_new_packet_timestamp_or_unrelated_fact(work):
    conn, path, job, packet, packet_path = work
    assert answer_resume.recover_agent_work(conn, path) == 1
    packet["created_at"] = "2099-01-01T00:00:00Z"
    booklet.write_private(packet_path, packet)
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    book = booklet.load(path)
    book["answers"]["identity.first_name"] = booklet.answer("Another", "synthetic correction")
    booklet.write_private(path, book)
    assert answer_resume.recover_agent_work(conn, path) == 0
    book["answers"]["disclosure.gender"] = booklet.answer("Male", "synthetic explicit correction")
    booklet.write_private(path, book)
    assert answer_resume.recover_agent_work(conn, path) == 1


@pytest.mark.parametrize("state", ["waiting_review", "waiting_login", "waiting_captcha", "submitted", "skipped",
                                  "discarded", "submission_uncertain", "history_hold", "running"])
def test_terminal_or_noninput_work_is_neither_question_nor_recovery(work, state):
    conn, path, *_ = work
    conn.execute("UPDATE applications SET state=?", (state,)); conn.commit()
    assert questions.pending(path, connection=conn) == []
    assert answer_resume.recover_agent_work(conn, path) == 0


@pytest.mark.parametrize("change", ["wrong_ref", "filled", "old_packet", "other_job", "changed_help", "country", "choices", "excluded", "discarded"])
def test_stale_contexts_cannot_schedule_work(work, change):
    conn, path, job, packet, packet_path = work
    if change == "wrong_ref": packet["missing"][0]["ref"] = "new_gender"
    elif change == "filled": packet["filled"] = [{"ref": "gender"}]
    elif change == "old_packet": packet["state"] = "waiting_review"
    elif change == "other_job": packet["job"] = {**job, "dedupe_hash": "f" * 64}
    elif change == "changed_help": packet["missing"][0]["description"] = "A newly changed restriction"
    elif change == "country": packet["missing"][0]["country_context"] = "Canada"
    elif change == "choices": packet["missing"][0]["choices"] = []
    elif change == "excluded":
        book = booklet.load(path)
        book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "synthetic explicit exclusion"}}
        booklet.write_private(path, book)
    elif change == "discarded":
        booklet.write_private(config.ROOT / "private" / "application-discards" / (job["dedupe_hash"] + ".json"), {})
    booklet.write_private(packet_path, packet)
    assert answer_resume.recover_agent_work(conn, path) == 0


def test_document_generation_is_scheduled_once_without_fake_candidate_response(work):
    conn, path, job, packet, packet_path = work
    packet["missing"] = [{"question": "Cover Letter", "ref": "cover", "type": "file", "required": True}]
    booklet.write_private(packet_path, packet)
    questions.collect(job, packet, path)
    assert questions.pending(path, connection=conn) == []
    assert answer_resume.recover_agent_work(conn, path) == 1
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    assert answer_resume.recover_agent_work(conn, path) == 0
    assert not booklet.load(path)["custom_answers"]


def test_control_id_churn_cannot_rearm_the_same_known_work(work):
    conn, path, job, packet, packet_path = work
    assert answer_resume.recover_agent_work(conn, path) == 1
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    packet["missing"][0]["ref"] = "regenerated_gender_id"
    booklet.write_private(packet_path, packet)
    questions.collect(job, packet, path)
    assert answer_resume.recover_agent_work(conn, path) == 0


def test_validated_resolver_revision_allows_one_new_recovery(work, monkeypatch):
    conn, path, *_ = work
    assert answer_resume.recover_agent_work(conn, path) == 1
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    monkeypatch.setattr(answer_resume, "AGENT_WORK_VERSION", answer_resume.AGENT_WORK_VERSION + 1)
    assert answer_resume.recover_agent_work(conn, path) == 1
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    assert answer_resume.recover_agent_work(conn, path) == 0


@pytest.mark.parametrize("selected_role", ["sde", "ml"])
def test_stale_required_ledger_does_not_veto_current_complete_review(work, selected_role):
    from jhb.applications.overnight import _manifest
    conn, path, job, packet, packet_path = work
    book = booklet.load(path)
    job["role_classes"] = "swe"
    resume_path = f"/synthetic/{selected_role}/resume.pdf"
    book["roles"][selected_role]["documents.resume"] = booklet.answer(resume_path, "synthetic")
    packet.update(state="waiting_review", missing=[], filled=[{"key": "documents.resume", "value": resume_path}])
    packet["review_inventory"] = {"complete": True, "fields": [
        {"ref": "resume", "question": "Resume", "required": True, "status": "answered"}]}
    manifest = _manifest(job, packet, book)
    assert manifest["selected_role"] == selected_role
    assert manifest["documents"]["documents.resume"]["value"] == resume_path
    packet["missing"] = [{"question": "Unanswered", "required": True}]
    with pytest.raises(ValueError, match="unresolved required"):
        _manifest(job, packet, book)
