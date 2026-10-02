"""Answer revisions win over worker observations made from older snapshots."""
import json
import sqlite3
import asyncio

from jhb.applications import booklet, pipeline, questions, queue
from jhb.applications.planner import deterministic_plan
from jhb.applications.worker import prepare


def make_book(tmp_path):
    path = tmp_path / "private" / "booklet.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {},
                                "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    return path


def job(number=1):
    return {"dedupe_hash": f"{number:064x}", "url": f"https://job-boards.greenhouse.io/example/jobs/{number}",
            "title": "Synthetic engineer", "company": "Example employer"}


def missing(key=None, *, required=True, incompatible=False):
    item = {"question": "Explicit employer-specific question", "ref": "question_1",
            "answer_key": key, "required": required, "type": "combobox", "choices": ["Yes", "No"]}
    if incompatible:
        item["reason"] = "Stored answer unavailable or incompatible with field"
    return {"state": "waiting_input", "missing" if required else "optional_questions": [item]}


def key_for(path, qid):
    return booklet.load(path)["question_handoffs"][qid]["custom_answer_key"]


def test_old_incompatible_value_does_not_invalidate_new_user_correction(tmp_path):
    path = make_book(tmp_path)
    qid = questions.collect(job(), missing(), path)[0]["id"]
    questions.answer(qid, True, path)
    old_snapshot = booklet.load(path)
    custom_key = key_for(path, qid)
    questions.answer(qid, False, path)
    assert questions.collect(job(), missing(custom_key, incompatible=True), path,
                             observed_book=old_snapshot) == []
    current = booklet.load(path)
    assert current["question_handoffs"][qid]["status"] == "answered"
    assert current["custom_answers"][custom_key]["status"] == "verified"
    assert current["custom_answers"][custom_key]["value"] is False


def test_incompatible_current_snapshot_reopens_for_a_new_explicit_answer(tmp_path):
    path = make_book(tmp_path)
    qid = questions.collect(job(), missing(), path)[0]["id"]
    questions.answer(qid, True, path)
    questions.mark_notified([qid], path)
    fresh = booklet.load(path)
    custom_key = key_for(path, qid)
    pending = questions.collect(job(), missing(custom_key, incompatible=True), path, observed_book=fresh)
    assert [q["id"] for q in pending] == [qid]
    current = booklet.load(path)
    assert current["custom_answers"][custom_key]["status"] == "needs_input"
    assert "notified_at" not in current["question_handoffs"][qid]


def test_optional_decline_becoming_required_reopens_only_on_current_snapshot(tmp_path):
    path = make_book(tmp_path)
    qid = questions.collect(job(), missing(required=False), path)[0]["id"]
    before_response = booklet.load(path)
    questions.answer(qid, None, path, decline=True)
    custom_key = key_for(path, qid)
    assert questions.collect(job(), missing(custom_key), path, observed_book=before_response) == []
    assert booklet.load(path)["question_handoffs"][qid]["status"] == "answered"
    fresh = booklet.load(path)
    assert questions.collect(job(), missing(custom_key), path, observed_book=fresh)[0]["id"] == qid
    assert booklet.load(path)["custom_answers"][custom_key]["status"] == "needs_input"


def test_new_job_context_is_added_even_when_its_snapshot_predates_response(tmp_path):
    path = make_book(tmp_path)
    qid = questions.collect(job(), missing(), path)[0]["id"]
    before_response = booklet.load(path)
    questions.answer(qid, False, path)
    result = missing()
    assert questions.collect(job(2), result, path, observed_book=before_response) == []
    record = booklet.load(path)["question_handoffs"][qid]
    assert record["contexts"][job(2)["dedupe_hash"]]["required"]
    assert record["status"] == "answered"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,'waiting_input',0)",
                 (job(2)["dedupe_hash"], json.dumps(job(2))))
    conn.commit()
    assert pipeline._requeue_answered_handoff(conn, job(2), result, path)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "queued"


def test_context_update_is_not_a_user_answer_revision(tmp_path):
    path = make_book(tmp_path)
    qid = questions.collect(job(), missing(), path)[0]["id"]
    questions.answer(qid, True, path)
    fresh_answer_snapshot = booklet.load(path)
    custom_key = key_for(path, qid)
    # Another completed run only adds context; the answer remains identical.
    questions.collect(job(2), missing(), path, observed_book=fresh_answer_snapshot)
    reopened = questions.collect(job(), missing(custom_key, incompatible=True), path,
                                 observed_book=fresh_answer_snapshot)
    assert [q["id"] for q in reopened] == [qid]


def test_relabelled_same_ref_field_requires_new_question_handoff():
    class DynamicLabelForm:
        blocked_requests = 0
        changed = False
        def allowed_url(self, url):
            return True
        async def open(self, url):
            pass
        async def observe(self):
            return {"fields": [{"ref": "reused-ref", "label": "Employer-specific certification" if self.changed else "First Name",
                                "type": "text", "required": True}],
                    "buttons": [{"ref": "submit", "label": "Submit application"}]}
        async def fill(self, field, value):
            self.changed = True
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"},
                                   {"identity.first_name": booklet.answer("Sam", "synthetic approved profile")},
                                   deterministic_plan, None, cli_actions=DynamicLabelForm()))
    assert result["state"] == "waiting_input"
    assert result["missing"][0]["question"] == "Employer-specific certification"
