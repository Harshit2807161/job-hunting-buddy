"""Repeated synthetic degree years keep scoped answers and legacy audit history."""
from copy import deepcopy

import pytest

from jhb.applications import booklet, planner, questions
from tests.applications.test_education_years import book, snapshot
from tests.applications.test_questions import job


def save(tmp_path, source=None):
    data = deepcopy(source or book())
    data.update(schema_version=1, custom_answers={}, question_handoffs={})
    path = tmp_path/"private"/"synthetic.json"
    booklet.write_private(path, data)
    return path


def missing():
    return {"state": "waiting_input", "missing": [
        {"ref": field["ref"], "question": field["label"], "type": "number", "required": True}
        for field in snapshot()["fields"]]}


def test_four_year_questions_keep_distinct_degree_refs_across_same_employer_jobs(tmp_path):
    path = save(tmp_path)
    first = questions.collect(job(), missing(), path)
    second = questions.collect(job(2), missing(), path)
    assert len(first) == len(second) == 4
    assert {r["id"] for r in first} == {r["id"] for r in second}
    assert {r["field_ref"] for r in first} == {f["ref"] for f in snapshot()["fields"]}
    assert all(len(r["contexts"]) == 2 for r in second)
    chosen = next(r for r in second if r["field_ref"] == "start-year--1")
    questions.answer(chosen["id"], "2020", path)
    saved = booklet.load(path)
    response = saved["custom_answers"][saved["question_handoffs"][chosen["id"]]["custom_answer_key"]]
    assert response["field_ref"] == "start-year--1"
    answers = booklet.for_role(saved, "sde"); answers.update(saved["custom_answers"])
    plan = planner.deterministic_plan(snapshot(), answers)
    assert [answers[b["answer_key"]]["value"] for b in plan["bindings"]] == ["2025", "2026", "2020", "2025"]
    assert [r["status"] for r in questions.pending(path)] == ["pending"]*3


@pytest.mark.parametrize("role", ["sde", "ml"])
@pytest.mark.parametrize("column", ["start", "end"])
def test_legacy_unscoped_custom_year_cannot_override_original_indexed_degree_facts(role, column):
    source = book(); original = deepcopy(source)
    answers = booklet.for_role(source, role)
    legacy = {"custom.legacy": {**booklet.answer("1999", "Synthetic old explicit response"),
                               "question": column.title()+" date year", "field_ref": None}}
    answers.update(legacy)
    for field in snapshot()["fields"]:
        assert planner.key_for_field(field, answers) == f"education.{field['ref'][-1]}.{field['ref'].split('-')[0]}_year"
    assert source == original and legacy["custom.legacy"]["value"] == "1999"


def test_legacy_unscoped_year_is_not_reused_when_original_degree_date_is_unknown():
    source = book(); source["education_records"][0].pop("start_date")
    answers = booklet.for_role(source, "sde")
    answers["custom.legacy"] = {**booklet.answer("1999", "Synthetic old explicit response"), "question": "Start date year"}
    assert planner.key_for_field(snapshot()["fields"][0], answers) is None
    assert planner.key_for_field(snapshot()["fields"][2], answers) == "education.1.start_year"


def test_scoped_year_override_never_spills_to_another_degree():
    answers = booklet.for_role(book(), "sde")
    answers["custom.exact"] = {**booklet.answer("2022", "Synthetic explicit response for graduate record"),
                               "question": "Start date year", "field_ref": "start-year--0"}
    assert planner.key_for_field(snapshot()["fields"][0], answers) == "custom.exact"
    assert planner.key_for_field(snapshot()["fields"][2], answers) == "education.1.start_year"


def test_old_merged_ledger_record_is_preserved_and_retires_only_after_verified_fill(tmp_path):
    path = save(tmp_path)
    data = booklet.load(path)
    employer = job(); scope = questions._scope(employer)
    legacy_id = questions._identity(scope, "Start date year", None, None, "field", employer["dedupe_hash"])
    legacy = {"id": legacy_id, "question": "Start date year", "normalized_question": "start date year",
              "scope": scope, "country_context": None, "kind": "field", "field_ref": None,
              "answer_key": None, "status": "pending", "created_at": "2026-01-01T00:00:00+00:00",
              "updated_at": "2026-01-01T00:00:00+00:00", "contexts": {employer["dedupe_hash"]: {
                  "ref": "start-year--1", "required": True}},
              "history": [{"event": "created", "at": "2026-01-01T00:00:00+00:00"}]}
    data["question_handoffs"][legacy_id] = deepcopy(legacy)
    booklet.write_private(path, data)
    created = questions.collect(employer, missing(), path)
    assert len(created) == 4 and legacy_id not in {r["id"] for r in created}
    assert booklet.load(path)["question_handoffs"][legacy_id] == legacy
    questions.reconcile(employer, {"filled": [{"ref": f["ref"]} for f in snapshot()["fields"]]}, path)
    preserved = booklet.load(path)["question_handoffs"][legacy_id]
    assert preserved["status"] == "resolved" and preserved["created_at"] == legacy["created_at"]
    assert preserved["history"][0] == legacy["history"][0]
    assert preserved["history"][-1]["event"] == "resolved"
    assert preserved["contexts"][employer["dedupe_hash"]]["resolution"] == "Verified field filled"


def test_existing_non_year_education_question_identity_remains_unchanged():
    scope = questions._scope(job())
    # The new regex extends recognized refs without changing existing scope,
    # kind, label or indexed School question identity.
    assert questions._identity(scope, "School", "school--0", None, "field") == "q_f70bad97dceacb5bd31d2733"
