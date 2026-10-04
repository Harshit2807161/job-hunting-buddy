"""Synthetic notification clock/SQLite/SMTP; no real mail or account access."""
import json
import sqlite3

import pytest

from jhb.applications import booklet, notices, questions, queue, worker


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("jhb.config.ROOT", tmp_path)
    clock = [1000]
    monkeypatch.setattr(notices.time, "time", lambda: clock[0])
    db = sqlite3.connect(tmp_path / "notices.sqlite3")
    db.row_factory = sqlite3.Row
    queue.initialize(db)
    yield db, clock, tmp_path
    db.close()


def add(db, number, state):
    job = {"dedupe_hash": f"{number:064x}", "title": "Synthetic role", "company": "Example", "source": "fixture",
           "url": f"https://job-boards.greenhouse.io/example/jobs/{number}"}
    db.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,0)",
               (job["dedupe_hash"], json.dumps(job), state))
    db.commit()
    return job


def test_operational_statuses_remain_local_while_reviews_coalesce(setup, monkeypatch):
    db, clock, root = setup
    for number, state in enumerate(["failed", "skipped", "unsupported", "waiting_login", "waiting_captcha", "waiting_input"], 1):
        add(db, number, state)
    reviews = [add(db, number, "waiting_review") for number in (10, 11, 12)]
    sent = []
    monkeypatch.setattr("jhb.notify.send", lambda jobs, **kwargs: sent.append((jobs, kwargs)) or True)
    worker.notify_pending(db, send_email=True)
    assert len(sent) == 1 and len(sent[0][0]) == 3
    assert sent[0][1]["subject_prefix"] == "[applications ready for review] "
    assert len(list((root / "private" / "notifications").glob("*.json"))) == 9
    # A worker rewriting the same status clears legacy notified_at, but not the
    # durable identity of the successfully delivered review notice.
    for job in reviews:
        queue.finish(db, job["dedupe_hash"], "waiting_review")
    worker.notify_pending(db, send_email=True)
    assert len(sent) == 1
    add(db, 13, "waiting_review")
    add(db, 14, "waiting_review")
    worker.notify_pending(db, send_email=True)
    assert len(sent) == 1
    clock[0] += notices.COALESCE_SECONDS
    worker.notify_pending(db, send_email=True)
    assert len(sent) == 2 and len(sent[1][0]) == 2


def test_failed_delivery_backs_off_new_keys_and_caps_delay(setup):
    db, clock, root = setup
    calls = []
    def fail(keys):
        calls.append(keys)
        raise RuntimeError("Synthetic private error text must never be persisted")
    assert notices.deliver(db, {"event:1"}, "reviews", fail) == set()
    clock[0] += 60
    assert notices.deliver(db, {"event:1", "event:2"}, "reviews", fail) == set()
    assert len(calls) == 1
    for attempt in range(1, 10):
        row = db.execute("SELECT * FROM application_notice_channels").fetchone()
        assert row["next_attempt_at"] - clock[0] <= notices.RETRY_MAX_SECONDS
        clock[0] = row["next_attempt_at"]
        notices.deliver(db, {"event:1", "event:2"}, "reviews", fail)
    row = db.execute("SELECT * FROM application_notice_channels").fetchone()
    assert row["next_attempt_at"] - clock[0] == notices.RETRY_MAX_SECONDS
    assert row["last_error"] == "RuntimeError"
    clock[0] = row["next_attempt_at"]
    assert notices.deliver(db, {"event:1", "event:2"}, "reviews", lambda keys: True) == {"event:1", "event:2"}
    assert db.execute("SELECT attempts FROM application_notice_channels").fetchone()[0] == 0


