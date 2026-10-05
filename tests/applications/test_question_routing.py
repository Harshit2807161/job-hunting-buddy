"""Candidate questions remain separate from verified-fact and document work."""
import json

import pytest

from jhb.applications import booklet, questions
from jhb.applications.question_routing import CANDIDATE, DOCUMENT, KNOWN, reconcile, route


def book():
    return {"schema_version": 1, "roles": {"sde": {}, "ml": {}}, "custom_answers": {},
            "answers": {"disclosure.gender": booklet.answer("Female", "synthetic explicit answer"),
                        "disclosure.hispanic": booklet.answer(False, "synthetic explicit answer"),
                        "disclosure.veteran": booklet.answer(False, "synthetic explicit answer")}}


def item(label="Gender", ref="gender", **extra):
    return {"question": label, "scope": {"region": "global", "board": "example"},
            "contexts": {}, "status": "pending", "history": [], "country_context": None}, {
            "job_hash": "a"*64, "ref": ref, "type": "combobox", "required": False,
            "reason": "Stored answer unavailable or incompatible with field", **extra}


@pytest.mark.parametrize("label,ref,choices", [("Gender", "gender", ["Female", "Male"]),
    ("Are you Hispanic/Latino?", "hispanic_ethnicity", ["Yes", "No"]),
    ("Veteran Status", "veteran_status", ["I am not a protected veteran", "I am a protected veteran"])])
def test_exact_verified_disclosures_route_to_agent_even_after_widget_failure(label, ref, choices):
    record, context = item(label, ref, choices=choices)
    assert route(book(), record, context) == KNOWN
    context["choices"] = []
    assert route(book(), record, context) == KNOWN  # failed catalog is not a new fact


def test_unknown_or_incompatible_choices_are_still_candidate_questions():
    record, context = item(choices=["Transgender woman", "Cisgender woman", "Other"])
    assert route(book(), record, context) == CANDIDATE
    context["choices"] = ["Female", "Female"]
    assert route(book(), record, context) == CANDIDATE
    context["choices"] = []
    changed = book(); changed["answers"]["disclosure.gender"] = booklet.answer()
    assert route(changed, record, context) == CANDIDATE
    record["question"] = "Do you identify as transgender?"
    assert route(book(), record, context) == CANDIDATE


def test_employer_guidance_or_public_proof_cannot_be_hidden_by_a_known_label():
    record, context = item(description="Please answer in your own words. Do not use generative AI.")
    assert route(book(), record, context) == CANDIDATE
    context["description"] = "Important eligibility restriction"
    context["description_truncated"] = True
    assert route(book(), record, context) == CANDIDATE


@pytest.mark.parametrize("label", ["Cover Letter", "Upload cover letter", "Portfolio or Cover Letter"])
def test_documents_are_agent_work_not_candidate_questions(label):
    record, context = item(label, "cover_letter", type="file", required=True)
    assert route(book(), record, context) == DOCUMENT
    context["description"] = "Do not use AI to write your answer."
    assert route(book(), record, context) == CANDIDATE
    context.update(description="", type="textarea")
    assert route(book(), record, context) == CANDIDATE


def test_maintenance_preserves_answers_history_permissions_and_is_idempotent(tmp_path):
    path = tmp_path / "private" / "book.json"
    data = book(); booklet.write_private(path, data)
    job = {"url": "https://job-boards.greenhouse.io/example/jobs/1", "dedupe_hash": "a"*64}
    question = questions.collect(job, {"optional_questions": [{"question": "Gender", "ref": "gender", "type": "combobox",
        "answer_key": "disclosure.gender", "choices": ["Female", "Male"]}]}, path)[0]
    before = booklet.load(path)
    before["question_handoffs"][question["id"]]["contexts"][job["dedupe_hash"]].pop("routing")
    booklet.write_private(path, before)
    result = reconcile(path)
    assert result["changed_contexts"] == 1 and result["agent_tasks"][0]["task_kind"] == KNOWN
    saved = booklet.load(path)
    assert saved["answers"] == before["answers"] and saved["custom_answers"] == before["custom_answers"]
    assert saved["question_handoffs"][question["id"]]["status"] == "pending"
    assert questions.pending(path) == []
    assert path.stat().st_mode & 0o777 == 0o600
    unchanged = path.read_bytes()
    assert reconcile(path)["changed_contexts"] == 0 and path.read_bytes() == unchanged
    with pytest.raises(questions.QuestionChanged):
        questions.answer(question["id"], "Another answer", path, context_job_hashes=[job["dedupe_hash"]])
    assert path.read_bytes() == unchanged


def test_mixed_contexts_filter_individually_and_restore_when_fact_changes(tmp_path):
    path = tmp_path / "private" / "book.json"; booklet.write_private(path, book())
    first = {"url": "https://job-boards.greenhouse.io/example/jobs/1", "dedupe_hash": "a"*64}
    second = {"url": "https://job-boards.greenhouse.io/example/jobs/2", "dedupe_hash": "b"*64}
    for job, choices in [(first, ["Female", "Male"]), (second, ["Cisgender woman", "Transgender woman"])]:
        questions.collect(job, {"optional_questions": [{"question": "Gender", "ref": "gender", "type": "combobox", "choices": choices}]}, path)
    pending = questions.pending(path)
    assert len(pending) == 1 and set(pending[0]["contexts"]) == {second["dedupe_hash"]}
    reconcile(path)
    saved = booklet.load(path); saved["answers"]["disclosure.gender"] = booklet.answer()
    booklet.write_private(path, saved)
    assert set(questions.pending(path)[0]["contexts"]) == {first["dedupe_hash"], second["dedupe_hash"]}


