"""Explicit portal edits recover across stores without renewing old authority."""
import base64
import hashlib
import json
import sqlite3
import time
import struct
import zlib

import pytest

from jhb import config
from jhb.applications import answer_resume, approvals, boards, booklet, questions, queue
# Load immutable planner/skill paths before individual fixtures redirect ROOT.
from jhb.applications import worker  # noqa: F401

IMAGE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


@pytest.fixture
def edited(tmp_path, monkeypatch, request):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    key = boards.application_hash(url)
    job = {"dedupe_hash": key, "url": url, "company": "Example", "title": "Software Engineer", "role_classes": "swe"}
    book_path = tmp_path / "private" / "book.json"
    resume = tmp_path / "private" / "resume.pdf"
    resume.parent.mkdir(); resume.write_bytes(b"%PDF-1.4\nSynthetic approved resume"); resume.chmod(0o600)
    book = {"schema_version": 1, "answers": {"identity.full_name": booklet.answer("Synthetic Candidate", "synthetic source")},
            "roles": {"sde": {"documents.resume": booklet.answer(str(resume), "synthetic SDE source")}, "ml": {}}, "custom_answers": {}}
    booklet.write_private(book_path, book)
    packet = {"job": job, "selected_role": "sde", "state": "waiting_review", "submitted": False, "created_at": int(time.time()),
        "missing": [], "filled": [{"ref": "name", "question": "Full name", "key": "identity.full_name", "value": "Synthetic Candidate", "source": "synthetic source"},
            {"ref": "resume", "question": "Resume", "key": "documents.resume", "value": str(resume), "source": "synthetic SDE source"}],
        "optional_questions": [{"ref": "motivation", "question": "Why this employer?", "required": False, "type": "textarea"}],
        "review_inventory": {"complete": True, "fields": [
            {"ref": "name", "question": "Full name", "type": "text", "required": True, "status": "answered"},
            {"ref": "resume", "question": "Resume", "type": "file", "required": True, "status": "answered"},
            {"ref": "motivation", "question": "Why this employer?", "type": "textarea", "required": False, "status": "blank"}]}}
    image = IMAGE
    if getattr(request, "param", None) == "large":
        # A valid ancillary PNG text chunk models a long retained screenshot.
        data = b"Synthetic\0"+b"x"*(2*1024*1024)
        kind = b"tEXt"
        chunk = struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data) & 0xffffffff)
        image = IMAGE[:-12]+chunk+IMAGE[-12:]
    packet["capture"] = {"schema_version": 1, "verified": True, "capture_id": "a"*32, "filename": "browser.png",
        "job_hash": key, "packet_created_at": packet["created_at"], "captured_at": "synthetic fresh capture",
        "sha256": hashlib.sha256(image).hexdigest(), "target_id": "synthetic-owned-tab"}
    packet_path = tmp_path / "private" / "applications" / key / "packet.json"
    booklet.write_private(packet_path, packet)
    packet_path.with_name("browser.png").write_bytes(image)
    packet_path.with_name("browser.png").chmod(0o600)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; queue.initialize(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'waiting_review',?,?)",
                 (key, json.dumps(job), int(time.time()), str(packet_path.with_name("review.html")))); conn.commit()
    # This binding comes from the server's actual pre-edit validated draft.
    _, binding, revision = approvals._draft(conn, key, book_path)
    q = questions.collect(job, packet, book_path)[0]
    questions.answer(q["id"], "Synthetic candidate-written response", book_path, decline=getattr(request, "param", None) == "declined")
    book = booklet.load(book_path)
    record = book["question_handoffs"][q["id"]]
    intent = {"provider": "local_dashboard_explicit_edit", "question_id": q["id"], "answer_revision": record["answer_revision"],
              "job_hash": key, "packet_path": str(packet_path), "packet_sha256": binding["packet_sha256"],
              "review_revision": revision, "review_binding": binding, "approval_revoked": True}
    record["candidate_edit_intents"] = {key: intent}
    booklet.write_private(book_path, book)
    yield conn, key, book_path, packet_path, q["id"]
    conn.close()


def state(conn):
    return conn.execute("SELECT state FROM applications").fetchone()[0]


def test_explicit_edit_recovers_once_with_old_bound_draft_and_retained_response(edited):
    conn, key, book, packet, _ = edited
    originals = {path: path.read_bytes() for path in [book, packet, packet.with_name("browser.png")]}
    assert answer_resume.recover(conn, book) == 1
    assert state(conn) == "queued"
    assert answer_resume.recover(conn, book) == 0
    assert all(path.read_bytes() == value for path, value in originals.items())
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_approvals'").fetchone()


def test_preparation_preservation_allows_only_verified_explicit_portal_edit(edited):
    from jhb.applications.retained_preparation import retained_review
    conn, key, book_path, packet_path, _ = edited
    assert answer_resume.recover(conn, book_path) == 1
    (config.ROOT / "data").mkdir()
    with sqlite3.connect(config.ROOT / "data" / "jobs.sqlite3") as disk:
        conn.backup(disk)
    job = json.loads(packet_path.read_text())["job"]
    book = booklet.load(book_path)
    assert retained_review(job, book, book_path) is None
    assert retained_review(job, book) is not None  # Generic retry has no bound edit context.


