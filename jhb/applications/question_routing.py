"""Pure routing of unresolved controls; never invent answers or mark drafts complete."""
from __future__ import annotations

import re

from . import booklet

CANDIDATE = "candidate"
KNOWN = "known_answer_fill"
DOCUMENT = "document_generation"


def _shared_education(book):
    """Legacy contexts can route common facts without selecting a resume.

    Only indexed education values present and verified in every saved role are
    eligible. Role-specific documents, projects and skills remain unselected.
    """
    roles = book.get("roles", {})
    if not isinstance(roles, dict) or not all(isinstance(roles.get(role), dict) for role in ("sde", "ml")):
        return {}
    try:
        catalogs = [booklet.for_role(book, role) for role in ("sde", "ml")]
    except (KeyError, TypeError, ValueError):
        return {}  # Incomplete legacy profile metadata cannot establish facts.
    shared = {}
    for key, first in catalogs[0].items():
        if not re.fullmatch(r"education\.\d+\.(?:school|degree|major|gpa|start_date|end_date|start_year|end_year|start_month|end_month)", key):
            continue
        records = [catalog.get(key, {}) for catalog in catalogs]
        if all(record.get("status") == "verified" and record.get("source")
               and record.get("value") == first.get("value") and record.get("value") not in (None, "")
               for record in records):
            shared[key] = first
    return shared


def catalog(book, record, context, *, job=None):
    """Use the same verified catalog for handoff routing and native preparation."""
    try:
        answers = booklet.common_answers(book, job=job)
    except (KeyError, ValueError, TypeError):
        answers = dict(book.get("answers", {}))
    role = context.get("selected_role")
    if role in {"sde", "ml"}:
        answers.update(booklet.for_role(book, role, job=job) if job else booklet.for_role(book, role))
    else:
        answers = {key: item for key, item in answers.items() if not re.match(r"education\.\d+\.", key)}
        for key, item in _shared_education(book).items():
            answers.setdefault(key, item)
    documents = book.get("job_document_answers", {}).get(context.get("job_hash"), {})
    if role in {"sde", "ml"} and documents.get("role") == role and "documents.cover_letter" in documents:
        answers["documents.cover_letter"] = documents["documents.cover_letter"]
    answers.update({key: item for key, item in book.get("custom_answers", {}).items()
                    if item.get("scope") == record.get("scope")
                    and (not item.get("job_hash") or item["job_hash"] == context.get("job_hash"))
                    and (not item.get("job_hashes") or context.get("job_hash") in item["job_hashes"])})
    return answers


def observed_field(record, context, *, job=None):
    field = {"label": record.get("question", ""), "ref": context.get("ref") or "",
             "type": context.get("type", "text"), "required": context.get("required") is True,
             "country_context": record.get("country_context"),
             "description": context.get("description", ""),
             "description_truncated": context.get("description_truncated") is True,
             "choices": context.get("choices", [])}
    if job and not field["country_context"]:
        booklet.annotate_work_country({"fields": [field]}, job)
    return field


def route(book, record, context, *, job=None):
    """Distinguish candidate decisions from agent work using current verified facts.

    A populated but incompatible native catalog remains a candidate decision.
    An empty catalog cannot prove a stored fact is wrong: it needs native work.
    Employer instructions and public-guided responses retain their existing gates.
    """
    label = record.get("question", "")
    field = observed_field(record, context, job=job)
    answers = catalog(book, record, context, job=job)
    # Public text can route an already-known exact Yes/No fact to the agent
    # awaiting native verification. It cannot supply a native binding or answer.
    metadata = context.get("public_question_metadata", {})
    if not field["description"] and isinstance(metadata, dict):
        from .review_inventory import candidate_wording_requested
        if (metadata.get("source") == "official_public_question_metadata"
                and metadata.get("field_ref") == field["ref"]
                and candidate_wording_requested(str(metadata.get("description", "")))):
            return CANDIDATE  # Published own-wording guidance cannot become generator work.
        from .question_metadata import TYPES
        authorized = answers.get("eligibility.authorized_us", {})
        if (metadata.get("source") == "official_public_question_metadata"
                and metadata.get("field_ref") == field["ref"]
                and booklet.normalize(metadata.get("label", "")) == booklet.normalize(label)
                and field["type"] in TYPES.get(metadata.get("type"), set())
                and metadata.get("required") is field["required"]
                and booklet.normalize(label) == "u.s. work authorization"
                and booklet.normalize(metadata.get("description", "")) == "are you authorized to work in the united states?"
                and not field["description_truncated"]
                and booklet.normalize(str(field["country_context"] or "")) in {"", "us", "usa", "united states"}
                and sorted(metadata.get("choices", [])) == ["No", "Yes"]
                and authorized.get("status") == "verified" and authorized.get("source")
                and isinstance(authorized.get("value"), bool)):
            return KNOWN
        # This is routing only: public help may identify existing agent work,
        # while filling still requires the fresh visible owned description.
        from .known_answers import GOVERNMENT_CONFLICT_DESCRIPTION
        government = answers.get("screening.us_government_or_military_5y", {})
        if (metadata.get("source") == "official_public_question_metadata"
                and metadata.get("field_ref") == field["ref"]
                and booklet.normalize(metadata.get("label", "")) == booklet.normalize(label) == "conflict of interest"
                and field["type"] in TYPES.get(metadata.get("type"), set())
                and metadata.get("required") is field["required"]
                and booklet.normalize(metadata.get("description", "")) == GOVERNMENT_CONFLICT_DESCRIPTION
                and not field["description_truncated"]
                and sorted(metadata.get("choices", [])) == ["No", "Yes"]
                and government.get("status") == "verified" and government.get("source")
                and government.get("value") is False):
            return KNOWN
    return field_route(field, answers, job=job)


