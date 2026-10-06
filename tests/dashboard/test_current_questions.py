"""Candidate questions are current packet work; archived ledger data survives."""
import copy
import json

import pytest

from jhb.applications import approvals, booklet, questions, historical
from jhb.applications.question_lifecycle import current_context
from test_dashboard import portal, add_job, pending_question, current_question_packet, reviewable


@pytest.fixture(autouse=True)
def initialized_approval_schema(portal):
    approvals.initialize(portal[1])


@pytest.mark.parametrize("state", ["queued", "running", "retry", "failed", "waiting_login", "waiting_captcha",
    "waiting_review", "submitted", "submission_uncertain", "skipped", "discarded", "history_hold"])
def test_only_current_waiting_input_is_in_count_cards_and_details(portal, state):
    root, conn, book, client, headers = portal
    live, _, _ = add_job(conn, root, n=1, state="waiting_input")
    inactive, _, _ = add_job(conn, root, n=2, state="waiting_input")
    shared = pending_question(live, book)
    pending_question(inactive, book)
    conn.execute("UPDATE applications SET state=? WHERE job_hash=?", (state, inactive["dedupe_hash"]))
    conn.commit()
    before = book.read_bytes(), list(conn.iterdump())
    overview = client.get("/api/v1/overview").json()
    assert overview["summary"]["questions"] == len(overview["questions"]) == 1
    assert [c["job_hash"] for c in overview["questions"][0]["contexts"]] == [live["dedupe_hash"]]
    assert client.get(f"/api/v1/applications/{live['dedupe_hash']}").json()["questions"][0]["id"] == shared["id"]
    assert client.get(f"/api/v1/applications/{inactive['dedupe_hash']}").json()["questions"] == []
    assert (book.read_bytes(), list(conn.iterdump())) == before


@pytest.mark.parametrize("fault", ["orphan", "wrong_context_job", "wrong_context_url", "wrong_packet_job", "old_packet_state",
    "missing_packet", "authoritative_packet_missing", "absent_ref", "changed_label", "changed_description", "changed_type",
    "changed_required", "changed_choices", "already_filled", "inventory_answered", "declined_optional", "excluded", "discard_marker"])
