"""Local outbox writes must preserve a later email delivery and failure retry."""
import json
import sqlite3

import pytest

from jhb.applications import boards, booklet, notices, pipeline, questions, queue, source_queue, worker


def job():
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    return {"dedupe_hash": boards.application_hash(url), "url": url,
            "title": "Synthetic engineer", "company": "Example employer", "source": "synthetic"}


@pytest.mark.parametrize("kind", ["application", "source"])
def test_local_notice_keeps_email_pending_until_success(monkeypatch, tmp_path, kind):
    monkeypatch.setattr("jhb.config.ROOT", tmp_path)
    clock = [1000]
    monkeypatch.setattr(notices.time, "time", lambda: clock[0])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = job()
    if kind == "application":
        queue.initialize(conn)
        conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,'waiting_review',0)",
                     (row["dedupe_hash"], json.dumps(row)))
        conn.commit()
        deliver = worker.notify_pending
        table = "applications"
    else:
        source_queue.enqueue(conn, [row])
        source_queue.finish(conn, row["dedupe_hash"], "waiting_captcha", board="unknown")
        deliver = pipeline.notify_source_handoffs
        table = "application_sources"
    sends = []
    succeed = False
    def send(*args, **kwargs):
        sends.append(kwargs)
        return succeed
    monkeypatch.setattr("jhb.notify.send", send)
    deliver(conn, send_email=False)
    assert sends == []
    assert conn.execute(f"SELECT notified_at FROM {table}").fetchone()[0] is None
    deliver(conn, send_email=True)
    if kind == "source":
        assert sends == []  # Source auth/challenges coalesce for an hour.
        clock[0] += 3600
        deliver(conn, send_email=True)
    assert len(sends) == 1
    assert conn.execute(f"SELECT notified_at FROM {table}").fetchone()[0] is None
    succeed = True
    deliver(conn, send_email=True)
    assert len(sends) == 1  # SMTP retry cannot happen every cron minute.
    clock[0] += notices.RETRY_BASE_SECONDS
    deliver(conn, send_email=True)
    assert len(sends) == 2
    assert conn.execute(f"SELECT notified_at FROM {table}").fetchone()[0] is not None
    deliver(conn, send_email=True)
    assert len(sends) == 2
    local = list((tmp_path / "private" / "notifications").glob("*.json"))
    assert len(local) == 1
    assert local[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(local[0].read_text())["submitted"] is False


def test_question_local_notice_keeps_email_pending_until_success(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.config.ROOT", tmp_path)
    clock = [1000]
    monkeypatch.setattr(notices.time, "time", lambda: clock[0])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    row = job()
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,'waiting_input',0)",
                 (row["dedupe_hash"], json.dumps(row)))
    conn.commit()
    path = tmp_path / "private" / "booklet.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}},
                                "custom_answers": {}})
    result = {"missing": [{"question": "Explicit employer-specific certification", "ref": "new_question",
                           "required": True}]}
    packet_path = tmp_path / "private" / "applications" / row["dedupe_hash"] / "packet.json"
    booklet.write_private(packet_path, {"job": row, "state": "waiting_input", "submitted": False,
                                        "filled": [], **result})
    queue.finish(conn, row["dedupe_hash"], "waiting_input", packet_path.with_name("review.html"))
    questions.collect(row, result, path)
    sends = []
    succeed = False
    def send(*args, **kwargs):
        sends.append(kwargs)
        return succeed
    monkeypatch.setattr("jhb.notify.send", send)
    assert questions.notify_new(conn, path, send_email=False) == 0
    assert sends == []
    assert len(questions.pending(path, unnotified=True)) == 1
    assert questions.notify_new(conn, path, send_email=True) == 0
    assert len(sends) == 1
    assert len(questions.pending(path, unnotified=True)) == 1
    succeed = True
    assert questions.notify_new(conn, path, send_email=True) == 0
    assert len(sends) == 1
    clock[0] += notices.RETRY_BASE_SECONDS
    assert questions.notify_new(conn, path, send_email=True) == 1
    assert len(sends) == 2
    assert questions.pending(path, unnotified=True) == []
    assert questions.notify_new(conn, path, send_email=True) == 0
    assert len(sends) == 2


@pytest.mark.parametrize("stale", ["missing_packet", "wrong_job", "filled", "changed_ref", "submitted"])
def test_historical_question_does_not_send_without_current_unresolved_input(monkeypatch, tmp_path, stale):
    monkeypatch.setattr("jhb.config.ROOT", tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    row = job()
    path = tmp_path / "private" / "booklet.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}},
                                "custom_answers": {}})
    field = {"question": "Exact employer-specific question", "ref": "question", "required": True}
    result = {"missing": [field]}
    questions.collect(row, result, path)
    packet_path = tmp_path / "private" / "applications" / row["dedupe_hash"] / "packet.json"
    packet = {"job": row, "state": "waiting_input", "submitted": False, "filled": [], **result}
    if stale == "wrong_job":
        packet["job"] = {**row, "dedupe_hash": "f" * 64}
    elif stale == "filled":
        packet["filled"] = [{"ref": field["ref"]}]
    elif stale == "changed_ref":
        packet["missing"] = [{**field, "ref": "new_question"}]
    if stale != "missing_packet":
        booklet.write_private(packet_path, packet)
    state = "submitted" if stale == "submitted" else "waiting_input"
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,?,0,?)",
                 (row["dedupe_hash"], json.dumps(row), state, str(packet_path.with_name("review.html"))))
    conn.commit()
    monkeypatch.setattr("jhb.notify.send", lambda *args, **kwargs: pytest.fail("Stale question emailed candidate"))
    assert questions.notify_new(conn, path, send_email=True) == 0
    assert len(questions.pending(path, unnotified=True)) == 1  # Retain history without an active email.
    conn.close()
