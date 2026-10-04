"""Known profile facts reuse safely, without changing qualification or preference facts."""
import copy

import pytest

from jhb.applications import booklet, worker
from jhb.applications.planner import deterministic_plan, key_for_field, validate_plan


def field(label, kind="text", *, choices=(), required=True, country=None):
    return {"ref": "field:" + label, "label": label, "type": kind, "required": required,
            "options": [{"label": value, "value": str(i), "disabled": False} for i, value in enumerate(choices)],
            **({"country_context": country} if country else {})}


def book():
    return {"answers": {
        "identity.first_name": booklet.answer("Taylor", "synthetic identity"),
        "identity.last_name": booklet.answer("Example", "synthetic identity"),
        "identity.full_name": booklet.answer("Taylor Example", "synthetic identity"),
        "links.github": booklet.answer("https://github.com/synthetic-candidate", "synthetic identity"),
        "preferences.application_city": booklet.answer("San Diego, CA", "synthetic candidate choice"),
        "preferences.relocation": booklet.answer(True, "synthetic candidate anywhere choice"),
        "preferences.start_date": booklet.answer("January 2030", "synthetic candidate availability"),
        "education.expected_graduation_date": booklet.answer("2030-12-14", "synthetic candidate date"),
        "eligibility.sponsorship": booklet.answer(True, "synthetic candidate US sponsorship answer"),
        "eligibility.authorized_us": booklet.answer(True, "synthetic candidate US authorization answer"),
    }, "roles": {"sde": {
        "role.skills": booklet.answer("Python, LangGraph, React, Docker", "synthetic SDE resume"),
        "role.education": booklet.answer("Coursework: Deep Learning, Computer Vision", "synthetic SDE resume"),
        "role.experience": booklet.answer(
            "Cloud Example  Jun 2026 – Sep 2026\nSDE Intern  Seattle, WA\n• Built an OpenClaw agent.\n"
            "Earlier Example  May 2024 – Oct 2024\nResearch Intern  Paris, France\n• Improved search speed.\n"
            "Regular Employer  Feb 2025 – Aug 2025\nAI Engineer  London, UK\n• Built agentic AI and RAG.\n", "synthetic SDE resume"),
    }, "ml": {"role.skills": booklet.answer("TensorFlow, PEFT", "synthetic ML resume")}},
        "workflow_preferences": {"preferred_first_name": {"optional": "leave blank", "required": "use identity.first_name"},
                                 "relocation": "Open to relocating anywhere"},
        "education_records": [{"school": "Example University", "degree": "Master of Science", "major": "Computer Science",
            "start_date": "2025-09", "end_date": "2030-12", "expected": True, "status": "verified", "source": "synthetic education"},
            {"school": "Undergrad Example", "degree": "Bachelor of Science", "major": "Mathematics",
             "start_date": "2021-07", "end_date": "2025-05", "expected": False, "status": "verified", "source": "synthetic education"}]}


def value(control, answers):
    key = key_for_field(control, answers)
    return answers[key]["value"] if key else None


def test_names_links_and_required_preferred_last_reuse_verified_facts_only():
    answers = booklet.for_role(book(), "sde")
    answers["identity.location"] = booklet.answer("Example Suburb, CA, USA", "synthetic older address locality")
    assert value(field("Current Location", "combobox"), answers) == "San Diego, CA"
    assert value(field("Legal First and Last Name"), answers) == "Taylor Example"
    assert value(field("Github Link"), answers) == "https://github.com/synthetic-candidate"
    assert value(field("Preferred Last Name"), answers) == "Example"
    assert value(field("Preferred Last Name", required=False), answers) is None
    assert value(field("Former Legal Name"), answers) is None


def test_current_study_uses_expected_record_without_claiming_earned_masters():
    original = book()
    before = copy.deepcopy(original)
    answers = booklet.for_role(original, "sde")
    assert value(field("University"), answers) == "Example University"
    assert value(field("Graduation Date"), answers) == "12/14/2030"
    degree = field("Degree Type", "multiselect", choices=["Undergraduate/Bachelor's", "Master's", "PhD"])
    assert value(degree, answers) == ["Master's"]
    assert answers[key_for_field(degree, answers)]["source"]["earned_degree_claim"] is False
    assert value(field("What degree have you already earned?"), answers) is None
    assert original == before and original["education_records"][0]["expected"] is True