def test_stale_context_never_prompts_or_accepts_an_answer(portal, fault):
    root, conn, book, client, headers = portal
    job, folder, _ = add_job(conn, root, state="waiting_input")
    question = pending_question(job, book, required=fault != "declined_optional")
    data = booklet.load(book);context = data["question_handoffs"][question["id"]]["contexts"][job["dedupe_hash"]]
    packet = json.loads((folder / "packet.json").read_text());field = packet["missing"][0]
    if fault == "orphan": conn.execute("DELETE FROM applications")
    elif fault == "wrong_context_job": context["job_hash"] = "f"*64
    elif fault == "wrong_context_url": context["url"] = "https://job-boards.greenhouse.io/example/jobs/999"
    elif fault == "wrong_packet_job": packet["job"]["dedupe_hash"] = "f"*64
    elif fault == "old_packet_state": packet["state"] = "waiting_login"
    elif fault == "missing_packet": (folder / "packet.json").unlink()
    elif fault == "authoritative_packet_missing":
        conn.execute("UPDATE applications SET packet=?", (str(root / "private/new-attempt/review.html"),))
    elif fault == "absent_ref": packet["missing"] = []
    elif fault == "changed_label": field["question"] = "A different question"
    elif fault == "changed_description": field["description"] = "A new legally relevant condition"
    elif fault == "changed_type": field["type"] = "radio"
    elif fault == "changed_required": field["required"] = False
    elif fault == "changed_choices": field["choices"], context["choices"] = ["Current"], ["Historical"]
    elif fault == "already_filled": packet["filled"].append({"ref": field["ref"], "value": "Already retained"})
    elif fault == "inventory_answered":
        packet["review_inventory"] = {"fields": [{**field, "status": "answered"}]}
    elif fault == "declined_optional": packet["resolved_optional_refs"] = [field["ref"]]
    elif fault == "excluded": data["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "Synthetic explicit exclusion"}}
    elif fault == "discard_marker":
        booklet.write_private(root / "private/application-discards" / (job["dedupe_hash"]+".json"), {
            "job_hash": job["dedupe_hash"], "state": "discarded", "tab_close": {"state": "pending"},
            "worker_stop": {"state": "pending"}})
    if fault != "missing_packet": booklet.write_private(folder / "packet.json", packet)
    booklet.write_private(book, data);conn.commit()
    before = book.read_bytes(), list(conn.iterdump())
    overview = client.get("/api/v1/overview").json()
    assert overview["summary"]["questions"] == 0 and overview["questions"] == []
    if fault != "orphan": assert client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()["questions"] == []
    response = client.post(f"/api/v1/questions/{question['id']}/answer", headers=headers,
                          json={"value": "Unexpected replacement", "revision": question["updated_at"]})
    assert response.status_code == 409
    assert (book.read_bytes(), list(conn.iterdump())) == before


def test_durable_history_hold_hides_context_even_before_queue_state_reconciles(portal, monkeypatch):
    root, conn, book, client, _ = portal
    job, _, _ = add_job(conn, root, state="waiting_input")
    pending_question(job, book)
    calls = []
    def matched(connection, candidate, **kwargs):
        calls.append(candidate["dedupe_hash"])
        return {"disposition": "hold", "submission_confirmed": False, "match_kind": "audited_history_hold"}
    monkeypatch.setattr(historical, "match", matched)
    before = book.read_bytes(), list(conn.iterdump())
    assert client.get("/api/v1/overview").json()["questions"] == []
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()["questions"] == []
    assert job["dedupe_hash"] in calls
    assert (book.read_bytes(), list(conn.iterdump())) == before


def test_current_optional_blank_is_input_but_ready_review_blank_is_not(portal):
    root, conn, book, client, _ = portal
    job, folder, _ = add_job(conn, root, state="waiting_input")
    item = {"ref": "context", "question": "Any factual context to add?", "type": "textarea", "required": False}
    packet = json.loads((folder / "packet.json").read_text());packet["optional_questions"] = [item]
    booklet.write_private(folder / "packet.json", packet)
    q = questions.collect(job, packet, book)[0]
    assert client.get("/api/v1/overview").json()["questions"][0]["id"] == q["id"]
    conn.execute("UPDATE applications SET state='waiting_review'");conn.commit()
    packet["state"] = "waiting_review";booklet.write_private(folder / "packet.json", packet)
    assert client.get("/api/v1/overview").json()["questions"] == []


def test_old_known_answer_handoffs_do_not_block_current_review_readiness(portal):
    root, conn, book, client, _ = portal
    job, _, _ = reviewable(portal)
    data = booklet.load(book);data["answers"]["disclosure.gender"] = booklet.answer("Female", "Synthetic explicit answer")
    booklet.write_private(book, data)
    questions.collect(job, {"missing": [{"ref": "old_gender", "question": "Gender", "type": "combobox", "choices": []}]}, book)
    before = book.read_bytes()
    overview = client.get("/api/v1/overview").json();detail = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert overview["summary"]["ready"] == 1 and overview["summary"]["questions"] == 0
    assert overview["applications"][0]["pending_agent_tasks"] == 0 and detail["agent_tasks"] == []
    assert book.read_bytes() == before


def test_pure_lifecycle_helper_requires_current_identity_and_explicit_blocking(portal):
    root, conn, book, _, _ = portal
    job, folder, _ = add_job(conn, root, state="waiting_input")
    q = pending_question(job, book);data = booklet.load(book);context = q["contexts"][job["dedupe_hash"]]
    row = conn.execute("SELECT * FROM applications").fetchone();packet = json.loads((folder / "packet.json").read_text())
    before = copy.deepcopy((data, q, context, packet))
    assert current_context(data, q, context, row, packet)
    assert not current_context(data, q, context, row, packet, blocked=True)
    assert (data, q, context, packet) == before
