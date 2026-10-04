"""Read-only review reproductions use synthetic private state only."""
import sqlite3

import pytest

from jhb import config, store
from jhb.applications import booklet, pipeline, questions, queue


def test_saved_answer_before_failed_sqlite_resume_is_recovered_by_next_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    conn = store.connect(tmp_path / "jobs.sqlite3")
    candidate = {"dedupe_hash": "1" * 64, "source": "synthetic", "company": "Example", "title": "Software Engineer",
                 "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    queue.enqueue(conn, [candidate])
    item = queue.claim(conn)
    candidate = item["job"]
    missing = {"state": "waiting_input", "filled": [], "missing": [{"question": "Synthetic required availability choice?",
                "ref": "availability", "type": "radio", "required": True, "choices": ["Yes", "No"]}]}
    packet_path = path.parent / "applications" / item["job_hash"] / "packet.json"
    booklet.write_private(packet_path, {**missing, "job": candidate, "submitted": False})
    queue.finish(conn, item["job_hash"], "waiting_input", packet_path.with_name("review.html"))
    question = questions.collect(candidate, missing, path)[0]

    class FailedResumeConnection:
        def execute(self, *args):
            raise sqlite3.OperationalError("synthetic process interruption after booklet commit")
        def commit(self):
            pytest.fail("Interrupted database must not commit")

    with pytest.raises(sqlite3.OperationalError):
        questions.answer(question["id"], "Yes", path, FailedResumeConnection())
    assert booklet.load(path)["question_handoffs"][question["id"]]["status"] == "answered"
    assert questions.pending(path) == []
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"

    calls = []
    async def runner(actual, fresh_book, **kwargs):
        assert actual["dedupe_hash"] == item["job_hash"]
        assert fresh_book["question_handoffs"][question["id"]]["status"] == "answered"
        calls.append(actual["dedupe_hash"])
        return {"state": "waiting_review", "missing": [], "filled": []}, packet_path.with_name("review.html")
    summary = pipeline.run_cycle(conn, path, resolver=runner, runner=runner,
                                 source_limit=1, application_limit=1)
    assert summary["auto_requeued"] == 1 and summary["applications_prepared"] == 1
    assert calls == [item["job_hash"]]
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"
    summary = pipeline.run_cycle(conn, path, resolver=runner, runner=runner,
                                 source_limit=1, application_limit=1)
    assert summary["auto_requeued"] == 0 and summary["applications_prepared"] == 0
    assert calls == [item["job_hash"]]
    conn.close()
