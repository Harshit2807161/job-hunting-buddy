"""Current education projections for observed Ashby question context.

These answers describe study in progress, never an already-earned degree.
Native institution choices must come from the control's owned result list.
"""
from datetime import date
import re

from .booklet import _education_bound, normalize
from .cli_runtime import option_matches

_CURRENT_NOTE = "for most recent or in progress degree."
_SCHOOL_REF = "ashby:_systemfield_education_history:control:0"


def structured_school_option(option):
    """Validate separately observed native name/country/domain components."""
    detail = option.get("school_metadata")
    if not isinstance(detail, dict) or detail.get("source") != "owned_native_school_option":
        return None
    if any(not isinstance(detail.get(k), str) or not 0 < len(detail[k]) <= 500
           for k in ("name", "country", "domain")):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", detail["domain"]):
        return None
    if normalize(str(option.get("label", ""))) != normalize(" ".join(detail[k] for k in ("name", "country", "domain"))):
        return None
    return detail


def selection_binding(field, scope):
    return {"scope": scope, "ref": field["ref"], "label": field["label"], "required": field.get("required"),
            "description": field.get("description", ""), "description_truncated": field.get("description_truncated", False)}


def retained_school_catalog(proof, field, scope, state):
    if (not school_control(field) or not isinstance(proof, dict) or not isinstance(state, dict)
            or proof.get("binding") != selection_binding(field, scope)
            or state.get("expanded") != "false" or state.get("invalid")
            or proof.get("selected") != state.get("value")):
        return None
    options = proof.get("options")
    if not isinstance(options, list) or not 0 < len(options) <= 50:
        return None
    matches = [o for o in options if isinstance(o, dict) and o.get("label") == proof.get("choice")]
    detail = structured_school_option(matches[0]) if len(matches) == 1 else None
    if not detail or state["value"] not in {detail["name"], proof["choice"]}:
        return None
    if any(not isinstance(o, dict) or not isinstance(o.get("label"), str) or not 0 < len(o["label"]) <= 500 for o in options):
        return None
    return {"choices": [o["label"] for o in options], "choice_details": options, "selected_choice": proof["choice"]}


def education_question(field):
    """Identify these observed controls before any generic scalar fallback."""
    label, kind = normalize(field.get("label", "")), field.get("type")
    if label == "school name" and kind == "combobox" and field.get("ref") == _SCHOOL_REF:
        return "school"
    if (label == "degree" and kind == "radio" and str(field.get("ref", "")).startswith("ashby:")
            and (field.get("description") or field.get("description_truncated"))):
        return "degree"
    if label == "discipline/field of study" and kind == "text":
        return "major"
    if label == "graduation date or anticipated graduation date" and kind in {"text", "date"}:
        return "graduation"
    return None


def _current_context(field):
    return (normalize(field.get("description") or "") == _CURRENT_NOTE
            and field.get("description_truncated") is not True)


def school_control(field):
    return education_question(field) == "school" and _current_context(field)


def school_basis(answers, *, as_of=None):
    today = as_of or date.today()
    item = answers.get("standing.current_education_school", {})
    source = item.get("source")
    record = source.get("original_record", {}) if isinstance(source, dict) else {}
    if (not isinstance(record, dict) or item.get("status") != "verified" or not source or record.get("status") != "verified"
            or not record.get("source") or record.get("expected") is not True
            or source.get("expected") is not True or source.get("method") != "verified_expected_education_record"
            or not isinstance(item.get("value"), str) or not item["value"].strip()
            or item["value"] != record.get("school")):
        return None
    start, end = (_education_bound(record.get("start_date")),
                  _education_bound(record.get("end_date"), end=True))
    if not start or not end or not start <= today <= end:
        return None
    return item


def derive(field, answers, *, as_of=None):
    """Return one value/evidence pair, or leave an unresolved question alone."""
    column = education_question(field)
    if not column or not _current_context(field):
        return None
    current = school_basis(answers, as_of=as_of)
    if not current:
        return None
    record = current["source"]["original_record"]
    evidence = {"records": {"standing.current_education_school": current},
                "criterion": "One verified current degree; employer explicitly accepts study in progress",
                "expected": True}
    if column == "school":
        options = [o for o in field.get("options", [])
                   if isinstance(o, dict) and not o.get("disabled") and isinstance(o.get("label"), str)]
        choices = [o["label"] for o in options]
        matches = []
        for option in options:
            detail = structured_school_option(option)
            if "school_metadata" in option and detail is None:
                continue
            name = detail["name"] if detail else option["label"]
            if option_matches(name, current["value"], field_id="school--0"):
                matches.append(option)
        if len(matches) != 1:
            return None
        evidence.update(observed_choices=choices,
                        projection={"control_type": "combobox", "native_catalog_verification_required": True})
        if structured_school_option(matches[0]):
            evidence["projection"]["selected_native_option"] = matches[0]
        return {"query": current["value"], "choice": matches[0]["label"]}, evidence
    if column == "degree":
        value = record.get("degree")
        if not isinstance(value, str) or not value.strip():
            return None
        choices = [o["label"] for o in field.get("options", [])
                   if isinstance(o, dict) and not o.get("disabled") and isinstance(o.get("label"), str)]
        matches = [v for v in choices if option_matches(v, value)]
        if len(matches) != 1:
            return None
        evidence["observed_choices"] = choices
        return matches[0], evidence
    if column == "major":
        value = record.get("major")
        return (value, evidence) if isinstance(value, str) and value.strip() else None
    expected = answers.get("education.expected_graduation_date", {})
    if expected.get("status") != "verified" or not expected.get("source"):
        return None
    try:
        exact = date.fromisoformat(expected["value"])
    except (ValueError, TypeError, KeyError):
        return None
    lower, upper = (_education_bound(record.get("end_date")),
                    _education_bound(record.get("end_date"), end=True))
    if not lower or not upper or not lower <= exact <= upper:
        return None
    if field["type"] == "text" and field.get("calendar_format") != "MM/DD/YYYY":
        return None  # An unobserved text date format needs its own binding.
    evidence["records"]["education.expected_graduation_date"] = expected
    evidence["projection"] = {"control_type": field["type"], "calendar_format": field.get("calendar_format")}
    return exact.isoformat(), evidence