def test_upgrade_retains_previous_review_delivery_after_worker_rewrites(setup, monkeypatch):
    db, clock, root = setup
    job = add(db, 1, "waiting_review")
    db.execute("UPDATE applications SET notified_at=500"); db.commit()
    sends = []
    monkeypatch.setattr("jhb.notify.send", lambda *args, **kwargs: sends.append(kwargs) or True)
    worker.notify_pending(db, send_email=False)
    queue.finish(db, job["dedupe_hash"], "waiting_review")
    worker.notify_pending(db, send_email=True)
    assert not sends
    assert db.execute("SELECT notified_at FROM applications").fetchone()[0] == clock[0]


def test_concurrent_sender_and_restart_cannot_repeat_delivered_events(setup):
    db, clock, root = setup
    second = sqlite3.connect(root / "notices.sqlite3")
    duplicate = []
    def send(keys):
        assert notices.deliver(second, keys, "reviews", lambda keys: duplicate.append(keys) or True) == set()
        return True
    assert notices.deliver(db, {"event:1"}, "reviews", send) == {"event:1"}
    assert notices.deliver(second, {"event:1"}, "reviews", lambda keys: duplicate.append(keys) or True) == {"event:1"}
    assert not duplicate
    second.close()


def test_interrupted_send_persists_retry_gate_before_callback(setup):
    db, clock, root = setup
    def interrupted(keys):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        notices.deliver(db, {"event:1"}, "reviews", interrupted)
    attempted = []
    clock[0] += 60
    assert notices.deliver(db, {"event:1"}, "reviews", lambda keys: attempted.append(keys) or True) == set()
    assert not attempted
    clock[0] += notices.RETRY_BASE_SECONDS
    assert notices.deliver(db, {"event:1"}, "reviews", lambda keys: attempted.append(keys) or True) == {"event:1"}
    assert len(attempted) == 1


def test_optional_questions_stay_local_and_required_reopen_is_new_event(setup, monkeypatch):
    db, clock, root = setup
    job = add(db, 1, "waiting_input")
    path = root / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    required = {"question": "Choose your office", "ref": "office", "required": True}
    questions.collect(job, {"optional_questions": [{"question": "Preferred name", "required": False}]}, path)
    sends = []
    monkeypatch.setattr("jhb.notify.send", lambda jobs, **kwargs: sends.append(kwargs) or True)
    assert questions.notify_new(db, path, send_email=True) == 0 and not sends
    record = questions.collect(job, {"missing": [required]}, path)[0]
    assert questions.notify_new(db, path, send_email=True) == 1
    questions.answer(record["id"], "Unknown office", path)
    current = booklet.load(path)
    key = current["question_handoffs"][record["id"]]["custom_answer_key"]
    questions.collect(job, {"missing": [{**required, "answer_key": key, "reason": "Selected office is unavailable"}]}, path, observed_book=current)
    assert questions.notify_new(db, path, send_email=True) == 0  # Coalesce correction.
    clock[0] += notices.COALESCE_SECONDS
    assert questions.notify_new(db, path, send_email=True) == 1
    assert len(sends) == 2
    assert all("Preferred name" not in item["details"] for item in sends)


def test_question_reopened_during_smtp_is_not_marked_as_delivered_new_revision(setup, monkeypatch):
    db, clock, root = setup
    job = add(db, 1, "waiting_input")
    path = root / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    field = {"question": "Choose your office", "ref": "office", "required": True}
    record = questions.collect(job, {"missing": [field]}, path)[0]
    sends = []
    def send(*args, **kwargs):
        sends.append(kwargs)
        if len(sends) == 1:
            questions.answer(record["id"], "Unavailable office", path)
            fresh = booklet.load(path)
            key = fresh["question_handoffs"][record["id"]]["custom_answer_key"]
            questions.collect(job, {"missing": [{**field, "answer_key": key, "reason": "Office unavailable"}]}, path, observed_book=fresh)
        return True
    monkeypatch.setattr("jhb.notify.send", send)
    assert questions.notify_new(db, path, send_email=True) == 1
    assert [item["id"] for item in questions.pending(path, unnotified=True)] == [record["id"]]
    clock[0] += notices.COALESCE_SECONDS
    assert questions.notify_new(db, path, send_email=True) == 1
    assert len(sends) == 2