def test_current_disability_is_not_a_verified_lifetime_history_answer():
    record, context = item("Disability Status", "disability_status",
        choices=["Yes, I have a disability, or have had one in the past", "No, I do not have a disability and have not had one in the past"])
    data = book(); data["answers"]["disclosure.disability"] = booklet.answer(False,
        {"question": "Do you have a disability?", "provider": "synthetic current-only profile"})
    assert route(data, record, context) == CANDIDATE


def test_metadata_with_wrong_known_answer_key_cannot_hide_unknown_fact():
    record, context = item("Are you a US citizen?", "citizen", answer_key="disclosure.hispanic", choices=["Yes", "No"])
    assert route(book(), record, context) == CANDIDATE


def test_bounded_maintenance_fails_without_partial_save(tmp_path):
    path = tmp_path / "private" / "book.json"; booklet.write_private(path, book())
    job = {"url": "https://job-boards.greenhouse.io/example/jobs/1", "dedupe_hash": "a"*64}
    questions.collect(job, {"missing": [{"question": "Cover Letter", "ref": "cover", "type": "file"}]}, path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="limit"):
        reconcile(path, max_contexts=0)
    assert path.read_bytes() == before


def test_public_exact_us_authorization_routes_known_fact_without_creating_native_proof():
    record, context = item("U.S. WORK AUTHORIZATION*", "question_123", required=True, choices=["Yes", "No"])
    context["public_question_metadata"] = {"source": "official_public_question_metadata", "field_ref": "question_123",
        "label": "U.S. WORK AUTHORIZATION", "type": "multi_value_single_select", "required": True,
        "description": "Are you authorized to work in the United States?", "choices": ["Yes", "No"]}
    data = book(); data["answers"]["eligibility.authorized_us"] = booklet.answer(True, "synthetic authorization answer")
    original = json.dumps((data, record, context), sort_keys=True)
    assert route(data, record, context) == KNOWN
    assert json.dumps((data, record, context), sort_keys=True) == original
    context["public_question_metadata"]["description"] = "Are you a US citizen or permanent resident?"
    assert route(data, record, context) == CANDIDATE
    context["public_question_metadata"]["description"] = "Are you authorized to work in the United States?"
    context["description"] = "Are you authorized to work without sponsorship?"
    assert route(data, record, context) == CANDIDATE
    context["description"] = ""; record["country_context"] = "canada"
    assert route(data, record, context) == CANDIDATE


def test_runtime_owned_authorization_enrichment_is_ephemeral_and_exact():
    from jhb.applications.question_routing import field_route
    field = {"label": "U.S. WORK AUTHORIZATION", "ref": "question_123", "type": "combobox", "required": True,
             "description": "Are you authorized to work in the United States?", "description_truncated": False,
             "options": [{"label": "Yes", "value": "1"}, {"label": "No", "value": "0"}]}
    answers = {"eligibility.authorized_us": booklet.answer(False, "synthetic explicit user fact")}
    original = json.dumps(answers, sort_keys=True)
    assert field_route(field, answers) == KNOWN
    assert json.dumps(answers, sort_keys=True) == original
    field["description"] = "Are you authorized to work without sponsorship?"
    assert field_route(field, answers) == CANDIDATE


def test_existing_verified_cover_letter_upload_is_mechanical_not_regeneration():
    from jhb.applications.question_routing import field_route
    answers = {"documents.cover_letter": booklet.answer("/synthetic/exact-employer.pdf", {"job_hash": "a"*64, "sha256": "b"*64})}
    assert field_route({"label": "Cover Letter", "ref": "cover", "type": "file", "required": False}, answers) == KNOWN


def test_partial_visible_reply_does_not_override_or_resume_hidden_known_job(tmp_path):
    path = tmp_path / "private" / "book.json"; booklet.write_private(path, book())
    first = {"url": "https://job-boards.greenhouse.io/example/jobs/1", "dedupe_hash": "a"*64}
    second = {"url": "https://job-boards.greenhouse.io/example/jobs/2", "dedupe_hash": "b"*64}
    for job, choices in [(first, ["Female", "Male"]), (second, ["Cisgender woman", "Transgender woman"])]:
        q = questions.collect(job, {"optional_questions": [{"question": "Gender", "ref": "gender", "type": "combobox", "choices": choices}]}, path)[0]
    assert questions.answer(q["id"], "Cisgender woman", path, context_job_hashes=[second["dedupe_hash"]]) == [second["dedupe_hash"]]
    data = booklet.load(path); record = data["question_handoffs"][q["id"]]
    response = data["custom_answers"][record["custom_answer_key"]]
    assert response["job_hashes"] == [second["dedupe_hash"]]
    assert response["source"]["contexts"] == [second["dedupe_hash"]]
    assert route(data, record, record["contexts"][first["dedupe_hash"]]) == KNOWN
    from jhb.applications.question_routing import agent_contexts
    assert set(agent_contexts(data, record)) == {first["dedupe_hash"]}
    questions.reconcile(first, {"filled": [{"ref": "gender"}]}, path)
    saved = booklet.load(path)
    assert saved["question_handoffs"][q["id"]]["status"] == "answered"
    assert saved["question_handoffs"][q["id"]]["contexts"][first["dedupe_hash"]]["resolved"] is True


def test_published_own_wording_instruction_cannot_route_cover_file_to_generation():
    record, context = item("Cover Letter", "cover_letter", type="file", required=False)
    context["public_question_metadata"] = {"source": "official_public_question_metadata", "field_ref": "cover_letter",
        "description": "Please write in your own words. Do not use generative AI."}
    assert route(book(), record, context) == CANDIDATE