@pytest.mark.parametrize("change", ["unverified", "missing_source", "expired_expected", "future_start", "multiple_expected"])
def test_current_study_is_not_guessed_from_incomplete_ambiguous_or_stale_records(change):
    original = book()
    record = original["education_records"][0]
    if change == "unverified": record["status"] = "needs_input"
    elif change == "missing_source": record["source"] = ""
    elif change == "expired_expected": record["end_date"] = "2025-12"
    elif change == "future_start": record["start_date"] = "2029-01"
    else: original["education_records"].append(dict(record, school="Second Current Example"))
    answers = booklet.for_role(original, "sde")
    assert value(field("University"), answers) is None
    assert value(field("Degree Type", "multiselect", choices=["Master's"]), answers) is None


def test_latest_internship_reuses_original_dates_but_resume_does_not_prove_total_count():
    answers = booklet.for_role(book(), "sde")
    assert value(field("Where was your last internship?"), answers) == "Cloud Example, SDE Intern, 2026-06–2026-09"
    assert value(field("How many prior internships have you had?", "radio", choices=["0", "1", "2", "3+"]), answers) is None


def test_explicit_reviewed_internship_count_takes_precedence_over_resume_records():
    answers = booklet.for_role(book(), "sde")
    control = field("How many prior internships have you had?", "radio", choices=["0", "1", "2", "3+"])
    answers["custom.reviewed"] = {**booklet.answer("3+", "synthetic candidate-reviewed exact employer answer"), "question": control["label"]}
    assert key_for_field(control, answers) == "custom.reviewed"
    assert value(control, answers) == "3+"


def test_ai_technologies_include_only_verified_chosen_role_tokens_and_relevant_courses():
    answers = booklet.for_role(book(), "sde")
    result = value(field("What are some AI specific technologies you are comfortable with?"), answers)
    for token in ["LangGraph", "OpenClaw", "agentic AI", "RAG", "Deep Learning", "Computer Vision"]:
        assert token in result
    for token in ["TensorFlow", "PEFT", "React", "Docker"]:
        assert token not in result
    assert answers["standing.ai_technologies"]["source"]["selected_role"] == "sde"


def test_observed_quarter_requires_exact_verified_start_month_and_unique_valid_range():
    answers = booklet.for_role(book(), "sde")
    control = field("Please indicate which quarter you would be able to start work for this position.", "multiselect",
                    choices=["Q1: January 2030 - March 2030", "Q2: April 2030 - June 2030"])
    assert value(control, answers) == ["Q1: January 2030 - March 2030"]
    answers["preferences.start_date"] = booklet.answer("January 2031", "synthetic later availability")
    assert value(control, answers) is None
    answers["preferences.start_date"] = booklet.answer("January 2030", "synthetic availability", "needs_input")
    assert value(control, answers) is None


def test_anywhere_relocation_reuses_all_observed_cities_and_does_not_claim_local_residence():
    answers = booklet.for_role(book(), "sde")
    cities = field("Please indicate all locations that you would be interested in relocating to for this position.", "multiselect",
                   choices=["New York, NY", "San Francisco, CA"])
    assert value(cities, answers) == ["New York, NY", "San Francisco, CA"]
    hybrid = field("This role is tied to the office location listed in the job posting. Team members are expected to work from the office 3 days per week as part of Example’s hybrid work model. Are you currently based in the listed location and able to work in person 3 days per week?", "radio", choices=[
        "Yes, I’m based in this location and able to work from the office 3 days per week",
        "No, I’m not based in this location but willing to relocate", "No, I’m only able to work remotely"])
    assert value(hybrid, answers) == "No, I’m not based in this location but willing to relocate"
    answers["preferences.application_city"] = booklet.answer("San Francisco, CA", "synthetic current SF residence")
    assert value(hybrid, answers) is None  # no automatic affirmative combined residence/willingness claim
    cities["options"].append({"label": "I already live here", "disabled": False})
    assert value(cities, answers) is None


def test_present_future_sponsorship_richer_choice_has_us_context_and_exact_positive_meaning():
    answers = booklet.for_role(book(), "sde")
    control = field("Will you now or will you in the future require employment visa sponsorship?", "radio", country="united states",
                    choices=["Yes, I will require Example to sponsor my employment", "No, I do not require sponsorship to work in the country where this role is located"])
    assert value(control, answers) == control["options"][0]["label"]
    for country in [None, "canada", "United States and Canada"]:
        control["country_context"] = country
        assert value(control, answers) is None
    control["country_context"] = "united states"
    control["options"][0]["label"] = "Yes, but only for unrestricted permanent work authorization"
    assert value(control, answers) is None


