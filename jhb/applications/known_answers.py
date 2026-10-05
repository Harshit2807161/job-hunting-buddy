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
    return (_label(field) == "location" and field.get("type") == "combobox"
            and field.get("ref") == "ashby:_systemfield_location:control:0")


def plain_contact_location(field):
    """Only the observed standard contact question has this standing mapping."""
    return (contact_location(field)
            and normalize(field.get("description") or "") in {"", "city, state, and country"}
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
    if (_label(field) == "state/country of residence" and field.get("type") == "combobox") or contact_location(field):
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
    if has_conditional_instruction(field) and field.get("type") in {"radio", "select", "combobox"}:
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
