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


def _signature(field):
    observation = {"ref": field.get("ref"), "label": _label(field), "type": field.get("type"),
                   "choices": _choices(field), "country_context": field.get("country_context")}
    return hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()


def key_for_field(field, answers):
    """Bind exact aliases; no fuzzy screening or citizenship assumptions."""
    label = _label(field)
    aliases = {
        "are you open to relocation?": "preferences.relocation",
        "i am authorized to work in the united states.": "eligibility.authorized_us",
        _RESTRICTION: "screening.non_compete",
    }
    key = aliases.get(label)
    if key and (item := _verified(answers, key)) and isinstance(item.get("value"), bool):
        return key
    if label == "state/country of residence" and field.get("type") in {"text", "combobox", "select"}:
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
    if label == _GRADUATE and field.get("type") in {"radio", "select", "combobox"}:
        result = _graduate(answers, today)
        if result:
            value, evidence = result
    elif label == _LINKS and field.get("type") in {"text", "textarea"}:
        records = {key: item for key in ("links.github", "links.scholar", "links.portfolio")
                   if (item := _verified(answers, key)) and isinstance(item.get("value"), str)
                   and re.fullmatch(r"https://[^\s]+", item["value"])}
        if records:
            value = "\n".join(dict.fromkeys(item["value"] for item in records.values()))
            evidence = {"records": records}
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
