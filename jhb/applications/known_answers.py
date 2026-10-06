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

from .booklet import answer, normalize

_GRADUATE = ("are you currently pursuing or recently completed a graduate degree (ms or phd, or equivalent research experience) "
             "in physics, electrical engineering, computer engineering, computer science, applied math, or a related field?")
_AVAILABILITY = "if presented with an offer, when would be the earliest you would be available to start?"
_LINKS = "github, scholar, publications, or personal site you'd like to share?"
_DISCOVERY = "how did you hear about this job opportunity?"
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
    "are you authorized to work in the country where the job is located?",
    "are you legally authorized to work in the country where the job is located?",
    "are you legally authorized to work in the country in which this job is located?",
})
RELATIVE_SPONSORSHIP = frozenset({
    "will you now or in the future require sponsorship for employment visa status in this country?",
    "will you now or in the future require company sponsorship to retain or extend your work authorization in the country where the job is located?",
})
_GRADUATION_LABELS = frozenset({"what is your expected graduation date?",
    "what is your expected graduation month & year?", "what is your expected graduation month and year?"})
_START_LABELS = frozenset({"when are you available to start work?", "when can you start a new role?",
                         "how soon are you able to start a new role?"})
_DISCLOSURES = {
    "how would you describe your gender identity?": "disclosure.gender",
    "how would you describe your sexual orientation?": "disclosure.sexual_orientation",
    "sexual orientation": "disclosure.sexual_orientation",
    "do you identify as transgender?": "disclosure.transgender",
    "are you a veteran or active member of the united states armed forces?": "disclosure.veteran",
    "do you have a disability or chronic condition (physical, visual, auditory, cognitive, mental, emotional, or other) that substantially limits one or more of your major life activities, including mobility, communication (seeing, hearing, speaking), and learning?": "disclosure.disability",
    "race": "disclosure.race",
}


def _prompt(field):
    return re.sub(r" \((?:mark all that apply|select one)\)$", "", _label(field))


def needs_catalog(field, answers):
    """A verified fact selects a bounded native inspection, not an answer."""
    label = _prompt(field)
    keys = []
    if label in _GRADUATION_LABELS:
        keys = ["education.expected_graduation_date"]
    elif label in _START_LABELS:
        keys = ["preferences.start_date"]
    elif label in RELATIVE_AUTHORIZATION:
        keys = ["eligibility.authorized_us", "eligibility.authorized_canada", "eligibility.authorized_uk"]
    elif label in RELATIVE_SPONSORSHIP:
        keys = ["eligibility.sponsorship"]
    elif label in {"what is your current gpa", "what is your current gpa?"}:
        keys = ["standing.current_education_gpa"]
    elif label in _DISCLOSURES:
        keys = [_DISCLOSURES[label]]
    return any(_verified(answers, key) for key in keys)


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
    select = kind in {"radio", "combobox", "select", "multiselect"}
    def result(value, records, criterion):
        return value, {"records": records, "criterion": criterion}
    def chosen(labels, records, criterion):
        matches = [c for c in choices if normalize(c) in labels]
        if len(matches) == 1:
            return result(matches if kind == "multiselect" else matches[0], records, criterion)
        return None
    if label in _GRADUATION_LABELS | _START_LABELS:
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
        timing = "now" if now and now["value"] is True else "in the future" if now and now["value"] is False and future and future["value"] is True else None
        if timing:
            records.update({"eligibility.sponsorship_"+part: item for part, item in (("now", now), ("future", future)) if item})
            return chosen({f"yes, i will require immigration sponsorship {timing} to legally work in the country where the job is located."}, records, "Separate verified timing facts; combined True alone cannot distinguish now from future")
        return None
    if select and label in _DISCLOSURES:
        key = _DISCLOSURES[label];item = _verified(answers, key)
        if not item or field.get("description"):
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
        days = r"(?:[1-5]|one|two|three|four|five) days(?: per | a )week"
        schedule = r"(?:"+days+r"|on (?:mondays|tuesdays|wednesdays|thursdays|fridays)(?: and (?:mondays|tuesdays|wednesdays|thursdays|fridays))? \([1-5] days/week\))"
        willingness = (re.fullmatch(r"are you (?:able|willing) to work (?:"+days+r" )?(?:from|in) our [a-z ,.-]+ office(?: "+schedule+r")?\?", label)
            or label == "are you able and willing to report to the office location listed in the job description, in a hybrid capacity?")
        if willingness:
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