def test_any_employer_cannot_be_derived_from_generic_authorized_boolean():
    answers = booklet.for_role(book(), "sde")
    control = field("Are you legally authorized to work in the country where this role is located, for any employer?", "radio", country="united states", choices=["Yes", "No"])
    assert value(control, answers) is None
    answers["custom.reviewed_any_employer"] = {**booklet.answer("Yes", "synthetic candidate-reviewed same exact employer question"),
        "question": control["label"], "country_context": "united states"}
    assert value(control, answers) == "Yes"
    control["country_context"] = "canada"
    assert value(control, answers) is None


@pytest.mark.parametrize("country", [None, "United States and Canada", "Unknown"])
def test_worker_does_not_infer_mixed_or_unknown_posting_country(country):
    snapshot = {"fields": [field("Are you legally authorized to work in the country where this role is located, for any employer?")]}
    booklet.annotate_work_country(snapshot, {"work_country": country})
    assert not snapshot["fields"][0].get("country_context")


def test_worker_attaches_explicit_posting_country_but_preserves_more_specific_observed_country():
    controls = [field("Are you legally authorized to work in the country where this role is located, for any employer?"),
                field("Will you now or will you in the future require employment visa sponsorship?", country="canada"),
                field("Do you hold unrestricted global work authorization?")]
    booklet.annotate_work_country({"fields": controls}, {"work_country": "United States"})
    assert controls[0]["country_context"] == "united states"
    assert controls[1]["country_context"] == "canada"
    assert not controls[2].get("country_context")


def test_derived_bindings_round_trip_exact_plan_validator_with_observed_native_options():
    answers = booklet.for_role(book(), "sde")
    snapshot = {"fields": [field("Legal First and Last Name"), field("University"), field("Graduation Date"),
        field("Degree Type", "multiselect", choices=["Master's", "PhD"])], "buttons": [{"ref": "submit", "label": "Submit Application"}]}
    plan = deterministic_plan(snapshot, answers)
    assert len(plan["bindings"]) == 4 and plan["next_ref"] is None
    assert validate_plan(plan, snapshot, answers) == plan


def test_final_audit_rebinds_prepared_derived_records_without_reinferring_profile():
    from jhb.applications import submission_runtime
    answers = booklet.for_role(book(), "sde")
    fields = [field("Graduation Date"), field("Degree Type", "multiselect", choices=["Master's", "PhD"]),
              field("Please indicate which quarter you would be able to start work for this position.", "multiselect",
                    choices=["Q1: January 2030 - March 2030"]),
              field("Please indicate all locations that you would be interested in relocating to for this position.", "multiselect",
                    choices=["New York, NY", "San Francisco, CA"])]
    prepared = []
    for control in fields:
        key = key_for_field(control, answers)
        assert key
        prepared.append({"key": key, "ref": control["ref"], "question": control["label"],
                         "value": answers[key]["value"], "source": answers[key]["source"]})
    # The final runtime owns only packet-retained records, not the full booklet.
    retained = {r["key"]: booklet.answer(r["value"], r["source"]) for r in prepared}
    for record in prepared:
        if record["key"].startswith("custom."):
            retained[record["key"]].update(question=record["question"], field_ref=record["ref"])
    for control, record in zip(fields, prepared):
        assert key_for_field(control, retained) == record["key"]
    assert submission_runtime.annotate_work_country is booklet.annotate_work_country


def test_same_scoped_reviewed_authorization_survives_prepare_and_final_annotations():
    from jhb.applications import submission_runtime
    question = "Are you legally authorized to work in the country where this role is located, for any employer?"
    answer = {**booklet.answer("Yes", "synthetic exact user-reviewed employer answer"),
              "question": question, "country_context": "united states"}
    for annotation in (booklet.annotate_work_country, submission_runtime.annotate_work_country):
        snapshot = {"fields": [field(question, "radio", choices=["Yes", "No"])]}
        annotation(snapshot, {"work_country": "United States"})
        assert key_for_field(snapshot["fields"][0], {"custom.reviewed": answer}) == "custom.reviewed"
        snapshot["fields"][0]["country_context"] = "canada"
        annotation(snapshot, {"work_country": "United States"})
        assert key_for_field(snapshot["fields"][0], {"custom.reviewed": answer}) is None


def test_graduation_day_cannot_be_combined_with_a_different_current_degree_month():
    original = book()
    original["answers"]["education.expected_graduation_date"] = booklet.answer("2029-12-14", "synthetic another degree's date")
    assert value(field("Graduation Date"), booklet.for_role(original, "sde")) is None


