"""Old contexts reuse verified facts without choosing a role or erasing questions."""
from copy import deepcopy

import pytest

from jhb.applications import booklet, known_answers, planner, question_routing


def book():
    return {"answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {},
            "education_records": [
                {"school": "Synthetic Graduate School", "degree": "Master of Science", "major": "Computer Science",
                 "start_date": "2025-09", "end_date": "2026-12-14", "expected": True,
                 "status": "verified", "source": "synthetic graduate source"},
                {"school": "Synthetic Undergraduate School", "degree": "Bachelor of Science", "major": "Mathematics",
                 "start_date": "2021-07", "end_date": "2025-05", "expected": False,
                 "status": "verified", "source": "synthetic undergraduate source"}]}


def context(label, ref, *, kind="combobox", choices=None, description="", **kwargs):
    return {"question": label, "scope": "synthetic", "country_context": None}, {
        "ref": ref, "type": kind, "required": True, "choices": choices or [],
        "description": description, "job_hash": "a" * 64, **kwargs}


@pytest.mark.parametrize("label,ref,kind", [
    ("School*", "school--0", "combobox"), ("School*", "school--1", "combobox"),
    ("Start date year", "start-year--1", "number"), ("End date year", "end-year--1", "number")])
def test_legacy_roleless_context_routes_common_indexed_original_education(label, ref, kind):
    data = book(); before = deepcopy(data)
    record, observed = context(label, ref, kind=kind)
    assert question_routing.route(data, record, observed) == question_routing.KNOWN
    assert data == before
    shared = question_routing._shared_education(data)
    assert shared["education.0.end_year"]["source"]["expected"] is True
    assert shared["education.1.end_year"]["value"] == "2025"
    assert all(key.startswith("education.") for key in shared)


def test_unverified_source_or_incompatible_school_catalog_remains_candidate():
    record, observed = context("School*", "school--1")
    data = book(); data["education_records"][1]["status"] = "needs_input"
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    data = book(); observed["choices"] = ["Another School"]
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    observed["choices"] = []; observed["ref"] = "school--3"
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE


def test_roleless_projection_requires_both_roles_agree_and_have_verified_sources(monkeypatch):
    original = booklet.for_role
    def altered(data, role):
        result = original(data, role)
        if role == "ml":
            result["education.1.school"] = booklet.answer("Different verified school", "synthetic alternate")
            result["education.1.start_year"]["source"] = None
        return result
    monkeypatch.setattr(booklet, "for_role", altered)
    shared = question_routing._shared_education(book())
    assert "education.1.school" not in shared and "education.1.start_year" not in shared
    record, observed = context("School*", "school--1")
    assert question_routing.route(book(), record, observed) == question_routing.CANDIDATE
    observed["selected_role"] = "sde"
    assert question_routing.route(book(), record, observed) == question_routing.KNOWN


def test_missing_source_in_legacy_education_is_not_a_dashboard_failure():
    data = book(); data["education_records"][0].pop("source")
    record, observed = context("School*", "school--0")
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE


def test_exact_saved_office_policy_routes_known_willingness_but_not_relocation_need():
    data = book(); data["workflow_preferences"] = {"office_locations": {
        "value": True, "source": "Explicit candidate standing office willingness"}}
    record, observed = context("I am willing and able to work entirely on-site.*", "onsite", choices=["Yes", "No"])
    assert question_routing.route(data, record, observed) == question_routing.KNOWN
    data["workflow_preferences"] = {}
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    data["workflow_preferences"] = {"office_locations": True}
    record["question"] = "I will need relocation to work on-site.*"
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE


def government_book(value=False):
    data = book(); data["answers"]["screening.us_government_or_military_5y"] = booklet.answer(value, "synthetic explicit five-year government/military response")
    return data


def test_native_owned_conflict_question_derives_only_the_narrow_false_conjunction():
    data = government_book(); answers = dict(data["answers"])
    field = {"label": "CONFLICT OF INTEREST*", "ref": "question_101", "type": "combobox", "required": True,
             "description": known_answers.GOVERNMENT_CONFLICT_DESCRIPTION, "description_truncated": False,
             "options": [{"label": "Yes", "value": "101"}, {"label": "No", "value": "102"}]}
    key = known_answers.enrich(field, {}, answers)
    assert key and answers[key]["value"] is False and planner.key_for_field(field, answers) == key
    assert "screening.us_government_or_military_5y" in answers[key]["source"]["records"]
    assert data["answers"] == government_book()["answers"]
    record, observed = context(field["label"], field["ref"], choices=["Yes", "No"], description=field["description"])
    assert question_routing.route(data, record, observed) == question_routing.KNOWN
    assert question_routing.route(government_book(True), record, observed) == question_routing.CANDIDATE


def test_public_conflict_description_routes_work_but_never_becomes_native_answer_proof():
    record, observed = context("CONFLICT OF INTEREST*", "question_101")
    observed["public_question_metadata"] = {"source": "official_public_question_metadata", "field_ref": observed["ref"],
        "label": "CONFLICT OF INTEREST", "type": "multi_value_single_select", "required": True,
        "description": known_answers.GOVERNMENT_CONFLICT_DESCRIPTION, "choices": ["Yes", "No"]}
    data = government_book(); before = deepcopy((data, record, observed))
    assert question_routing.route(data, record, observed) == question_routing.KNOWN
    assert (data, record, observed) == before
    native = {"label": record["question"], "ref": observed["ref"], "type": "combobox", "required": True,
              "description": "", "description_truncated": False, "options": []}
    assert known_answers.enrich(native, {}, dict(data["answers"])) is None


@pytest.mark.parametrize("change", ["longer_window", "different_topic", "truncated", "mismatched_ref", "duplicate_choice", "unverified"])
def test_public_conflict_proof_mismatch_and_ambiguous_facts_stay_visible(change):
    record, observed = context("CONFLICT OF INTEREST*", "question_101")
    metadata = {"source": "official_public_question_metadata", "field_ref": observed["ref"],
        "label": "CONFLICT OF INTEREST", "type": "multi_value_single_select", "required": True,
        "description": known_answers.GOVERNMENT_CONFLICT_DESCRIPTION, "choices": ["Yes", "No"]}
    observed["public_question_metadata"] = metadata; data = government_book()
    if change == "longer_window": metadata["description"] = metadata["description"].replace("5 years", "10 years")
    if change == "different_topic": metadata["description"] = "Have you ever worked for the employer?"
    if change == "truncated": observed["description_truncated"] = True
    if change == "mismatched_ref": metadata["field_ref"] = "question_102"
    if change == "duplicate_choice": metadata["choices"].append("No")
    if change == "unverified": data["answers"]["screening.us_government_or_military_5y"]["status"] = "needs_input"
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE


def test_richer_authorization_employer_history_and_medical_history_are_not_erased():
    data = government_book(); data["answers"].update({
        "eligibility.authorized_us": booklet.answer(True, "synthetic generic work authorization"),
        "disclosure.disability": booklet.answer(False, "synthetic current-only disability response")})
    record, observed = context("Are you legally authorized to work in the United States?*", "authorization",
        choices=["I am authorized to work in the United States for any employer", "I am authorized to work in the United States for my present employer only", "I require sponsorship to work in the United States"])
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    record["question"] = "Have you ever been employed by Anduril or any company that Anduril has acquired?*"
    observed["choices"] = ["Yes", "No"]
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    record["question"] = "HISTORY WITH ANDURIL*"
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
    record["question"] = "Disability Status"
    observed["ref"] = "disability_status"; observed["choices"] = ["No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    assert question_routing.route(data, record, observed) == question_routing.CANDIDATE