def field_route(field, answers, *, job=None):
    """Runtime interface: classify an observed field against already scoped answers."""
    from .planner import key_for_field
    from .review_inventory import candidate_wording_requested
    label = field.get("label", "")
    # Incomplete instructions or an employer's own-wording requirement must
    # never be hidden behind a stored profile value or document task.
    if field.get("description_truncated"):
        return CANDIDATE
    own_wording = candidate_wording_requested(label + "\n" + str(field.get("description", "")))
    from .cover_letter_runner import cover_field
    if not own_wording and cover_field(field):
        # Exact scoped user choices precede reusable role documents, just as
        # they do in the preparer. An optional explicit blank is settled;
        # making that document required needs a new candidate decision.
        document = answers.get(key_for_field(field, answers), {})
        if document.get("status") == "declined":
            return CANDIDATE if field.get("required") else KNOWN
        return (KNOWN if document.get("status") == "verified" and document.get("source")
                and isinstance(document.get("value"), str) and document["value"].strip() else DOCUMENT)
    # Derivations are ephemeral and recomputed from these exact observations.
    # Never mutate the caller's catalog or persist derived facts here.
    from .known_answers import enrich
    scoped = dict(answers)
    observed = {**field, "options": field.get("options") or [
        {"label": choice} for choice in field.get("choices", []) if isinstance(choice, str)]}
    enrich(observed, job or {}, scoped)
    key = key_for_field(observed, scoped)
    item = scoped.get(key, {})
    if own_wording:
        from .review_inventory import candidate_response
        if not candidate_response(item):
            return CANDIDATE
    if not own_wording and field.get("type") in {"combobox", "select", "radio", "multiselect"} and not observed["options"]:
        from .known_answers import needs_catalog
        if needs_catalog(observed, scoped):
            return KNOWN  # Inspect the native catalog before asking for a known fact again.
    if item.get("status") not in {"verified", "declined"} or not item.get("source"):
        return CANDIDATE
    if item.get("status") == "declined":
        return KNOWN if not field.get("required") else CANDIDATE
    if item.get("value") is None or item.get("value") == "":
        return CANDIDATE
    choices = field.get("choices", [])
    if field.get("options"):
        choices = [option.get("label") for option in field["options"]
                   if isinstance(option, dict) and not option.get("disabled") and option.get("value") != ""]
    if field["type"] in {"combobox", "select", "radio", "multiselect"} and choices:
        from .cli_runtime import option_matches
        values = item["value"] if isinstance(item["value"], list) else [item["value"]]
        if not all(sum(option_matches(choice, value, field_id=field["ref"], field_label=label)
                       for choice in choices if isinstance(choice, str)) == 1 for value in values):
            return CANDIDATE
    return KNOWN


def candidate_contexts(book, record):
    return {key: context for key, context in record.get("contexts", {}).items()
            if not context.get("resolved") and route(book, record, context) == CANDIDATE}


def agent_contexts(book, record):
    return {key: context for key, context in record.get("contexts", {}).items()
            if not context.get("resolved") and route(book, record, context) != CANDIDATE
            and (record.get("status") == "pending" or context.get("routing") in {KNOWN, DOCUMENT})}


def reconcile(bookpath=booklet.DEFAULT_PATH, *, max_contexts=1000):
    """Explicit bounded maintenance: annotate pending contexts without resuming jobs.

    Preserve answers, ledger status, history, native metadata and review authority.
    The caller schedules agent work separately; this is not a fill/approval action.
    """
    from . import questions
    with questions._locked(bookpath):
        book = booklet.load(bookpath)
        changed, tasks, examined = 0, [], 0
        for record in book.get("question_handoffs", {}).values():
            if record.get("status") != "pending":
                continue
            for job_hash, context in record.get("contexts", {}).items():
                if context.get("resolved"):
                    continue
                examined += 1
                if examined > max_contexts:
                    raise ValueError("Pending context limit exceeded; no maintenance was saved")
                routing = route(book, record, context)
                if context.get("routing", CANDIDATE) != routing:
                    stamp = questions._now()
                    context["routing"] = routing
                    record["updated_at"] = stamp
                    record.setdefault("history", []).append({"event": "routing_changed", "at": stamp,
                        "job_hash": job_hash, "routing": routing})
                    changed += 1
                if routing != CANDIDATE:
                    tasks.append({"job_hash": job_hash, "question_id": record["id"], "ref": context.get("ref"),
                                  "question": record["question"], "required": context.get("required") is True,
                                  "task_kind": routing, "answer_key": context.get("answer_key"), "type": context.get("type", "text")})
        if changed:
            booklet.write_private(bookpath, book)
        return {"changed_contexts": changed, "agent_tasks": tasks}