@pytest.mark.parametrize("observed_country,expected_ok", [(None, True), ("canada", False)])
def test_final_runtime_checks_retained_newgrad_and_scoped_authorization_without_clicks(monkeypatch, observed_country, expected_ok):
    from jhb.applications import submission_runtime
    url = "https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application"
    question = "Are you legally authorized to work in the country where this role is located, for any employer?"
    answers = booklet.for_role(book(), "sde")
    answers["custom.reviewed_authorization"] = {**booklet.answer("Yes", "synthetic candidate-reviewed exact employer answer"),
        "question": question, "country_context": "united states"}
    controls = [field("Legal First and Last Name"), field("University"), field("Graduation Date"),
        field("Degree Type", "multiselect", choices=["Master's", "PhD"]),
        field("What are some AI specific technologies you are comfortable with?"),
        field("Please indicate which quarter you would be able to start work for this position.", "multiselect",
              choices=["Q1: January 2030 - March 2030"]),
        field("Please indicate all locations that you would be interested in relocating to for this position.", "multiselect",
              choices=["New York, NY", "San Francisco, CA"]),
        field(question, "radio", choices=["Yes", "No"], country="united states")]
    packet = {"job": {"work_country": "United States"}, "filled": []}
    retained = {}
    for control in controls:
        key = key_for_field(control, answers)
        record = {"key": key, "value": answers[key]["value"], "source": answers[key]["source"],
                  "question": control["label"], "ref": control["ref"]}
        if control.get("country_context"):
            record["country_context"] = control["country_context"]
        packet["filled"].append(record)
        retained[control["ref"]] = {"value": str(record["value"]) if control["type"] == "text" else "",
            "selected": record["value"] if isinstance(record["value"], list) else [record["value"]], "invalid": False}
    controls[-1]["country_context"] = observed_country
    snapshot = {"fields": controls, "buttons": [{"ref": "42", "label": "Submit application"}]}
    monkeypatch.setattr(submission_runtime, "_board_dispatch", lambda *args: copy.deepcopy(snapshot))
    monkeypatch.setattr(submission_runtime, "_control_state", lambda helpers, f, board: retained[f["ref"]])
    monkeypatch.setattr(submission_runtime, "_native_form_submit", lambda *args: True)
    def js(expression):
        if expression == "location.href": return url
        if expression == "window.__jhbGuard===true": return True
        if "input[type=password]" in expression: return False
        raise AssertionError("unexpected script in read-only final checks")
    helpers = {"js": js, "current_tab": lambda: {"targetId": "owned"}}
    result = submission_runtime._checks({"target_id": "owned", "documents": {}}, helpers, packet, {"application_url": url})
    if expected_ok:
        assert result["double_check_count"] == len(controls)
        assert result["button"]["ref"] == "42"  # returned for caller; never clicked
        assert result["fields"][-1]["country_context"] == "united states"
    else:
        assert result["state"] == "waiting_review" and result["click_started"] is False


@pytest.mark.parametrize('availability,expected', [
    ('2029-12-14', 'Q1: January 2030 - March 2030'),
    ('2030-03-31', 'Q1: January 2030 - March 2030'),
    ('2030-04-01', 'Q2: April 2030 - June 2030'),
    ('2030-06', 'Q2: April 2030 - June 2030'),
    ('2030-07-01', None), ('2030-02-30', None), ('December 14', None),
])
def test_first_offered_quarter_respects_full_date_or_month_earliest_availability(availability, expected):
    answers = booklet.for_role(book(), 'sde')
    answers['preferences.start_date'] = booklet.answer(availability, 'synthetic earliest availability')
    control = field('Please indicate which quarter you would be able to start work for this position.', 'multiselect',
                    choices=['Q2: April 2030 - June 2030', 'Other', 'Q1: January 2030 - March 2030'])
    assert value(control, answers) == ([expected] if expected else None)
    assert answers['preferences.start_date']['value'] == availability


def test_duplicate_or_invalid_earliest_quarter_is_not_guessed():
    answers = booklet.for_role(book(), 'sde')
    answers['preferences.start_date'] = booklet.answer('2029-12-14', 'synthetic earliest availability')
    control = field('Please indicate which quarter you would be able to start work for this position.', 'multiselect',
                    choices=['Q1: January 2030 - March 2030'] * 2)
    assert value(control, answers) is None
    control['options'] = [{'label': 'Q1: April 2030 - June 2030', 'disabled': False}]
    assert value(control, answers) is None
