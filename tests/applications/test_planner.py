import pytest

from jhb.applications import booklet
from jhb.applications.planner import deterministic_plan, validate_plan
from jhb.applications.worker import role_for_job


def test_role_routing_requires_explicit_choice_for_mixed_roles():
    assert role_for_job({"role_classes": "swe"}) == "sde"
    assert role_for_job({"role_classes": "ml"}) == "ml"
    assert role_for_job({"role_classes": "swe,ml"}) is None


@pytest.mark.parametrize("title", [
    "Software Engineer New Grad, Machine Learning Platform (Remote)",
    "Machine-Learning Engineer", "AI/ML Engineer", "ML Platform Engineer",
    "Data Scientist I", "Applied Scientist, New Grad",
])
@pytest.mark.parametrize("classes", ["swe,ml", ["swe", "ml"]])
def test_explicit_ml_specialization_resolves_mixed_resume_routing(title, classes):
    assert role_for_job({"role_classes": classes, "title": title}) == "ml"


@pytest.mark.parametrize("title", ["Software Engineer", "AI Product Engineer", "HTML Engineer", "Software Engineer, AIML Labs"])
def test_company_and_description_keywords_do_not_resolve_ambiguous_resume(title):
    assert role_for_job({"role_classes": "swe,ml", "title": title,
                         "company": "Machine Learning Company",
                         "description": "We build machine learning products."}) is None


def test_title_choice_does_not_override_unambiguous_class_or_accept_unknown_classes():
    assert role_for_job({"role_classes": "swe", "title": "Machine Learning Platform Engineer"}) == "sde"
    assert role_for_job({"role_classes": "swe,ml,other", "title": "Machine Learning Engineer"}) is None


def test_application_city_and_mailing_city_use_separate_answers():
    snapshot = {"fields": [
        {"ref": "candidate-location", "label": "Location (City)*", "type": "combobox"},
        {"ref": "mailing-city", "label": "City", "type": "text"},
    ], "buttons": []}
    answers = {
        "preferences.application_city": booklet.answer("Example Metro, CA", "explicit user input"),
        "identity.city": booklet.answer("Example Neighborhood", "explicit user input"),
        "identity.location": booklet.answer("Example Neighborhood, CA, USA", "synthetic profile"),
    }
    plan = validate_plan(deterministic_plan(snapshot, answers), snapshot, answers)
    assert plan["bindings"] == [
        {"ref": "candidate-location", "answer_key": "preferences.application_city"},
        {"ref": "mailing-city", "answer_key": "identity.city"},
    ]


def test_exact_employer_answer_takes_priority_over_standing_default():
    question = "Are you related to anyone currently employed at Example Corp?"
    snapshot = {"fields": [{"ref": "relative", "label": question, "type": "combobox"}], "buttons": []}
    answers = {
        "screening.employee_relative": booklet.answer(False, "explicit standing preference"),
        "custom.example": {**booklet.answer(True, "explicit employer-specific answer"), "question": question},
    }
    plan = deterministic_plan(snapshot, answers)
    assert plan["bindings"] == [{"ref": "relative", "answer_key": "custom.example"}]
    assert validate_plan(plan, snapshot, answers) == plan


def test_multiple_education_rows_keep_their_own_institutions():
    book = {"answers": {}, "roles": {"sde": {}, "ml": {}}, "education_records": [
        {"school": "Example Graduate University", "degree": "Master of Science", "status": "verified", "source": "synthetic resume"},
        {"school": "Example Undergraduate College", "degree": "Bachelor of Science", "status": "verified", "source": "synthetic resume"},
    ]}
    answers = booklet.for_role(book, "ml")
    snapshot = {"fields": [{"ref": f"school--{i}", "label": "School", "type": "combobox"} for i in [0, 1]], "buttons": []}
    plan = validate_plan(deterministic_plan(snapshot, answers), snapshot, answers)
    assert [b["answer_key"] for b in plan["bindings"]] == ["education.0.school", "education.1.school"]
    assert answers["education.0.school"]["value"] != answers["education.1.school"]["value"]


def test_question_response_does_not_cross_education_or_country_context():
    from jhb.applications.planner import key_for_field
    answers = {"custom.school": {**booklet.answer("Other", "synthetic explicit answer"), "question": "School", "field_ref": "school--1"},
               "custom.consent": {**booklet.answer(True, "synthetic explicit answer"), "question": "Employer certification", "country_context": "united states"}}
    assert key_for_field({"ref": "school--0", "label": "School", "type": "combobox"}, answers) is None
    assert key_for_field({"ref": "school--1", "label": "School", "type": "combobox"}, answers) == "custom.school"
    assert key_for_field({"ref": "consent", "label": "Employer certification", "type": "checkbox", "country_context": "united kingdom"}, answers) is None


def test_planner_cannot_guess_screening_or_click_final_submit():
    snapshot = {"fields": [{"ref": "sponsor", "label": "Do you need employer-specific approval?", "type": "text"}],
                "buttons": [{"ref": "submit", "label": "Submit application"}]}
    answers = {"eligibility.sponsorship": booklet.answer(True, "synthetic user")}
    plan = deterministic_plan(snapshot, answers)
    assert plan["bindings"] == [] and plan["next_ref"] is None
    with pytest.raises(ValueError, match="reinterpret"):
        validate_plan({"bindings": [{"ref": "sponsor", "answer_key": "eligibility.sponsorship"}],
                       "next_ref": None, "reason": "guess"}, snapshot, answers)
    with pytest.raises(ValueError, match="Terminal"):
        validate_plan({"bindings": [], "next_ref": "submit", "reason": "submit"}, snapshot, answers)


def test_repeated_controls_cannot_receive_conflicting_bindings():
    snapshot = {"fields": [{"ref": "email", "label": "Email", "type": "text"}], "buttons": []}
    answers = {"identity.email": booklet.answer("sam@example.test", "synthetic user")}
    binding = {"ref": "email", "answer_key": "identity.email"}
    with pytest.raises(ValueError, match="duplicate"):
        validate_plan({"bindings": [binding, binding], "next_ref": None, "reason": "duplicate"}, snapshot, answers)
