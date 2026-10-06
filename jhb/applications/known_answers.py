"""Narrow observed-form mappings backed by verified facts and preferences.

Derived values live only in the preparation answer catalog. They are bound to
the exact observed question and options, rather than becoming candidate facts.
"""
from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .booklet import ALIASES, answer, normalize

_GRADUATE = ("are you currently pursuing or recently completed a graduate degree (ms or phd, or equivalent research experience) "
             "in physics, electrical engineering, computer engineering, computer science, applied math, or a related field?")
_AVAILABILITY = "if presented with an offer, when would be the earliest you would be available to start?"
_LINKS = "github, scholar, publications, or personal site you'd like to share?"
_DISCOVERY = "how did you hear about this job opportunity?"
_PROJECT_SHARE = "do you have a personal project you're proud of that you'd like to share?"
_DISCOVERY_OTHER_CHOICES = {"career fair", "google", "handshake", "linkedin", "word of mouth", "i'm a customer", "other"}
_RESTRICTION = ("are you currently subject to any agreement (such as a non-compete, non-solicitation, non-disclosure, "
                "or similar restriction) that could limit your ability to perform this role?")
_CALIFORNIA_NOTE = "note: if you are based in california, please mark n/a."
GOVERNMENT_CONFLICT_DESCRIPTION = ("do you currently, or have you in the last 5 years, worked for the us government "
    "(e.g., congressional staffer, member of the military, state, or federal agencies) and had oversight or similar "
    "responsibility over anduril’s business or other interests?")


def _label(field):
    return normalize(field.get("label", "")).replace("\u2011", "-").replace("\u2010", "-")


def _verified(answers, key):
    item = answers.get(key, {})
    return item if item.get("status") == "verified" and item.get("source") else None


def _choices(field):
    result = []
    for option in field.get("options", []):
        if isinstance(option, dict) and not option.get("disabled"):
            label = option.get("label")
        elif isinstance(option, str):
            label = option
        else:
            continue
        if isinstance(label, str) and label.strip():
            result.append(label)
    return result


def authorization_needs_specific_response(field):
    """Generic country authorization does not prove present/any-employer scope.

    Scope may appear in the selected option or owned help, not just the label.
    An explicit answer to that exact employer question can still be reused by
    the planner; this guard only prevents deriving it from a broader profile.
    """
    label = _label(field)
    authorization = (label in {"u.s. work authorization", "work authorization"} or re.search(
        r"\b(?:are you|i am)\s+(?:(?:currently|legally)\s+){0,2}(?:authorized|eligible)\s+to\s+work\b", label))
    context = "\n".join([label, str(field.get("description") or ""), *_choices(field)])
    return bool(authorization and re.search(
        r"\bcurrently\b|\bimmediate(?:ly)?\b|\b(?:any|every)\s+employer\b|\bunrestricted\b|\bright now\b|\bat present\b",
        context, re.I))


def contact_location(field):
    return (_label(field) in {"location", "home location", "where are you currently located?"} and field.get("type") == "combobox"
            and field.get("ref") == "ashby:_systemfield_location:control:0")


def plain_contact_location(field):
    """Only the observed standard contact question has this standing mapping."""
    hints = {"location": {"", "city, state, and country"},
             "where are you currently located?": {""},
             "home location": {"the city you currently live in. start typing and select from the list."}}
    return (contact_location(field)
            and normalize(field.get("description") or "") in hints.get(_label(field), set())
            and not field.get("description_truncated"))


def contact_location_basis(answers):
    city, state, country = (_verified(answers, key) for key in
                            ("preferences.application_city", "identity.state", "identity.country"))
    if not all(r and isinstance(r.get("value"), str) for r in (city, state, country)):
        return None
    parts = [p.strip() for p in city["value"].split(",")]
    from .cli_runtime import option_matches
    if (len(parts) != 2 or not parts[0] or not option_matches(parts[1], state["value"], field_id="state")
            or normalize(country["value"]) not in {"us", "usa", "united states", "united states of america"}):
        return None
    return parts[0], state, country, city


def _signature(field):
    observation = {"ref": field.get("ref"), "label": _label(field), "type": field.get("type"),
                   "choices": _choices(field), "country_context": field.get("country_context")}
    if _label(field) in PROFILE_QUESTIONS:
        observation["required"] = field.get("required")
    from .ashby_education import school_control
    if ((_label(field) == "state/country of residence" and field.get("type") == "combobox")
            or contact_location(field) or school_control(field)):
        # Search results are transient; a closed committed autocomplete emits
        # no options. Bind stable question metadata, then audit exact committed
        # value against the recorded native catalog choice and verified facts.
        observation["choices"] = []
    if field.get("description") or field.get("description_truncated"):
        observation.update(description=field.get("description"), description_truncated=bool(field.get("description_truncated")))
    return hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()