@pytest.mark.parametrize("change", ["none", "revision", "question_id", "job_hash", "provider", "revoked", "binding", "packet_sha", "packet_path", "book_path", "context_ref", "resolved", "screenshot", "packet", "legacy_capture"])
def test_generic_or_stale_edit_evidence_preserves_waiting_review(edited, change):
    conn, key, book_path, packet_path, qid = edited
    book = booklet.load(book_path)
    record = book["question_handoffs"][qid]
    intent = record["candidate_edit_intents"][key]
    if change == "none": record.pop("candidate_edit_intents")
    elif change == "revision": intent["answer_revision"] = "stale"
    elif change == "question_id": intent["question_id"] = "q_"+"f"*24
    elif change == "job_hash": intent["job_hash"] = "f"*64
    elif change == "provider": intent["provider"] = "assistant proposed edit"
    elif change == "revoked": intent["approval_revoked"] = False
    elif change == "binding": intent["review_revision"] = "f"*64
    elif change == "packet_sha": intent["packet_sha256"] = "f"*64
    elif change == "packet_path": intent["packet_path"] = str(book_path)
    elif change == "book_path": intent["review_binding"]["book_path"] = str(packet_path)
    elif change == "context_ref": record["contexts"][key]["ref"] = "different"
    elif change == "resolved": record["contexts"][key]["resolved"] = True
    elif change == "screenshot": packet_path.with_name("browser.png").write_bytes(IMAGE+b"changed")
    else:
        packet = json.loads(packet_path.read_bytes())
        if change == "legacy_capture": packet.pop("capture")
        else: packet["selected_role"] = "ml"
        booklet.write_private(packet_path, packet)
    booklet.write_private(book_path, book)
    assert answer_resume.recover(conn, book_path) == 0
    assert state(conn) == "waiting_review"


@pytest.mark.parametrize("approval", ["approved", "submitting", "revoked"])
def test_only_revoked_or_absent_approval_allows_explicit_edit_recovery(edited, approval):
    conn, key, book, *_ = edited
    approvals.initialize(conn)
    conn.execute("INSERT INTO application_approvals VALUES('synthetic',?,?,0,9999999999,'revision','private/path',NULL)", (key, approval)); conn.commit()
    assert answer_resume.recover(conn, book) == (1 if approval == "revoked" else 0)
    assert state(conn) == ("queued" if approval == "revoked" else "waiting_review")
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == approval


@pytest.mark.parametrize("previous_state", ["waiting_input", "waiting_review"])
@pytest.mark.parametrize("click", [False, True, None, "missing", "mismatched_identity"])
def test_old_final_attempt_requires_exact_durable_no_click_proof(edited, click, previous_state):
    conn, key, book, packet, qid = edited
    if previous_state == "waiting_input":
        original = json.loads(packet.read_bytes())
        original.update(state="waiting_input", missing=[{"ref": "motivation", "question": "Why this employer?",
                                                         "required": True, "type": "textarea"}])
        booklet.write_private(packet, original)
        source = booklet.load(book)
        source["question_handoffs"][qid]["contexts"][key]["required"] = True
        booklet.write_private(book, source)
        conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    conn.execute("CREATE TABLE authorized_submission_attempts(job_hash TEXT,state TEXT,attempt_path TEXT,authorization_id TEXT)")
    attempt_path = packet.parent / "attempt.json"
    proof = {"runtime_click_started": click, "job_hash": key, "authorization_id": "synthetic-authority",
             "application_url": "https://job-boards.greenhouse.io/example/jobs/123"}
    if click == "mismatched_identity": proof.update(runtime_click_started=False, application_url="https://job-boards.greenhouse.io/example/jobs/456")
    if click != "missing": booklet.write_private(attempt_path, proof)
    conn.execute("INSERT INTO authorized_submission_attempts VALUES(?,'waiting_review',?,'synthetic-authority')", (key, str(attempt_path))); conn.commit()
    assert answer_resume.recover(conn, book) == (1 if click is False else 0)
    assert state(conn) == ("queued" if click is False else previous_state)


@pytest.mark.parametrize("edited", ["large"], indirect=True)
def test_valid_long_screenshot_uses_capture_size_bound(edited):
    conn, _, book, packet, _ = edited
    assert packet.with_name("browser.png").stat().st_size > 2_000_000
    assert answer_resume.recover(conn, book) == 1
    assert state(conn) == "queued"


def test_malformed_required_inventory_does_not_requeue(edited):
    conn, key, book, packet, qid = edited
    original = json.loads(packet.read_bytes())
    original.update(state="waiting_input", missing=[{"ref": "motivation", "question": "Why this employer?", "required": True}, "corrupt question"])
    booklet.write_private(packet, original)
    source = booklet.load(book)
    source["question_handoffs"][qid]["contexts"][key]["required"] = True
    booklet.write_private(book, source)
    conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
    assert answer_resume.recover(conn, book) == 0
    assert state(conn) == "waiting_input"


@pytest.mark.parametrize("edited", ["declined"], indirect=True)
def test_explicit_optional_blank_choice_recovers_but_never_answers_required_gap(edited):
    conn, key, book, packet, qid = edited
    saved = booklet.load(book)
    record = saved["question_handoffs"][qid]
    assert saved["custom_answers"][record["custom_answer_key"]]["status"] == "declined"
    record["contexts"][key]["required"] = True
    booklet.write_private(book, saved)
    assert answer_resume.recover(conn, book) == 0
    record["contexts"][key]["required"] = False
    booklet.write_private(book, saved)
    assert answer_resume.recover(conn, book) == 1
    assert state(conn) == "queued"
