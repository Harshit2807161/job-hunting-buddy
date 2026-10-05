"""Pure routing of unresolved controls; never invent answers or mark drafts complete."""
from __future__ import annotations

from . import booklet

CANDIDATE = "candidate"
KNOWN = "known_answer_fill"
DOCUMENT = "document_generation"


def route(book, record, context):
    """Distinguish candidate decisions from agent work using current verified facts.

    A populated but incompatible native catalog remains a candidate decision.
    An empty catalog cannot prove a stored fact is wrong: it needs native work.
    Employer instructions and public-guided responses retain their existing gates.
    """
    label = record.get("question", "")
    field = {"label": label, "ref": context.get("ref") or "", "type": context.get("type", "text"),
             "required": context.get("required") is True, "country_context": record.get("country_context"),
             "description": context.get("description", ""),
             "description_truncated": context.get("description_truncated") is True,
             "choices": context.get("choices", [])}
    answers = dict(book.get("answers", {}))
    role = context.get("selected_role")
    if role in {"sde", "ml"}:
        answers.update(booklet.for_role(book, role))
    documents = book.get("job_document_answers", {}).get(context.get("job_hash"), {})
    if role in {"sde", "ml"} and documents.get("role") == role and "documents.cover_letter" in documents:
        answers["documents.cover_letter"] = documents["documents.cover_letter"]
    answers.update({key: item for key, item in book.get("custom_answers", {}).items()
                    if item.get("scope") == record.get("scope")
                    and (not item.get("job_hash") or item["job_hash"] == context.get("job_hash"))
                    and (not item.get("job_hashes") or context.get("job_hash") in item["job_hashes"])})
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
    return field_route(field, answers)


def field_route(field, answers):
    """Runtime interface: classify an observed field against already scoped answers."""
    from .planner import key_for_field
    from .review_inventory import candidate_wording_requested
    label = field.get("label", "")
    # Incomplete instructions or an employer's own-wording requirement must
    # never be hidden behind a stored profile value or document task.
    if field.get("description_truncated"):
        return CANDIDATE
    own_wording = candidate_wording_requested(label + "\n" + str(field.get("description", "")))
    if (not own_wording and field["type"] == "file" and booklet.normalize(label) in
            {"cover letter", "upload cover letter", "attach cover letter", "portfolio or cover letter"}):
        document = answers.get("documents.cover_letter", {})
        return (KNOWN if document.get("status") == "verified" and document.get("source")
                and isinstance(document.get("value"), str) and document["value"].strip() else DOCUMENT)
    # Derivations are ephemeral and recomputed from these exact observations.
    # Never mutate the caller's catalog or persist derived facts here.
    from .known_answers import enrich
    scoped = dict(answers)
    observed = {**field, "options": field.get("options") or [
        {"label": choice} for choice in field.get("choices", []) if isinstance(choice, str)]}
    enrich(observed, {}, scoped)
    key = key_for_field(observed, scoped)
    item = scoped.get(key, {})
    if own_wording:
        from .review_inventory import candidate_response
        if not candidate_response(item):
            return CANDIDATE
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