def key_for_field(field, answers):
    """Bind exact aliases; no fuzzy screening or citizenship assumptions."""
    if authorization_needs_specific_response(field):
        return None
    label = _label(field)
    aliases = {
        "are you open to relocation?": "preferences.relocation",
        "i am authorized to work in the united states.": "eligibility.authorized_us",
        _RESTRICTION: "screening.non_compete",
    }
    key = aliases.get(label) if not has_conditional_instruction(field) else None
    if key and (item := _verified(answers, key)) and isinstance(item.get("value"), bool):
        return key
    if (label in {"are you open to travel?", "are you willing to travel up to 20% of the time?",
                  "are you willing to travel 25%+ of the time on average?"}
            and field.get("type") in {"radio", "select", "combobox", "checkbox"}):
        travel = _verified(answers, "preferences.travel")
        if travel and isinstance(travel.get("value"), bool):
            return "preferences.travel"
    if label == "earliest residency start date?" and field.get("type") in {"text", "date"}:
        start = _verified(answers, "preferences.start_date")
        if start and isinstance(start.get("value"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start["value"]):
            try:
                date.fromisoformat(start["value"])
            except ValueError:
                pass
            else:
                return "preferences.start_date"
    if label == "state/country of residence" and field.get("type") in {"text", "select"}:
        # This is contact residence, never the country of the job or nationality.
        state = _verified(answers, "identity.state")
        if state and isinstance(state.get("value"), str) and state["value"].strip():
            return "identity.state"
    if (label == "i will now or in the future need assistance with a work visa."
            and normalize(field.get("country_context") or "") in {"united states", "us", "usa"}):
        item = _verified(answers, "eligibility.sponsorship")
        if item and isinstance(item.get("value"), bool):
            return "eligibility.sponsorship"
    signature = _signature(field)
    key = "standing.observed." + signature
    item = _verified(answers, key)
    if (item and isinstance(item.get("source"), dict)
            and item["source"].get("observation_sha256") == signature):
        if label in PROFILE_QUESTIONS:
            current = _profile_projection(field, answers)
            if (not current or current[0] != item.get("value")
                    or current[1]["records"] != item["source"].get("records")):
                return None  # A changed source record invalidates the old projection.
        return key
    return None


def has_conditional_instruction(field):
    """The restriction answer must account for its owned instruction first."""
    return (_label(field) == _RESTRICTION
            and bool(field.get("description") or field.get("description_truncated")))



def context_response_key(field, answers):
    """Only a newly explicit response may resolve an unfamiliar complete note.

    Employer scope is filtered by the worker before this catalog is built.
    Legacy generic answers lack a proof of the displayed owned instruction.
    """
    if not has_conditional_instruction(field) or field.get("description_truncated"):
        return None
    description = field.get("description")
    if not isinstance(description, str) or not description:
        return None
    if normalize(description).replace("note :", "note:") == _CALIFORNIA_NOTE and _california_residence(answers) is True:
        # A contradictory No cannot satisfy the explicit N/A instruction,
        # including when its required option is missing or ambiguous.
        return None
    from .review_inventory import candidate_response
    expected = {"owned_description_sha256": hashlib.sha256(description.encode()).hexdigest(),
                "owned_description_truncated": False, "field_ref": field.get("ref"),
                "country_context": normalize(str(field.get("country_context") or ""))}
    for key, record in answers.items():
        if (not key.startswith("custom.") or not candidate_response(record)
                or _label({"label": record.get("question", "")}) != _label(field)
                or (record.get("field_ref") and record["field_ref"] != field.get("ref"))
                or (record.get("country_context") and record["country_context"] != field.get("country_context"))):
            continue
        source = record["source"]
        if "public_question_metadata_proofs" in source:
            from .question_metadata import public_response_allowed
            if public_response_allowed(field, record):
                return key
            continue  # No fallback from mismatched public to generic/owned proof.
        proofs = source.get("owned_description_proofs", [])
        if not isinstance(proofs, list):
            proofs = []
        proofs = [source, *proofs]
        if any(isinstance(proof, dict) and proof.get("owned_description_sha256") == expected["owned_description_sha256"]
               and proof.get("owned_description_truncated") is False
               and proof.get("field_ref") == expected["field_ref"]
               and proof.get("country_context") == expected["country_context"] for proof in proofs):
            return key
    return None


def _california_residence(answers):
    state, country = _verified(answers, "identity.state"), _verified(answers, "identity.country")
    if (not state or not country or not isinstance(state.get("value"), str)
            or not isinstance(country.get("value"), str)):
        return None
    # Contact residence, never job-country annotation, nationality or inferred
    # location. Recognize standard US state codes; unknown strings stay unknown.
    if normalize(country["value"]) not in {"united states", "united states of america", "us", "usa", "u.s.", "u.s.a."}:
        return None
    value = normalize(state["value"])
    if value in {"ca", "california"}:
        return True
    other_codes = set("al ak az ar co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc".split())
    if value in other_codes:
        return False
    return None


def _bounds(value):
    """Bounds of an original date/month; a month does not invent a day."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = date.fromisoformat(value)
            return parsed, parsed
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
        else:
            parsed = None
            for fmt in ("%B %Y", "%b %Y"):
                try:
                    parsed = datetime.strptime(value, fmt).date()
                    break
                except ValueError:
                    pass
            if parsed is None:
                return None
            year, month = parsed.year, parsed.month
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except ValueError:
        return None


def _graduate(answers, today):
    for key in answers:
        match = re.fullmatch(r"education\.(\d+)\.degree", key)
        if not match:
            continue
        prefix = f"education.{match[1]}."
        records = {column: _verified(answers, prefix + column)
                   for column in ("degree", "major", "start_date", "end_date", "end_year")}
        if not all(records.values()):
            continue
        degree, major = records["degree"]["value"], records["major"]["value"]
        if (not isinstance(degree, str) or not isinstance(major, str)
                or normalize(major) != "computer science"
                or not re.fullmatch(r"master(?:['’]s)?(?: of [a-z ]+)?|m\.?s\.?|ph\.?d\.?|doctor of philosophy", normalize(degree))):
            continue
        expected = records["end_year"].get("source", {})
        if (not isinstance(expected, dict) or expected.get("expected") is not True
                or expected.get("original_date") != records["end_date"]["value"]):
            continue
        start, end = _bounds(records["start_date"]["value"]), _bounds(records["end_date"]["value"])
        if start and end and start[1] <= today < end[0]:
            return True, {"record_prefix": prefix, "records": records,
                          "criterion": "Verified current graduate Computer Science study; no claim of completion"}
    return None


# Country-relative questions need posting context, never the candidate's address.
RELATIVE_AUTHORIZATION = frozenset({
    "are you authorized to work in the country in which you are applying?",
    "are you authorized to work in the country where the job is located?",
    "are you legally authorized to work in the country where the job is located?",
    "are you legally authorized to work in the country in which this job is located?",
})
RELATIVE_SPONSORSHIP = frozenset({
    "do you now, or will you in the future, require sponsorship for employment in the country which you are applying?",
    "will you now or in the future require visa sponsorship for employment at whoop?",
    "will you now or in the future require sponsorship for employment visa status in this country?",
    "will you now or in the future require company sponsorship to retain or extend your work authorization in the country where the job is located?",
    "will you now or in the future require visa sponsorship to work in the country where this position is located?",
})
_GRADUATION_LABELS = frozenset({"what is your expected graduation date?",
    "what is your expected graduation month & year?", "what is your expected graduation month and year?"})
PROFILE_QUESTIONS = frozenset({
    "what is your current/most recent employer?",
    "please list the city and state/province that you are located in today.",
    "if you are not located in one of the above hubs, are you willing to relocate?",
    "please select your graduation month", "please select your graduation year",
    "please select your current or most recent university.",
    "have you had a previous work experience in software engineering?",
})
PROFILE_CATALOG_QUESTIONS = frozenset({"please select your graduation month",
    "please select your graduation year", "please select your current or most recent university."})
_UNIVERSITY = "please select your current or most recent university."
_UNIVERSITY_NOTES = {"", "if your university is not listed, please select ‘other.’",
                     "if your university is not listed, please select 'other.'",
                     'if your university is not listed, please select "other."'}
_START_LABELS = frozenset({"when are you available to start work?", "when can you start a new role?",
                         "how soon are you able to start a new role?",
                         "if offered a position, what is your ideal start-date?"})
INTERVIEW_RECORDING_DESCRIPTION = (
    "About Interview Recording\n\nTo keep your interview experience seamless and distraction-free, we use an AI Notetaker "
    "to record and transcribe our interviews. This helps interviewers focus fully on the conversation and ensures your "
    "responses are captured accurately. The recording will only be used internally for evaluation. If you would prefer "
    "not to be recorded, you can opt out on this page. Opting out will not impact your candidacy in any way.")
_DISCLOSURES = {
    "how would you describe your gender identity?": "disclosure.gender",
    "how would you describe your sexual orientation?": "disclosure.sexual_orientation",
    "sexual orientation": "disclosure.sexual_orientation",
    "do you identify as transgender?": "disclosure.transgender",
    "are you a veteran or active member of the united states armed forces?": "disclosure.veteran",
    "do you have a disability or chronic condition (physical, visual, auditory, cognitive, mental, emotional, or other) that substantially limits one or more of your major life activities, including mobility, communication (seeing, hearing, speaking), and learning?": "disclosure.disability",
    "race": "disclosure.race",
}


# Exact observed standard Ashby EEOC definitions, not instructions to infer identity.
EEOC_RACE_DESCRIPTION = 'Hispanic or Latino - A person of Cuban, Mexican, Puerto Rican, South or Central American, or other Spanish culture or origin regardless of race.\n\nWhite (Not Hispanic or Latino) - A person having origins in any of the original peoples of Europe, the Middle East, or North Africa.\n\nBlack or African American (Not Hispanic or Latino) - A person having origins in any of the Black racial groups of Africa.\n\nNative Hawaiian or Other Pacific Islander (Not Hispanic or Latino) - A person having origins in any of the peoples of Hawaii, Guam, Samoa, or other Pacific Islands.\n\nAsian (Not Hispanic or Latino) - A person having origins in any of the original peoples of the Far East, Southeast Asia, or the Indian Subcontinent, including, for example, Cambodia, China, India, Japan, Korea, Malaysia, Pakistan, the Philippine Islands, Thailand, and Vietnam.\n\nAmerican Indian or Alaska Native (Not Hispanic or Latino) - A person having origins in any of the original peoples of North and South America (including Central America), and who maintain tribal affiliation or community attachment.\n\nTwo or More Races (Not Hispanic or Latino) - All persons who identify with more than one of the above five races.'

def _disclosure_context(field):
    description = normalize(field.get("description") or "")
    return (not field.get("description_truncated") and (not description or (
        _label(field) == "race" and field.get("ref") == "ashby:_systemfield_eeoc_race"
        and description == normalize(EEOC_RACE_DESCRIPTION))))


def _prompt(field):
    return re.sub(r" \((?:mark all that apply|select one)\)$", "", _label(field))


def _office_willingness_question(label):
    """One bounded question family shared by native inspection and selection."""
    days = r"(?:[1-5]|one|two|three|four|five) days(?: per | a )week"
    schedule = r"(?:"+days+r"|on (?:mondays|tuesdays|wednesdays|thursdays|fridays)(?: and (?:mondays|tuesdays|wednesdays|thursdays|fridays))? \([1-5] days/week\))"
    return bool(re.fullmatch(r"are you (?:able|willing) to work (?:"+days+r" )?(?:from|in) our [a-z ,.-]+ office(?: "+schedule+r")?\?", label)
        or label == "are you able and willing to report to the office location listed in the job description, in a hybrid capacity?"
        or label == "are you open to a hybrid schedule with in-office days on monday, wednesday, and friday?"
        or label == "this is a hybrid role, working out of our boston, ma office 4 days per week. does this setup align to the working environment you are seeking in your next opportunity?"
        or label == "this position requires 4 days a week in office, including thursdays in our mountain view, ca headquarters and the remaining 3 days in either mountain view or our san francisco, ca office. are you able to meet this requirement?"
        or label == "i understand this is an in-person role in philadelphia, pa.")


def _project_share_basis(field, answers):
    """A verified resume project, never professional history or an invented story."""
    item = _verified(answers, "role.projects")
    text = item.get("value") if item else None
    if (_prompt(field) == _PROJECT_SHARE and not field.get("description")
            and not field.get("description_truncated") and isinstance(text, str)
            and re.search(r"(?m)^\S[^\n]+\|[^\n]+$", text)
            and re.search(r"(?m)^\s*•\s+\S", text)):
        return {"role.projects": item}
    return {}


def _profile_basis(field, answers):
    """Exact unqualified questions, bound to complete verified source records."""
    label, kind = _label(field), field.get("type")
    description = normalize(field.get("description") or "")
    permitted = _UNIVERSITY_NOTES if label == _UNIVERSITY else {""}
    if (label not in PROFILE_QUESTIONS or field.get("description_truncated")
            or description not in permitted):
        return {}
    if label in PROFILE_CATALOG_QUESTIONS:
        if kind not in {"combobox", "select"}:
            return {}
        from .ashby_education import school_basis
        current = school_basis(answers)
        if not current:
            return {}
        records = {"standing.current_education_school": current}
        if label != _UNIVERSITY:
            expected = _verified(answers, "education.expected_graduation_date")
            bounds = _bounds(expected.get("value")) if expected else None
            end = _bounds(current["source"]["original_record"].get("end_date"))
            if not bounds or not end or not end[0] <= bounds[0] <= bounds[1] <= end[1]:
                return {}
            # A year-only date cannot supply a graduation month.
            if label.endswith("month") and bounds[0].month != bounds[1].month:
                return {}
            records["education.expected_graduation_date"] = expected
        return records
    if label.startswith("please list the city"):
        item = _verified(answers, "preferences.application_city")
        return ({"preferences.application_city": item} if kind == "text" and item
                and isinstance(item.get("value"), str) and item["value"].strip() else {})
    if label.startswith("if you are not located"):
        item = _verified(answers, "preferences.relocation")
        return ({"preferences.relocation": item} if kind == "radio" and item
                and type(item.get("value")) is bool else {})
    if kind != ("text" if label.startswith("what is your current/") else "radio"):
        return {}
    records = {}
    indexes = sorted({m[1] for key in answers if (m := re.fullmatch(r"experience\.(\d+)\..+", key))})
    for index in indexes:
        row = {column: _verified(answers, f"experience.{index}.{column}")
               for column in ("company", "title", "start_date", "end_date", "current")}
        if (not all(row.values()) or any(item["source"] != row["company"]["source"] for item in row.values())
                or any(not isinstance(row[col]["value"], str) or not row[col]["value"].strip() for col in ("company", "title"))
                or type(row["current"]["value"]) is not bool):
            return {}
        start, end = _bounds(row["start_date"]["value"]), _bounds(row["end_date"]["value"])
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        if (not start or start[0] > today
                or row["current"]["value"] and row["end_date"]["value"] != ""
                or not row["current"]["value"] and (not end or end[1] >= today or end[0] < start[0])):
            return {}
        records.update({f"experience.{index}.{column}": item for column, item in row.items()})
    return records


def _profile_projection(field, answers):
    records = _profile_basis(field, answers)
    if not records:
        return None
    label, choices = _label(field), _choices(field)
    value = None
    if label.startswith("what is your current/"):
        indexes = {key.split(".")[1] for key in records}
        current = [i for i in indexes if records[f"experience.{i}.current"]["value"]]
        if len(current) > 1:
            return None
        if current:
            latest = current
        else:
            ends = {i: _bounds(records[f"experience.{i}.end_date"]["value"]) for i in indexes}
            newest = max(bound[1] for bound in ends.values())
            latest = [i for i, bound in ends.items() if bound[1] == newest]
        if len(latest) == 1:
            value = records[f"experience.{latest[0]}.company"]["value"]
    elif label.startswith("have you had a previous"):
        if any(key.endswith(".title") and re.search(r"\bsoftware (?:development )?(?:engineer|developer)\b", item["value"], re.I)
               for key, item in records.items()):
            matched = [choice for choice in choices if normalize(choice) == "yes"]
            value = matched[0] if len(matched) == 1 else None
        # Omission from a resume never establishes No to employment history.
    elif label.startswith("please list the city"):
        value = records["preferences.application_city"]["value"]
    elif label.startswith("if you are not located"):
        matched = [choice for choice in choices if normalize(choice) == ("yes" if records["preferences.relocation"]["value"] else "no")]
        value = matched[0] if len(matched) == 1 else None
    elif label == _UNIVERSITY:
        from .cli_runtime import option_matches
        school = records["standing.current_education_school"]["value"]
        matched = [choice for choice in choices if option_matches(choice, school, field_id="school--0")]
        value = matched[0] if len(matched) == 1 else None
        # An incomplete catalog cannot prove absence, so never infer Other.
    else:
        from calendar import month_name
        bound = _bounds(records["education.expected_graduation_date"]["value"])[0]
        desired = month_name[bound.month] if label.endswith("month") else str(bound.year)
        matched = [choice for choice in choices if normalize(choice) == normalize(desired)]
        value = matched[0] if len(matched) == 1 else None
    if (value is not None and field.get("type") == "combobox"
            and str(field.get("ref", "")).startswith("ashby:") and label in PROFILE_CATALOG_QUESTIONS):
        query = records["standing.current_education_school"]["value"] if label == _UNIVERSITY else value
        value = {"query": query, "choice": value}
    return ((value, {"records": records, "criterion": "Exact observed profile question projected from verified original records; no qualification or authorization expansion"})
            if value is not None else None)


def current_university_control(field):
    """Only this observed Ashby prompt permits a canonical school search."""
    return (str(field.get("ref", "")).startswith("ashby:") and field.get("type") == "combobox"
            and _label(field) == _UNIVERSITY and not field.get("description_truncated")
            and normalize(field.get("description") or "") in _UNIVERSITY_NOTES)


def current_university_query(field, answers):
    if current_university_control(field):
        record = _profile_basis(field, answers).get("standing.current_education_school")
        if record:
            return record["value"]
    return None


def catalog_basis(field, answers):
    """Complete verified prerequisites for inspecting a known native catalog.

    This never selects an option. Missing context cannot be hidden indefinitely
    as agent work when the actual answer derivation would reject the facts.
    """
    if field.get("description_truncated"):
        return {}
    label, records = _prompt(field), {}
    if label in PROFILE_CATALOG_QUESTIONS:
        return _profile_basis(field, answers)
    if plain_contact_location(field):
        basis = contact_location_basis(answers)
        return ({"preferences.application_city": basis[3], "identity.state": basis[1],
                 "identity.country": basis[2]} if basis else {})
    def require(key):
        item = _verified(answers, key)
        if item:
            records[key] = item
        return item
    if label in _GRADUATION_LABELS | _START_LABELS:
        key = "education.expected_graduation_date" if label in _GRADUATION_LABELS else "preferences.start_date"
        item = require(key)
        bounds = _bounds(item.get("value")) if item else None
        if not bounds:
            return {}
        if label in _GRADUATION_LABELS:
            current = require("standing.current_education_school")
            source = current.get("source", {}) if current else {}
            original = source.get("original_record", {}) if isinstance(source, dict) else {}
            end = _bounds(original.get("end_date"))
            if original.get("expected") is not True or not end or not (end[0] <= bounds[0] <= bounds[1] <= end[1]):
                return {}
    elif label in RELATIVE_AUTHORIZATION:
        if authorization_needs_specific_response(field):
            return {}
        suffix = {"united states": "us", "canada": "canada", "united kingdom": "uk"}.get(
            normalize(str(field.get("country_context") or "")))
        item = require("eligibility.authorized_" + suffix) if suffix else None
        if not item or type(item["value"]) is not bool:
            return {}
    elif (label in RELATIVE_SPONSORSHIP or label.replace("u.s.", "united states")
          == "will you now or in the future require visa sponsorship to work in the united states?"):
        country = normalize(str(field.get("country_context") or ""))
        explicit_us = label.replace("u.s.", "united states").endswith("in the united states?")
        if not (country == "united states" or explicit_us and country == ""):
            return {}
        item = require("eligibility.sponsorship")
        if not item or type(item["value"]) is not bool:
            return {}
        for part in ("now", "future"):
            require("eligibility.sponsorship_" + part)
    elif label in ALIASES["eligibility.sponsorship_future"]:
        # Future-only wording must use its own sourced fact, never infer it
        # from the combined now-or-future answer or present visa status.
        if (field.get("description") or normalize(str(field.get("country_context") or ""))
                not in {"", "united states"}):
            return {}
        item = require("eligibility.sponsorship_future")
        if not item or type(item["value"]) is not bool:
            return {}
    elif label in {"what is your current gpa", "what is your current gpa?"}:
        item = require("standing.current_education_gpa")
        match = re.fullmatch(r"(\d(?:\.\d+)?)\s*/\s*4(?:\.0+)?", str(item.get("value"))) if item else None
        if not match or not 0 <= float(match[1]) <= 4:
            return {}
    elif _office_willingness_question(label):
        office, relocation = require("standing.office_willingness"), require("preferences.relocation")
        if not office or office["value"] is not True or not relocation or relocation["value"] is not True:
            return {}
    elif label == _PROJECT_SHARE:
        return _project_share_basis(field, answers)
    elif (label in {_DISCOVERY, "how did you hear about this job?", "how did you hear about us?", "how did you hear about this role?"}
          or answers.get("standing.discovery_source", {}).get("company_question")
          and label == normalize(str(answers["standing.discovery_source"]["company_question"]))):
        source = require("standing.discovery_source")
        if not source or not isinstance(source["value"], str) or not source["value"].strip():
            return {}
    elif label in _DISCLOSURES:
        if not _disclosure_context(field):
            return {}
        key = _DISCLOSURES[label]
        item = require(key)
        if not item:
            return {}
        if key == "disclosure.veteran":
            service = require("screening.us_government_or_military_5y")
            if item["value"] is not False or not service or service["value"] is not False:
                return {}
        elif key == "disclosure.race":
            require("disclosure.hispanic")
    return records


def needs_catalog(field, answers):
    """A verified fact selects a bounded native inspection, not an answer."""
    return bool(catalog_basis(field, answers))


def _month_choice_bounds(text):
    """Parse only explicit month/year points and inclusive month ranges."""
    from calendar import month_abbr, month_name
    text = normalize(text).replace("\u2013", "-").replace("\u2014", "-")
    names = {name.casefold(): index for index in range(1, 13)
             for name in (month_name[index], month_abbr[index])}
    names["sept"] = 9
    month = "(" + "|".join(sorted(names, key=len, reverse=True)) + ")"
    match = re.fullmatch(month+r" (\d{4})", text)
    if match:
        year, start = int(match[2]), names[match[1]]
        try:
            return date(year, start, 1), date(year, start, monthrange(year, start)[1])
        except ValueError:
            return None
    match = re.fullmatch(month+r"(?: (\d{4}))?\s*[-/]\s*"+month+r" (\d{4})", text)
    if match:
        year1, year2 = int(match[2] or match[4]), int(match[4])
        try:
            first, last = date(year1, names[match[1]], 1), date(year2, names[match[3]], monthrange(year2, names[match[3]])[1])
        except ValueError:
            return None
        return (first, last) if first <= last else None
    return None


def _common_projection(field, job, answers):
    label, kind, choices = _prompt(field), field.get("type"), _choices(field)
    if field.get("description_truncated"):
        return None
    if label in PROFILE_QUESTIONS:
        return _profile_projection(field, answers)
    select = kind in {"radio", "combobox", "select", "multiselect"}
    def result(value, records, criterion):
        return value, {"records": records, "criterion": criterion}
    def chosen(labels, records, criterion):
        matches = [c for c in choices if normalize(c) in labels]
        if len(matches) == 1:
            return result(matches if kind == "multiselect" else matches[0], records, criterion)
        return None
    if label == "legal name" and kind == "text" and not field.get("description"):
        item = _verified(answers, "identity.full_name")
        if item and isinstance(item.get("value"), str) and item["value"].strip():
            return result(item["value"], {"identity.full_name": item}, "Verified full name for an unqualified legal-name field")
    if select and (projects := _project_share_basis(field, answers)):
        if sorted(map(normalize, choices)) == ["no", "yes"]:
            return chosen({"yes"}, projects, "Verified selected-resume project available to share; no invented project, link or personal story")
    if (label == "how did you hear about us?" and kind in {"combobox", "select", "radio"}
            and not field.get("description") and not field.get("options_truncated")
            and not field.get("choices_truncated")
            and len(choices) == len(_DISCOVERY_OTHER_CHOICES)
            and set(map(normalize, choices)) == _DISCOVERY_OTHER_CHOICES
            and answers.get("screening.referral", {}).get("status") != "verified"):
        # This complete observed catalog has no Job Board or Simplify choice.
        # An unknown/truncated catalog never establishes the Other category.
        from .boards import application_hash
        source = _verified(answers, "standing.discovery_source")
        proof = source.get("source", {}) if source else {}
        if (source and source.get("value") == "Simplify" and isinstance(proof, dict)
                and proof.get("method") == "recorded_phase1_discovery"
                and proof.get("source") == job.get("source") == "simplify"
                and proof.get("source_url") == job.get("source_url", job.get("url"))
                and job.get("dedupe_hash") and application_hash(job.get("url")) == job["dedupe_hash"]):
            return chosen({"other"}, {"standing.discovery_source": source},
                          "Exact job's recorded Simplify source is outside the complete observed discovery categories")
    if (label == "interview recording consent" and kind == "radio"
            and field.get("ref") == "ashby:_systemfield_recording_consent"
            and normalize(field.get("description") or "") == normalize(INTERVIEW_RECORDING_DESCRIPTION)
            and sorted(map(normalize, choices)) == ["opt out of recording", "yes, i consent to be recorded"]):
        item = _verified(answers, "standing.interview_recording")
        if item and type(item.get("value")) is bool:
            return chosen({"yes, i consent to be recorded" if item["value"] else "opt out of recording"},
                          {"standing.interview_recording": item},
                          "Explicit interview-recording preference for the observed internal recording/transcription consent")
    if label in _GRADUATION_LABELS | _START_LABELS:
        if label == "if offered a position, what is your ideal start-date?" and field.get("description"):
            return None
        key = "education.expected_graduation_date" if label in _GRADUATION_LABELS else "preferences.start_date"
        item = _verified(answers, key)
        bounds = _bounds(item.get("value")) if item else None
        if not bounds:
            return None
        records = {key: item}
        if label in _GRADUATION_LABELS:
            current = _verified(answers, "standing.current_education_school")
            source = current.get("source", {}) if current else {}
            original = source.get("original_record", {}) if isinstance(source, dict) else {}
            end = _bounds(original.get("end_date"))
            if original.get("expected") is not True or not end or not (end[0] <= bounds[0] <= bounds[1] <= end[1]):
                return None
            records["standing.current_education_school"] = current
        if kind in {"text", "date"}:
            return result(item["value"], records, "Exact verified availability/expected graduation; no earned-degree claim")
        if select:
            matches = [c for c in choices if (interval := _month_choice_bounds(c)) and interval[0] <= bounds[0] <= bounds[1] <= interval[1]]
            if len(matches) == 1:
                return result(matches if kind == "multiselect" else matches[0], records, "Unique observed explicit month range contains the complete verified date bounds")
        return None
    if label == "what degree and major are you pursuing?" and kind in {"text", "textarea"}:
        degree, major = (_verified(answers, key) for key in ("standing.current_education_degree", "standing.current_education_major"))
        if degree and major and all(isinstance(i["value"], str) for i in (degree, major)):
            return result(f"{degree['value']} in {major['value']}", {"standing.current_education_degree": degree,
                "standing.current_education_major": major}, "Original verified current degree and major, not employer catalog fallback")
    if label in {"what is your current gpa", "what is your current gpa?"}:
        item = _verified(answers, "standing.current_education_gpa")
        match = re.fullmatch(r"(\d(?:\.\d+)?)\s*/\s*4(?:\.0+)?", str(item.get("value"))) if item else None
        if match and 0 <= float(match[1]) <= 4:
            gpa = float(match[1])
            if kind in {"text", "number"}:
                return result(match[1], {"standing.current_education_gpa": item}, "Verified current GPA on an explicit 4.0 scale")
            matches = []
            for choice in choices if select else []:
                interval = re.fullmatch(r"(\d(?:\.\d+)?)\s*[-–]\s*(\d(?:\.\d+)?)", choice)
                if interval and 0 <= float(interval[1]) <= gpa <= float(interval[2]) <= 4:
                    matches.append(choice)
            if len(matches) == 1:
                return result(matches if kind == "multiselect" else matches[0], {"standing.current_education_gpa": item},
                              "Unique observed GPA interval; no conversion from a different grading scale")
        return None
    country = normalize(str(field.get("country_context") or ""))
    if label in RELATIVE_AUTHORIZATION and select:
        if authorization_needs_specific_response(field):
            return None
        suffix = {"united states": "us", "canada": "canada", "united kingdom": "uk"}.get(country)
        key = "eligibility.authorized_" + suffix if suffix else None
        item = _verified(answers, key) if key else None
        if item and type(item.get("value")) is bool:
            yes = item["value"]
            labels = {"yes" if yes else "no", "yes, i am currently legally authorized to work in the country where the jobs is located." if yes else
                      "no, i am not currently legally authorized to work in the country where the job is located."}
            return chosen(labels, {key: item}, "Verified authorization for the explicit posting country; no any-employer, visa, or citizenship claim")
    sponsor_label = label.replace("u.s.", "united states")
    if select and (label in RELATIVE_SPONSORSHIP or sponsor_label == "will you now or in the future require visa sponsorship to work in the united states?"):
        explicit_us = sponsor_label.endswith("in the united states?")
        if not (country == "united states" or explicit_us and country in {"", "united states"}):
            return None
        item = _verified(answers, "eligibility.sponsorship")
        if not item or type(item.get("value")) is not bool:
            return None
        records = {"eligibility.sponsorship": item}
        simple = chosen({"yes" if item["value"] else "no"}, records, "Verified combined present-or-future US sponsorship")
        if simple:
            return simple
        if item["value"] is False:
            return chosen({"no, i do not and will not require immigration sponsorship to legally work in the country where the job is located."}, records, "Explicit combined No covers both periods")
        now, future = (_verified(answers, "eligibility.sponsorship_"+part) for part in ("now", "future"))
        timing = "now" if now and now["value"] is True else "in the future" if future and future["value"] is True else None
        if timing:
            records.update({"eligibility.sponsorship_"+part: item for part, item in (("now", now), ("future", future)) if item})
            return chosen({f"yes, i will require immigration sponsorship {timing} to legally work in the country where the job is located."}, records, "Explicit need in the selected period; future-only wording makes no claim that present sponsorship is unnecessary")
        return None
    if select and label in _DISCLOSURES:
        key = _DISCLOSURES[label];item = _verified(answers, key)
        if not item or not _disclosure_context(field):
            return None  # Unfamiliar owned qualifications need their own binding.
        value = item["value"];records = {key: item}
        if key == "disclosure.gender":
            labels = {"male": {"male", "man"}, "female": {"female", "woman"}}.get(normalize(str(value)), {normalize(str(value))})
        elif key == "disclosure.sexual_orientation":
            labels = {"heterosexual", "straight", "heterosexual / straight", "heterosexual/straight"} if normalize(str(value)) in {"heterosexual", "straight", "heterosexual / straight", "heterosexual/straight"} else {normalize(str(value))}
        elif key == "disclosure.veteran":
            service = _verified(answers, "screening.us_government_or_military_5y")
            if value is not False or not service or service["value"] is not False:
                return None  # Veteran status alone does not exclude active service.
            records["screening.us_government_or_military_5y"] = service
            labels = {"no", "no, i am not a veteran or active member"}
        elif key == "disclosure.race":
            hispanic = _verified(answers, "disclosure.hispanic")
            labels = {normalize(str(value))}
            if hispanic and hispanic["value"] is False:
                records["disclosure.hispanic"] = hispanic
                labels.add(normalize(str(value))+" (not hispanic or latino)")
        elif type(value) is bool:
            labels = {"yes" if value else "no"}
        else:
            return None
        return chosen(labels, records, "Observed disclosure wording matched only to explicit verified self-identification")
    office = _verified(answers, "standing.office_willingness")
    relocation = _verified(answers, "preferences.relocation")
    if select and office and office["value"] is True and relocation and relocation["value"] is True:
        willingness = _office_willingness_question(label)
        if willingness and not field.get("description"):
            return chosen({"yes"}, {"standing.office_willingness": office, "preferences.relocation": relocation},
                          "Explicit office and relocation willingness; no assertion of current residence or immediate work eligibility")
        if (label == "please select all office locations of interest." and kind == "multiselect"
                and normalize(job.get("work_country") or "") == "united states"):
            allowed = {"san francisco", "mountain view", "seattle", "new york", "boston", "austin"}
            if choices and all(normalize(c) in allowed for c in choices):
                return result(list(choices), {"standing.office_willingness": office, "preferences.relocation": relocation}, "All actually offered US offices under explicit relocation willingness")
    return None


def enrich(field, job, answers, *, as_of=None):
    """Add one observed-choice derivation, preserving ambiguous factual answers."""
    today = as_of or datetime.now(ZoneInfo("America/Los_Angeles")).date()
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("Answer derivation requires a calendar date")
    # Every observed pass recomputes the derivation; an old result must not
    # survive a changed preference, calendar bound, or original record.
    answers.pop("standing.observed." + _signature(field), None)
    label, choices = _label(field), _choices(field)
    value, evidence = None, None
    from .ashby_education import derive as education_derivation
    education = education_derivation(field, answers, as_of=today)
    common = _common_projection(field, job, answers)
    if common:
        value, evidence = common
    elif education:
        value, evidence = education
    elif has_conditional_instruction(field) and field.get("type") in {"radio", "select", "combobox"}:
        description = field.get("description")
        note = normalize(description).replace("note :", "note:") if isinstance(description, str) else ""
        residence = _california_residence(answers)
        if note == _CALIFORNIA_NOTE and not field.get("description_truncated") and residence is not None:
            state, country = (_verified(answers, key) for key in ("identity.state", "identity.country"))
            records = {"identity.state": state, "identity.country": country}
            if residence:
                selected = [choice for choice in choices if normalize(choice) == "n/a"]
                if len(selected) == 1:
                    value, evidence = selected[0], {"records": records, "owned_instruction": description,
                        "criterion": "Observed instruction requires N/A for verified California residence"}
            elif (restriction := _verified(answers, "screening.non_compete")) and isinstance(restriction.get("value"), bool):
                records["screening.non_compete"] = restriction
                value, evidence = restriction["value"], {"records": records, "owned_instruction": description,
                    "criterion": "Verified US state code outside California; reuse explicit restriction answer"}
    elif (label == "u.s. work authorization" and field.get("type") in {"radio", "select", "combobox"}
          and normalize(field.get("description") or "") == "are you authorized to work in the united states?"
          and field.get("description_truncated") is False
          and normalize(str(field.get("country_context") or "")) in {"", "us", "usa", "united states"}):
        authorized = _verified(answers, "eligibility.authorized_us")
        if authorized and isinstance(authorized.get("value"), bool):
            value, evidence = authorized["value"], {"records": {"eligibility.authorized_us": authorized},
                "owned_question": field["description"],
                "criterion": "Exact owned US authorization question; no citizenship, export-control, or sponsorship inference"}
    elif (label == "conflict of interest" and field.get("type") in {"radio", "select", "combobox"}
          and normalize(field.get("description") or "") == GOVERNMENT_CONFLICT_DESCRIPTION
          and field.get("description_truncated") is False):
        government = _verified(answers, "screening.us_government_or_military_5y")
        if (government and government.get("value") is False
                and sorted(normalize(choice) for choice in choices) == ["no", "yes"]):
            value, evidence = False, {"records": {"screening.us_government_or_military_5y": government},
                "owned_question": field["description"],
                "criterion": "Verified No to any US government or military work within five years makes this narrower conjunction false; no employment, prior-application, or general conflict inference"}
    elif label == _GRADUATE and field.get("type") in {"radio", "select", "combobox"}:
        result = _graduate(answers, today)
        if result:
            value, evidence = result
    elif plain_contact_location(field):
        basis = contact_location_basis(answers)
        if basis:
            city, state, country, original = basis
            from .cli_runtime import option_matches
            selected = []
            for choice in choices:
                parts = [p.strip() for p in choice.split(",")]
                if (len(parts) == 3 and normalize(parts[0]) == normalize(city)
                        and option_matches(parts[1], state["value"], field_id="state")
                        and normalize(parts[2]) in {"united states", "united states of america", "usa", "us"}):
                    selected.append(choice)
            if len(selected) == 1:
                value = {"query": city, "choice": selected[0]}
                evidence = {"records": {"preferences.application_city": original,
                            "identity.state": state, "identity.country": country},
                            "criterion": "Verified application city matched to one observed city/state/country catalog choice; mailing city unchanged",
                            "observed_choices": choices,
                            "projection": {"control_type": "combobox", "native_catalog_verification_required": True}}
    elif label == "state/country of residence" and field.get("type") == "combobox":
        state, country = (_verified(answers, key) for key in ("identity.state", "identity.country"))
        if state and isinstance(state.get("value"), str) and state["value"].strip():
            from .cli_runtime import option_matches
            selected = [choice for choice in choices if option_matches(choice, state["value"], field_id="state")]
            if country and isinstance(country.get("value"), str):
                canonical_country = "United States" if normalize(country["value"]) in {"us", "usa", "united states", "united states of america"} else country["value"]
                selected += [choice for choice in choices if ", " in choice
                             and option_matches(choice.rsplit(", ", 1)[0], state["value"], field_id="state")
                             and normalize(choice.rsplit(", ", 1)[1]) == normalize(canonical_country)]
            if len(selected) == 1:
                value = {"query": state["value"], "choice": selected[0]}
                evidence = {"records": {"identity.state": state, **({"identity.country": country} if country else {})},
                            "criterion": "Verified contact residence matched to an actually observed native query catalog; fill requires real catalog commit",
                            "observed_choices": choices,
                            "projection": {"control_type": "combobox", "native_catalog_verification_required": True}}
    elif label == _LINKS and field.get("type") in {"text", "textarea"}:
        records = {key: item for key in ("links.github", "links.scholar", "links.portfolio")
                   if (item := _verified(answers, key)) and isinstance(item.get("value"), str)
                   and re.fullmatch(r"https://[^\s]+", item["value"])}
        if records:
            urls = list(dict.fromkeys(item["value"] for item in records.values()))
            separator = "\n" if field["type"] == "textarea" else " "
            value = separator.join(urls)
            evidence = {"records": records, "projection": {"control_type": field["type"],
                                                          "separator": separator, "urls": urls}}
    elif label == "how many months in a row can you commit to?" and field.get("type") == "multiselect":
        commitment = _verified(answers, "preferences.residency_commitment")
        def approved_range(text):
            return isinstance(text, str) and re.fullmatch(r"10\s*[-\u2011\u2013\u2014]\s*12\s+months", normalize(text))
        if commitment and approved_range(commitment.get("value")):
            selected = [choice for choice in choices if approved_range(choice)]
            if len(selected) == 1:
                value, evidence = selected, {"records": {"preferences.residency_commitment": commitment},
                                            "criterion": "Explicit approved residency commitment, matched to observed range"}
    elif label == _DISCOVERY and field.get("type") in {"multiselect", "radio", "select", "combobox"}:
        source = _verified(answers, "standing.discovery_source")
        if source and isinstance(source.get("value"), str):
            name = normalize(source["value"])
            selected = []
            if name in {"simplify", "simplify.jobs", "indeed", "glassdoor"}:
                selected = [choice for choice in choices if re.fullmatch(r"job board(?: \([^()]+\))?", normalize(choice))]
            elif name == "linkedin":
                selected = [choice for choice in choices if normalize(choice) == "linkedin"]
            if len(selected) == 1:
                value = selected if field["type"] == "multiselect" else selected[0]
                evidence = {"records": {"standing.discovery_source": source}, "category": "Recorded discovery source"}
    elif label == "office location" and field.get("type") == "multiselect":
        relocation = _verified(answers, "preferences.relocation")
        office = _verified(answers, "standing.office_willingness")
        if (relocation and relocation.get("value") is True and office and office.get("value") is True
                and normalize(job.get("work_country") or "") in {"united states", "us", "usa"}):
            # Observed US options only. A willingness to relocate never proves
            # UK work authorization or chooses an unsupported country.
            us_locations = {"austin, texas", "new york, new york", "san francisco, california",
                            "phoenix, arizona", "seattle, washington"}
            selected = [choice for choice in choices if normalize(choice) in us_locations]
            if selected:
                value, evidence = selected, {"records": {"preferences.relocation": relocation,
                    "standing.office_willingness": office}, "work_country": job["work_country"]}
    elif label == _AVAILABILITY and field.get("type") in {"radio", "select", "combobox"}:
        start = _verified(answers, "preferences.start_date")
        bounds = _bounds(start.get("value")) if start else None
        next_year, next_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        one_month = date(next_year, next_month, min(today.day, monthrange(next_year, next_month)[1]))
        selected = [choice for choice in choices if normalize(choice) == "one month +"]
        if bounds and bounds[0] > one_month and len(selected) == 1:
            value, evidence = selected[0], {"records": {"preferences.start_date": start},
                "criterion": "Earliest bound of original availability exceeds one calendar month from assessment",
                "availability_lower_bound": bounds[0].isoformat(), "one_month_bound": one_month.isoformat()}
    if evidence is None:
        return None
    signature = _signature(field)
    key = "standing.observed." + signature
    answers[key] = answer(value, {"method": "verified_observed_form_derivation", "observation_sha256": signature,
                                 "as_of": today.isoformat(), **evidence})
    return key
