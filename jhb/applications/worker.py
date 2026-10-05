"""Observe → plan → validate → act → candidate review, bounded at every stage."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import nullcontext
import fcntl
import html
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .. import config, notify
from . import booklet, queue
from .browser import BrowserActions
from .credentials import CredentialStore
from .planner import CodexPlanner, completed_cs_degree_answer, deterministic_plan, key_for_field, validate_plan


# A pipeline-owned attempt may reclassify a pre-browser transport handoff after
# run_job returns. Defer its one immutable observation until that final packet.
# ContextVar keeps concurrent tasks separate, including asyncio.wait_for tasks.
_FEEDBACK_ATTEMPT = ContextVar("application_feedback_attempt", default=None)


def _record_attempt_feedback(job, result, attempt_token, packet):
    """Diagnostics must never change an application outcome or trigger a replay."""
    try:
        from .attempt_feedback import record_attempt
        record_attempt(job, result, attempt_token=attempt_token, stage="preparation",
                       packet_path=Path(packet).with_name("packet.json"))
    except Exception as exc:
        # Exception messages may contain private paths, answers or credentials.
        logging.getLogger(__name__).warning("Application attempt feedback unavailable (%s)", type(exc).__name__)


def failure_result(exc, actions=None, *, job=None):
    """Classify transport/mechanics separately from unknown answers, without secrets."""
    from .cli_browser import BrowserOperationError
    if (isinstance(exc, BrowserOperationError) and getattr(exc, "condition", None) == "browser_capacity"
            and getattr(exc, "mutation_started", None) is False):
        return {"state": "failed", "reason": "Waiting for browser tab capacity",
                "error_kind": "browser_capacity", "retryable": True, "mutation_started": False,
                "events": [{"event": "browser_capacity_wait"}], "filled": []}
    kind = type(exc).__name__
    retryable = isinstance(exc, (TimeoutError, ConnectionError, subprocess.TimeoutExpired, FileNotFoundError))
    if isinstance(exc, BrowserOperationError) and exc.retryable:
        kind, retryable = "browser_mechanics", True
    elif isinstance(exc, RuntimeError) and str(exc) in {
        "Browser Use CLI failed; run browser-use --doctor", "Browser Use CLI returned no structured result"
    }:
        kind, retryable = "browser_transport", True
    event = {"event": "technical_failure", "kind": kind}
    observed = getattr(actions, "last_failure", None)
    if observed:
        event.update({k: observed[k] for k in ("operation", "kind", "elapsed_seconds") if k in observed})
    result = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}",
              "error_kind": kind, "retryable": retryable, "events": [event], "filled": []}
    progress = getattr(actions, "_preparation_progress", None)
    if isinstance(progress, dict) and job is not None:
        from .boards import application_hash, job_identity
        if (isinstance(job.get("dedupe_hash"), str) and re.fullmatch(r"[a-f0-9]{64}", job["dedupe_hash"])
                and application_hash(job.get("url")) == job.get("dedupe_hash")
                and job.get("dedupe_hash") == progress.get("job_hash")
                and job_identity(job.get("url")) == job_identity(progress.get("url"))
                and callable(progress.get("snapshot"))):
            try:
                result = progress["snapshot"](result)
            except Exception as checkpoint_error:
                # A malformed checkpoint cannot mask the original failure or
                # turn it into a reviewable draft. Do not expose error text.
                result["events"].append({"event": "partial_progress_unavailable", "kind": type(checkpoint_error).__name__})
    return result


def _apply_phone_format(answers, policy):
    """Keep the national-number alternative only under the explicit user rule."""
    record = answers.get("identity.phone_national", {})
    if (policy.get("phone_format", {}).get("separate_country") != "national"
            or record.get("status") != "verified"
            or not isinstance(record.get("value"), str) or not record["value"].strip()):
        answers.pop("identity.phone_national", None)


def _verified_start_month(record):
    """Reduce a verified calendar availability to its original month, never guess."""
    from calendar import month_name
    from datetime import date, datetime
    value = record.get("value")
    if record.get("status") != "verified" or not record.get("source") or not isinstance(value, str):
        return None
    parsed = None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?", value):
        try:
            parsed = date.fromisoformat(value if len(value) == 10 else value+"-01")
        except ValueError:
            return None
    else:
        for fmt in ("%B %Y", "%b %Y"):
            try:
                parsed = datetime.strptime(value, fmt).date()
                break
            except ValueError:
                pass
    if parsed is None:
        return None
    return booklet.answer(f"{month_name[parsed.month]} {parsed.year}",
                          {"method": "verified_availability_month", "original_value": value,
                           "original_source": record["source"], "precision": "month"})


def _located_us_from_country(answers):
    """Residence is derived from the verified contact country, never nationality.

    A small explicit country vocabulary covers the supported US workflow and
    common verified alternatives. Unrecognized/ambiguous country strings do not
    become a new factual answer through a negative default.
    """
    country = answers.get("identity.country", {})
    if (country.get("status") != "verified" or not country.get("source")
            or not isinstance(country.get("value"), str)):
        return None
    value = booklet.normalize(country["value"])
    us = {"united states", "united states of america", "us", "usa", "u.s.", "u.s.a."}
    other = {"canada", "united kingdom", "uk", "india", "australia", "germany", "france", "ireland",
             "netherlands", "spain", "italy", "switzerland", "sweden", "norway", "denmark", "finland",
             "new zealand", "mexico", "brazil", "china", "japan", "south korea", "singapore",
             "united arab emirates", "israel", "south africa", "poland", "portugal", "pakistan", "bangladesh"}
    if value not in us | other:
        return None
    return booklet.answer(value in us, {"method": "verified_contact_country_residence",
        "derived_from": "identity.country", "original_country": country["value"],
        "original_source": country["source"], "criterion": "Contact country is the United States"})


def _observed_relocation_choice(field, answers):
    """Map willingness to its observed option without claiming current residence."""
    record = answers.get("preferences.relocation", {})
    if (booklet.normalize(field.get("label", "")) != "are you based in san francisco or open to relocating?"
            or record.get("status") != "verified" or record.get("value") is not True or not record.get("source")):
        return None
    choices = [option for option in field.get("options", []) if isinstance(option, dict)
               and booklet.normalize(option.get("label", "")) == "open to relocating" and not option.get("disabled")]
    if len(choices) != 1:
        return None
    return booklet.answer(choices[0]["label"], {"method": "verified_willingness_observed_choice",
        "derived_from": "preferences.relocation", "original_source": record["source"],
        "observed_question": field["label"], "observed_choice": choices[0]["label"]})


def _inapplicable_optional(field, answers):
    if field["required"]:
        return False
    conditions = {
        "if you select yes, please tell us more about the non-compete.": "screening.non_compete",
        "if you answered yes to the above, please indicate the name of your relative:": "screening.employee_relative",
    }
    label = booklet.normalize(field["label"])
    source_patterns = {
        'if you answered "yes" to the question above, please enter your dates of employment. note that your performance history may be reviewed as part of the application process.': r"have you ever been employed full-time at [^?]+\?",
        'if you answered "yes" to the question above, please enter your dates of contract engagement or work through agency.': r"have you ever provided any contract work for [^?]+\?",
    }
    if label in source_patterns:
        return any(item.get("status") == "verified" and item.get("value") is False
                   and re.fullmatch(source_patterns[label], booklet.normalize(item.get("question", "")))
                   for item in answers.values())
    record = answers.get(conditions.get(label), {})
    return record.get("status") == "verified" and record.get("value") is False


def _question(field, key, reason=""):
    return {"question": field["label"], "answer_key": key, "ref": field["ref"],
            "required": field["required"], "type": field["type"],
            "country_context": field.get("country_context"),
            "choices": [o["label"] for o in field.get("options", [])], "reason": reason,
            "description": field.get("description", "")[:4096] if isinstance(field.get("description", ""), str) else "",
            "description_truncated": field.get("description_truncated") is True or
                                     isinstance(field.get("description"), str) and len(field["description"]) > 4096}


def _scoped_custom_answers(book, job, scope):
    """Portal answers apply only to the application contexts the user saw."""
    return {key: item for key, item in book.get("custom_answers", {}).items()
            if scope is not None and item.get("scope") == scope
            and (not item.get("job_hash") or item["job_hash"] == job["dedupe_hash"])
            and (not item.get("job_hashes") or job["dedupe_hash"] in item["job_hashes"])}


def role_for_job(job):
    """Choose a resume variant; the independent fit check still decides suitability."""
    value = job.get("role_classes", "")
    classes = {str(item).strip() for item in (value if isinstance(value, list) else str(value).split(","))}
    if classes == {"swe"}: return "sde"
    if classes == {"ml"}: return "ml"
    # Discovery can tag ML platform engineers as both software and ML. An
    # explicit specialization in the job title resolves the document choice
    # without asking the candidate a question about information already known.
    # Generic AI-company names and description keywords do not establish it.
    if classes == {"swe", "ml"} and re.search(
        r"\b(?:machine[\s-]+learning|AI\s*/\s*ML|ML\s+(?:platform|engineer|scientist)|data\s+scientist|applied\s+scientist)\b",
        str(job.get("title", "")), re.I,
    ):
        return "ml"
    return None


async def prepare(page, job, answers, planner, vault, *, demo_origin=None, max_steps=8, cli_actions=None, narrative_preferences=None, document_runner=None):
    if not demo_origin and cli_actions is None:
        raise ValueError("Live preparation requires the Browser Use CLI")
    located = _located_us_from_country(answers)
    if located:
        answers["standing.located_us"] = located
    else:
        answers.pop("standing.located_us", None)
    actions = cli_actions or BrowserActions(page, demo_origin=demo_origin)
    # A client may be reused in tests or local tooling; never carry an older
    # job/run checkpoint into a failure before this preparation observes it.
    actions._preparation_progress = None
    observed_fields = {}
    document_tasks = {}
    generated_documents = {}
    narrative_calls = 0
    observed_step = 0
    def outcome(result, *, stable=False):
        from .review_inventory import build
        return {**result, "agent_tasks": [*document_tasks.values(), *result.get("agent_tasks", [])], "generated_documents": generated_documents,
                **build(list(observed_fields.values()), result.get("filled", []), answers,
                                 key_for_field, complete=stable, step_count=observed_step+1 if observed_fields else 0)}
    if not cli_actions:
        await actions.install()
    if not actions.allowed_url(job["url"]):
        return outcome({"state": "unsupported", "reason": "The application URL differs from this adapter's exact job scope", "events": [], "filled": []}), actions
    try:
        if cli_actions:
            await actions.open(job["url"])
        else:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    except Exception:
        if getattr(actions, "redirected_to", None):
            return outcome({"state": "unsupported", "reason": "Employer ATS redirect is outside v1 scope", "events": [], "filled": []}), actions
        raise
    profile_result = {}
    if cli_actions and hasattr(actions, "ensure_profile"):
        profile_result = await actions.ensure_profile(answers)
        if profile_result.get("handoff"):
            return outcome({"state": profile_result["handoff"], "reason": profile_result["reason"],
                    "events": [{"event": "record_editor_handoff"}], "filled": profile_result.get("filled", [])}), actions
    if not cli_actions:
        await page.wait_for_timeout(350)
    else:
        indexes = [int(match[1]) for key in answers
                   if (match := re.fullmatch(r"education\.(\d+)\.school", key)) and answers[key]["status"] == "verified"]
        if indexes:
            await actions.ensure_education(max(indexes)+1)
    progress = {"job_hash": job.get("dedupe_hash"), "url": job.get("url"), "operation": "observe"}
    async def observe():
        progress.update(operation="observe")
        progress.pop("field_ref", None); progress.pop("field_type", None)
        snapshot = await actions.observe()
        booklet.annotate_work_country(snapshot, job)
        for field in snapshot.get("fields", []):
            observed_fields[(field["ref"], field["label"], field["type"])] = {**field, "observed_step": observed_step}
        if cli_actions and hasattr(actions, "describe") and not snapshot.get("handoff"):
            from .native_question_context import enrich_async
            await enrich_async(snapshot, job, answers, actions.describe)
            for field in snapshot.get("fields", []):
                observed_fields[(field["ref"], field["label"], field["type"])] = {**field, "observed_step": observed_step}
        if document_runner and not snapshot.get("handoff"):
            from .cover_letter_runner import cover_field, document_available
            for field in snapshot.get("fields", []):
                if not cover_field(field) or field["ref"] in document_tasks:
                    continue
                scoped = answers.get(key_for_field(field, answers), {})
                if scoped.get("status") == "declined" or document_available(scoped):
                    continue
                record = answers.setdefault("documents.cover_letter", booklet.answer(source="Cover letter awaits skill-based generation"))
                if record.get("status") == "declined" or document_available(record):
                    continue
                if record.get("status") == "verified":
                    answers["documents.cover_letter"] = booklet.answer(source="Existing cover-letter document unavailable; regenerate from the preserved skill")
                prepared = await asyncio.to_thread(document_runner.generate, field)
                if prepared.get("state") == "verified":
                    answers["documents.cover_letter"] = prepared["record"]
                    generated_documents["documents.cover_letter"] = prepared["record"]
                elif prepared.get("state") == "agent_task":
                    document_tasks[field["ref"]] = {**_question(field, "documents.cover_letter"),
                        "task_kind": "document_generation", "reason_code": prepared["reason_code"],
                        "retryable": prepared.get("retryable") is True}
        return snapshot
    events = [{"event": "verified_saved_records"}] if profile_result.get("filled") else []
    filled = {(row["question"], row["ref"]): row for row in profile_result.get("filled", [])}
    def retain_progress(failure):
        # Only fills verified in THIS run enter current evidence. Never copy a
        # prior packet or reclassify a failed control as an answered question.
        context = {"operation": progress["operation"]}
        for key in ("field_ref", "field_type"):
            value = progress.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.,\[\]-]{1,200}", value):
                context[key] = value
        return outcome({**failure, "filled": list(filled.values()),
                        "events": [*events, *failure["events"]], "failure_context": context}, stable=False)
    progress["snapshot"] = retain_progress
    actions._preparation_progress = progress
    previous = None
    optional_questions = {}
    resolved_optional_refs = set()
    authenticated = False
    deadline = time.monotonic() + 480
    for step in range(max_steps):
        observed_step = step
        if time.monotonic() > deadline: break
        snapshot = await observe()
        events.append({"step": step, "event": "observed", "time": int(time.time())})
        if snapshot.get("handoff"):
            if snapshot["handoff"] == "waiting_login" and not authenticated:
                authenticated = True
                if await actions.authenticate(answers, vault):
                    events.append({"step": step, "event": "authenticated", "credential_storage": "local vault" if vault.demo_path else "OS keyring"})
                    continue
            return outcome({"state": snapshot["handoff"], "reason": snapshot["reason"], "events": events, "filled": list(filled.values())}), actions
        answers.pop("standing.relocation_choice", None)
        from .known_answers import enrich as enrich_known_answer
        for field in snapshot["fields"]:
            relocation = _observed_relocation_choice(field, answers)
            if relocation:
                answers["standing.relocation_choice"] = relocation
            enrich_known_answer(field, job, answers)
        if answers.get("standing.salary_policy", {}).get("value") is True:
            ranges = snapshot.get("salary_ranges") or job.get("advertised_salary_ranges", [])
            if len(ranges) == 1:
                bounds = ranges[0]
                answers["preferences.salary"] = booklet.answer((bounds["lower"]+bounds["upper"])/2,
                    {"rule": "Explicit user policy: arithmetic midpoint of advertised base range", "advertised_range": bounds})
            elif len(ranges) > 1:
                answers["preferences.salary"] = booklet.answer(source="Multiple advertised ranges require a location decision")
        if answers.get("standing.school_policy", {}).get("value") is True:
            schools = {re.sub(r"[^a-z0-9]", "", value.casefold()) for value in answers["standing.education_schools"]["value"]}
            if "universityofcaliforniasandiego" in schools:
                schools.add("ucsd")
            for field in snapshot["fields"]:
                match = re.fullmatch(r"are you currently attending or a recent graduate of (?:the )?(.+)\?", booklet.normalize(field["label"]))
                if match and schools:
                    school = re.sub(r"[^a-z0-9]", "", match[1])
                    answers["standing.school."+school] = booklet.answer(school in schools,
                        "Verified education records and explicit user instruction to reuse education history")
        from .narratives import proposal
        from .review_inventory import candidate_wording_requested
        narrative_job = {**job, "observed_application_questions": [f["label"] for f in snapshot["fields"]]}
        for field in snapshot["fields"]:
            if candidate_wording_requested(field["label"]+"\n"+str(field.get("description", ""))):
                # Preserve exact text and surface the candidate-only prompt,
                # including optional prompts, in the ledger and portal.
                continue
            # An explicit candidate reply outranks an earlier proposed narrative
            # if this catalog is reused while a draft is being corrected.
            explicit = {key: item for key, item in answers.items()
                if key.startswith("custom.") and item.get("status") in {"verified", "declined"} and item.get("user_override") is True
                and isinstance(item.get("source"), dict) and item["source"].get("provider") == "explicit user question response"
                and key_for_field(field, {key: item}) == key}
            if explicit:
                reordered = {**explicit, **{key: value for key, value in answers.items() if key not in explicit}}
                answers.clear(); answers.update(reordered)
            existing = answers.get(key_for_field(field, answers), {})
            from .grounded_narratives import curated_observation
            source = existing.get("source", {})
            if (isinstance(source, dict) and source.get("method") == "codex_curated_company_interest"
                    and source.get("observed_question") != curated_observation(field)):
                # This same-run proposal was reviewed against different help.
                # Candidate responses and other factual bindings stay intact.
                answers.pop(key_for_field(field, answers), None)
                existing = {}
            if existing.get("status") in {"verified", "declined"}:
                continue
            from .grounded_narratives import intent, curated_company_interest, company_interest_candidate
            curated = (os.environ.get("JHB_CURATED_COMPANY_INTEREST") == "1"
                       and company_interest_candidate(field, narrative_job.get("company")))
            if curated:
                if narrative_calls >= 3:
                    proposed = {"state": "agent_task", "reason_code": "narrative_call_budget"}
                else:
                    narrative_calls += 1
                    proposed = await asyncio.to_thread(curated_company_interest, field, narrative_job, answers,
                                                       preferences=narrative_preferences)
                if proposed.get("state") == "agent_task":
                    return outcome({"state": "failed", "reason": "Company-interest drafting or review needs an agent retry",
                        "error_kind": "narrative_generation", "retryable": True, "missing": [],
                        "filled": list(filled.values()), "events": events, "agent_tasks": [{
                            "question": field["label"], "ref": field["ref"], "required": field["required"],
                            "type": field["type"], "task_kind": "narrative_generation",
                            "reason_code": proposed.get("reason_code", "narrative_generation_unavailable"), "retryable": True}]}, stable=False), actions
                record = proposed.get("record") if proposed.get("state") == "proposed" else None
            else:
                record = proposal(field, narrative_job, answers)
            if not curated and not record and os.environ.get("JHB_GROUNDED_NARRATIVES") == "1" and narrative_calls < 3:
                from .grounded_narratives import draft, intent
                if intent(field, narrative_job.get("company")):
                    narrative_calls += 1
                    proposed = await asyncio.to_thread(draft, field, narrative_job, answers, preferences=narrative_preferences)
                    record = proposed.get("record") if proposed.get("state") == "proposed" else None
            if record:
                # Exact observed prompts and verified selected-role facts give
                # these qualitative answers provenance, without new screening
                # assumptions or a model round trip for known accomplishments.
                answers["custom.grounded." + field["ref"]] = {
                    **record, "question": field["label"], "field_ref": field["ref"]}
        fingerprint = json.dumps(snapshot, sort_keys=True)
        if fingerprint == previous:
            return outcome({"state": "unsupported", "reason": "Continue did not reveal a new supported step; check blocked draft-save requests", "events": events, "filled": list(filled.values())}), actions
        progress.update(operation="planning")
        progress.pop("field_ref", None); progress.pop("field_type", None)
        plan = validate_plan(await asyncio.to_thread(planner, snapshot, answers), snapshot, answers)
        bindings = {b["ref"]: b["answer_key"] for b in plan["bindings"]}
        missing = []
        for field in snapshot["fields"]:
            # Exact deterministic bindings remain usable if the planner omits one.
            key = bindings.get(field["ref"]) or key_for_field(field, answers)
            record = answers.get(key, {})
            if field["ref"] in document_tasks:
                # Preserve the document task separately; the candidate never
                # needs to supply generated text or a filesystem path.
                continue
            from .review_inventory import candidate_authored
            if (record.get("status") == "verified" and
                    candidate_wording_requested(field["label"]+"\n"+str(field.get("description", ""))) and not candidate_authored(record)):
                record = {}  # Employer guidance may live below a short label.
            if record.get("status") != "verified":
                if field["required"]:
                    missing.append(_question(field, key))
                elif record.get("status") != "declined" and not _inapplicable_optional(field, answers):
                    optional_questions[field["ref"]] = _question(field, key)
                else:
                    resolved_optional_refs.add(field["ref"])
                continue
            try:
                progress.update(operation="fill", field_ref=field["ref"], field_type=field["type"])
                await actions.fill(field, record["value"])
                education_row = re.fullmatch(r"(?:school|degree|discipline|start_date|end_date)--(\d+)", field["ref"])
                display_label = field["label"] + (f" (education record {int(education_row[1])+1})" if education_row else "")
                filled[(field["label"], field["ref"])] = {"question": display_label, "ref": field["ref"], "key": key, "value": record["value"], "source": record["source"],
                    **({"country_context": field["country_context"]} if field.get("country_context") else {}),
                    **({"proposed": True} if record.get("proposed") else {}),
                    **({"user_override": True} if record.get("user_override") is True else {})}
                events.append({"step": step, "event": "filled", "question": field["label"], "answer_key": key})
            except Exception as exc:
                filled.pop((field["label"], field["ref"]), None)
                if not isinstance(exc, ValueError):
                    raise
                from .cli_browser import BrowserOperationError
                if isinstance(exc, BrowserOperationError) and exc.retryable:
                    # A stale control or interrupted widget is a technical retry,
                    # never a new factual question for the candidate.
                    raise
                education = re.fullmatch(r"education\.(\d+)\.(school|major)", key or "")
                fallback_key = f"standing.catalog.{education[1]}.{education[2]}" if education else None
                fallback = answers.get(fallback_key, {})
                if str(exc) == "Stored answer is absent from dropdown options" and fallback.get("status") == "verified":
                    try:
                        await actions.fill(field, fallback["value"])
                    except ValueError:
                        pass
                    else:
                        filled[(field["label"], field["ref"])] = {"question": field["label"], "ref": field["ref"],
                            "key": fallback_key, "value": fallback["value"], "source": fallback["source"]}
                        events.append({"step": step, "event": "filled", "question": field["label"], "answer_key": fallback_key})
                        continue
                events.append({"step": step, "event": "unfilled", "question": field["label"], "answer_key": key})
                # A known compatible answer failing to stick is worker work.
                # Re-read the exact control's choices before distinguishing
                # a mechanical failure from genuinely different question scope.
                observed = dict(field)
                if hasattr(actions, "describe") and field["type"] in {"combobox", "select"}:
                    try:
                        described = await actions.describe(field)
                        choices = described.get("choices") if isinstance(described, dict) else None
                        if (not isinstance(choices, list) or not choices or described.get("truncated")
                                or any(not isinstance(value, str) or not value.strip() for value in choices)
                                or len(set(choices)) != len(choices)):
                            raise BrowserOperationError("Verified answer needs a complete native catalog", retryable=True)
                        observed["options"] = [{"label": value} for value in choices]
                    except (ValueError, RuntimeError) as inspection_error:
                        raise BrowserOperationError("Verified answer needs a native field inspection", retryable=True) from inspection_error
                from .question_routing import CANDIDATE, field_route
                if field_route(observed, answers) != CANDIDATE:
                    raise BrowserOperationError("Verified answer needs a field repair", retryable=True) from exc
                if field["required"]:
                    missing.append(_question(observed, key, "Observed choices need a more specific answer"))
                else:
                    optional_questions[field["ref"]] = _question(observed, key, "Observed choices need a more specific answer")
        if missing:
            for item in missing + list(optional_questions.values()):
                if hasattr(actions, "describe") and item["type"] in {"combobox", "select"}:
                    try:
                        choices = await actions.describe({"ref": item["ref"], "label": item["question"], "type": item["type"]})
                        item["choices"] = choices.get("choices", [])
                    except (ValueError, RuntimeError):
                        pass
            return outcome({"state": "waiting_input", "reason": "Required answers or documents need candidate input", "missing": missing,
                    "optional_questions": list(optional_questions.values()), "resolved_optional_refs": sorted(resolved_optional_refs), "events": events, "filled": list(filled.values())}), actions
        updated = await observe()
        if updated.get("handoff"):
            return outcome({"state": updated["handoff"], "reason": updated["reason"], "events": events, "filled": list(filled.values())}), actions
        if document_tasks:
            return outcome({"state": "failed", "reason": "Cover-letter generation or validation needs an agent task",
                "retryable": all(task.get("retryable") is True for task in document_tasks.values()),
                "error_kind": "document_generation", "missing": [],
                "events": events, "filled": list(filled.values()), "optional_questions": list(optional_questions.values())}), actions
        if {(f["ref"], f["label"], f["type"], f["required"], f.get("country_context"), f.get("separate_phone_country"), f.get("calendar_format"), f.get("description") or "", bool(f.get("description_truncated"))) for f in updated["fields"]} != {(f["ref"], f["label"], f["type"], f["required"], f.get("country_context"), f.get("separate_phone_country"), f.get("calendar_format"), f.get("description") or "", bool(f.get("description_truncated"))) for f in snapshot["fields"]}:
            events.append({"step": step, "event": "fields_revealed"})
            previous = None
            continue
        if plan["next_ref"] is None:
            terminal = any(re.fullmatch(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)",
                                       booklet.normalize(b["label"])) for b in snapshot["buttons"])
            return outcome({"state": "waiting_review" if terminal else "unsupported",
                    "reason": "Draft needs portal review and explicit approval, including every unanswered optional question" if terminal else "Unrecognized application controls; open the application form manually",
                    "events": events, "filled": list(filled.values()), "optional_questions": list(optional_questions.values()),
                    "resolved_optional_refs": sorted(resolved_optional_refs), "blocked_requests": actions.blocked_requests}, stable=terminal), actions
        button = next(b for b in snapshot["buttons"] if b["ref"] == plan["next_ref"])
        progress.update(operation="continue")
        progress.pop("field_ref", None); progress.pop("field_type", None)
        await actions.click_next(button)
        previous = fingerprint
        events.append({"step": step, "event": "continued", "button": button["label"]})
    return outcome({"state": "unsupported", "reason": "Step or time budget reached", "events": events, "filled": list(filled.values())}), actions


async def write_packet(page, directory: Path, job, result, *, cli_actions=None):
    from .application_discard import check
    job_hash = job.get("dedupe_hash")
    check(config.ROOT, job_hash)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if "review_inventory" not in result:
        from .review_inventory import build
        result.update(build([], result.get("filled", []), {}, key_for_field))
    from .capture import fresh
    from .boards import job_identity
    capture = await fresh(directory, job, page=page, cli_actions=cli_actions)
    captured = capture["verified"]
    check(config.ROOT, job_hash)
    if (not captured and result["state"] == "waiting_review"
            and (cli_actions is not None or page is not None or job_identity(job.get("url")))):
        result.update(state="failed", retryable=True, error_kind="browser_capture",
                      reason="Fresh review screenshot could not be verified; draft retained for technical retry")
        result.setdefault("events", []).append({"event": "review_capture_failed", "kind": capture.get("error_kind")})
    from .application_discard import action_lock
    with action_lock(config.ROOT, job_hash) if job_hash else nullcontext():
        check(config.ROOT, job_hash)
        created_at = int(time.time())
        capture["packet_created_at"] = created_at
        result["capture"] = capture
        data = {"job": job, **result, "submitted": False, "created_at": created_at}
        booklet.write_private(directory / "packet.json", data)
        booklet.write_private(directory / "events.json", result["events"])
        esc = lambda value: html.escape(str(value), quote=True)
        notes = "".join(f"<li>{esc(note)}</li>" for note in result.get("review_notes", []))
        rows = "".join(f'<tr><td>{esc(r["question"])}</td><td><pre>{esc(r["value"])}</pre></td><td>{esc(r["source"])}</td></tr>' for r in result.get("filled", []))
        missing = "".join(f'<li>Required: {esc(r["question"])}</li>' for r in result.get("missing", []))
        missing += "".join(f'<li>Optional unanswered question: {esc(r["question"])}</li>' for r in result.get("optional_questions", []))
        inventory = result["review_inventory"]
        inventory_rows = "".join(f'<tr><td>{esc(row["question"])}</td><td>{esc(row["status"])}</td><td>{esc(row["category"])}</td><td>{"Required" if row["required"] else "Optional"}{"; candidate’s own wording requested" if row["candidate_wording_required"] else ""}</td></tr>' for row in inventory["fields"])
        body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Application review</title>
    <style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px}}td,th{{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;max-width:550px}}img{{max-width:100%}}.state{{padding:14px;background:#eef4ff}}a{{color:#1463bc}}</style>
    <h1>{esc(job['title'])} · {esc(job['company'])}</h1><p class="state">{esc(result['state'])}: {esc(result['reason'])}</p>
    <p>Application has not been submitted. Review the answers and documents before taking over the browser.</p>
    <p><a href="{esc(job['url'])}">Original posting</a> · <a href="packet.json">Structured packet</a></p>
    <ul>{notes}{missing}</ul><table><tr><th>Question</th><th>Answer</th><th>Evidence</th></tr>{rows}</table>
    <h2>Every discovered application question</h2><p>Inventory {"verified after final observation" if inventory["complete"] else "incomplete; approval is blocked"}. Blank optional questions require explicit portal acknowledgment.</p>
    <table><tr><th>Question</th><th>Status</th><th>Category</th><th>Requirement</th></tr>{inventory_rows}</table>
    <h2>Browser at handoff</h2>{'<img src="browser.png" alt="Browser screenshot at handoff">' if captured else '<p>Browser screenshot unavailable.</p>'}
    </html>'''
        target = directory / "review.html"
        target.write_text(body, encoding="utf-8")
        target.chmod(0o600)
        return target


def notify_pending(conn, *, send_email=False):
    queue.initialize(conn)
    from .notices import deliver, initialize as initialize_notices
    initialize_notices(conn)
    # Preserve delivery history when upgrading an existing installation, before
    # a later worker rewrite can clear the older queue notification marker.
    conn.execute("INSERT OR IGNORE INTO application_notice_delivery(notice_key,category,delivered_at) "
                 "SELECT 'application-review:' || job_hash,'ready_reviews',notified_at FROM applications "
                 "WHERE state='waiting_review' AND notified_at IS NOT NULL")
    conn.commit()
    ready = {}
    for row in conn.execute("SELECT * FROM applications WHERE state NOT IN ('queued','running','submitted') AND notified_at IS NULL").fetchall():
        job = json.loads(row["job_json"])
        notification = {"job_hash": row["job_hash"], "state": row["state"], "packet": row["packet"], "submitted": False}
        path = config.ROOT / "private" / "notifications" / (row["job_hash"] + ".json")
        booklet.write_private(path, notification)
        # Operational failures, filters, authentication and field errors remain
        # local. Required questions have their own deduplicated aggregate email.
        if row["state"] == "waiting_review":
            ready["application-review:" + row["job_hash"]] = (row, job)
    if not send_email or not ready:
        return
    def send(keys):
        selected = [ready[key] for key in sorted(keys)]
        details = ["Applications are ready for your review. No application was submitted.",
                   "Open the local portal on this Mac, review every answer, and click Approve only when ready."]
        for row, job in selected:
            details += ["", f"{job['title']} · {job['company']}",
                        f"Review and approve: http://127.0.0.1:8030/#review/{row['job_hash']}",
                        f"Local review packet: {row['packet']}", f"Application ID: {row['job_hash']}"]
        return notify.send([job for row, job in selected], subject_prefix="[applications ready for review] ", details="\n".join(details))
    for key in deliver(conn, ready, "ready_reviews", send):
        row, _ = ready[key]
        conn.execute("UPDATE applications SET notified_at=? WHERE job_hash=? AND state='waiting_review'",
                     (int(time.time()), row["job_hash"]))
    conn.commit()


def _recorded_discovery(job):
    name = {"simplify": "Simplify", "linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
            "jobspy:linkedin": "LinkedIn", "jobspy:indeed": "Indeed", "jobspy:glassdoor": "Glassdoor"}.get(job.get("source"))
    if name is None:
        return None
    return {**booklet.answer(name, {"method": "recorded_phase1_discovery", "source": job["source"],
        "source_url": job.get("source_url", job["url"]), "source_job_hash": job.get("source_job_hash")}),
        "company_question": f"How did you hear about {job['company']}?"}


async def run_job(job, book, **kwargs):
    """Run one job with cooperative, exact-application cancellation."""
    from . import application_discard as cancellation
    job_hash = job["dedupe_hash"]
    live = not kwargs.get("demo_origin")
    worker_token = None
    try:
        if live:
            worker_token = cancellation.worker_started(config.ROOT, job)
        return await _run_job(job, book, **kwargs)
    except cancellation.ApplicationDiscarded:
        record = cancellation._read(cancellation._path(config.ROOT, "application-discards", job_hash)) or {}
        return {"state": "discarded", "reason": "Candidate discarded this application", "missing": [], "filled": [],
                "events": [{"event": "candidate_discarded"}]}, record.get("packet_path")
    finally:
        if live:
            if worker_token:
                cancellation.worker_stopped(config.ROOT, job_hash, worker_token)
            if cancellation.discarded(config.ROOT, job_hash):
                await asyncio.to_thread(cancellation.finalize, config.ROOT, job_hash)


async def _run_job(job, book, *, planner_name="codex", demo_origin=None, headless=False,
                  interactive=False, review_seconds=0, role=None, artifacts=None, book_path=None):
    owner_token = _FEEDBACK_ATTEMPT.get()
    attempt_token = owner_token or uuid.uuid4().hex
    choice = book.get("job_role_answers", {}).get(job["dedupe_hash"], {})
    explicit_role = choice.get("value") if choice.get("status") == "verified" and choice.get("value") in {"sde", "ml"} else None
    selected_role = role or explicit_role or role_for_job(job)
    job = {**job, "selected_role": selected_role}
    if not re.fullmatch(r"[a-f0-9]{64}", job["dedupe_hash"]): raise ValueError("Invalid job identity")
    directory = (artifacts or config.ROOT / "private" / "applications") / job["dedupe_hash"]
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    async def persist(page, directory, job, result, *, cli_actions=None):
        from .application_discard import check
        check(config.ROOT, job["dedupe_hash"])
        result["selected_role"] = selected_role
        packet = await write_packet(page, directory, job, result, cli_actions=cli_actions)
        if owner_token is None:
            _record_attempt_feedback(job, result, attempt_token, packet)
        return packet
    if booklet.job_excluded(book, job):
        result = {"state": "skipped", "reason": "Explicit user instruction excludes this exact job",
                  "events": [{"event": "explicit_job_exclusion"}], "filled": [], "missing": []}
        return result, await persist(None, directory, job, result)
    if not demo_origin:
        from ..eligibility import assess_job
        eligibility = await asyncio.to_thread(assess_job, job)
        booklet.write_private(directory / "eligibility.json", eligibility)
        if eligibility["state"] != "eligible":
            result = {"state": eligibility["state"], "reason": eligibility["reason"],
                      "eligibility": eligibility, "events": [{"event": "eligibility_handoff", "policy": eligibility["policy"]}],
                      "filled": [], "missing": []}
            return result, await persist(None, directory, job, result)
        if eligibility.get("description"):
            job = {**job, "verified_job_description": eligibility["description"]}
    if not selected_role:
        result = {"state": "waiting_input", "reason": "Ambiguous role; choose --role sde or --role ml",
                  "missing": [{"question": "Choose the SDE or ML resume variant"}], "events": [], "filled": []}
        return result, await persist(None, directory, job, result)
    if not demo_origin:
        from .role_fit import assess as assess_role_fit
        fit = await asyncio.to_thread(assess_role_fit, job, book, selected_role)
        booklet.write_private(directory / "role-fit.json", fit)
        if fit.get("state") != "eligible":
            result = {"state": fit.get("state", "unsupported"), "reason": fit["reason"], "role_fit": fit,
                      "events": [{"event": "role_fit_handoff"}], "filled": [], "missing": []}
            return result, await persist(None, directory, job, result)
    answers = booklet.for_role(book, selected_role)
    start_month = _verified_start_month(answers.get("preferences.start_date", {}))
    if start_month:
        answers["standing.start_month"] = start_month
    discovery = _recorded_discovery(job)
    if discovery:
        answers["standing.discovery_source"] = discovery
    completed_cs = completed_cs_degree_answer(book.get("education_records", []))
    if completed_cs is not None:
        answers["standing.completed_cs_degree"] = completed_cs
    address_keys = ["identity.address", "identity.city", "identity.state", "identity.postal_code", "identity.country"]
    if all(answers.get(key, {}).get("status") == "verified" and answers[key].get("value") for key in address_keys):
        answers["standing.mailing_address"] = booklet.answer(
            ", ".join(str(answers[key]["value"]) for key in address_keys),
            {"method": "verified_mailing_address_components", "sources": {key: answers[key]["source"] for key in address_keys}})
    policy = book.get("workflow_preferences", {})
    _apply_phone_format(answers, policy)
    if policy.get("salary_when_no_range"):
        answers["standing.salary_policy"] = booklet.answer(True, "Explicit user salary policy")
    if policy.get("school_attendance"):
        answers["standing.school_policy"] = booklet.answer(True, "Explicit user instruction to reuse education facts")
        answers["standing.education_schools"] = booklet.answer(
            [record["school"] for record in book.get("education_records", []) if record.get("status") == "verified"],
            "Verified original education records, independent of employer dropdown mappings")
    if policy.get("preferred_first_name", {}).get("required") == "use identity.first_name":
        answers["standing.required_preferred_name"] = answers["identity.first_name"]
    if policy.get("office_locations"):
        answers["standing.office_willingness"] = booklet.answer(True, "Explicit user standing willingness to work at office/HQ locations")
    if policy.get("career_fair_contact"):
        answers["standing.career_fair_contact"] = booklet.answer("N/A", "Explicit user standing rule: no invented career-fair contacts")
    if policy.get("relocation") and "preferences.application_city" in answers:
        location = answers["preferences.application_city"]
        if location["status"] == "verified":
            answers["standing.location_relocation"] = booklet.answer(
                f"I am currently based in {location['value']} and am open to relocating anywhere.",
                {"location_source": location["source"], "relocation": "Explicit user standing willingness to relocate anywhere"})
    compliance = policy.get("application_compliance", {})
    if compliance.get("value") is True:
        answers["standing.compliance"] = booklet.answer(True, compliance["source"])
        answers["standing.interview_expectations"] = booklet.answer("I agree to these expectations", compliance["source"])
        signature = policy.get("legal_signature", {})
        if signature.get("value"):
            answers["standing.legal_signature"] = booklet.answer(signature["value"], signature["source"])
    relationship = policy.get("employer_relationships", {})
    for column, key in [("previous_full_time_employment", "standing.previous_employment"), ("previous_contract_work", "standing.previous_contract")]:
        if relationship.get(column) is False:
            answers[key] = booklet.answer(False, relationship["source"])
    catalog = policy.get("education_catalog", {})
    for index, record in enumerate(book.get("education_records", [])):
        if record.get("status") == "verified" and record.get("degree", "").casefold().startswith("bachelor"):
            for column, rule in [("school", "school_if_absent"), ("major", "major_if_absent")]:
                if catalog.get(rule):
                    answers[f"standing.catalog.{index}.{column}"] = booklet.answer(catalog[rule],
                        {"policy": catalog["source"], "actual_value": record[column], "original_source": record["source"]})
    documents = book.get("job_document_answers", {}).get(job["dedupe_hash"], {})
    if documents.get("role") == selected_role and "documents.cover_letter" in documents:
        answers["documents.cover_letter"] = documents["documents.cover_letter"]
    identity = queue.greenhouse_identity(job["url"])
    if identity:
        for index, record in enumerate(book.get("education_records", [])):
            for mapping in record.get("form_mappings", []):
                if mapping.get("region") == identity[0] and mapping.get("board") == identity[1]:
                    answers[f"education.{index}.school"] = booklet.answer(mapping["school_option"],
                        {"actual_institution": record["school"], "resume_source": record["source"],
                         "form_mapping": mapping})
    from .questions import _scope as question_scope
    try:
        scope = question_scope(job) if not demo_origin else None
    except ValueError:
        scope = None
    answers.update(_scoped_custom_answers(book, job, scope))
    from .boards import adapter, board_type, preparation_supported
    board = board_type(job["url"])
    if not demo_origin and not preparation_supported(board):
        result = {"state": "unsupported", "reason": f"{board} has no reviewed preparation adapter; preserve the exact job for browser evaluation",
                  "missing": [], "events": [], "filled": [], "board": board}
        return result, await persist(None, directory, job, result)
    planner = CodexPlanner(directory, board=board if not demo_origin else "greenhouse") if planner_name == "codex" else deterministic_plan
    document_runner = None
    if not demo_origin:
        from .cover_letter_runner import CoverLetterRunner
        document_runner = CoverLetterRunner(job, book, selected_role, directory, book_path=book_path)
    if not demo_origin:
        from .cli_browser import BrowserUseCLI
        if board == "greenhouse":
            actions = BrowserUseCLI()
        elif board == "workday":
            from .workday import WorkdayCLI
            experience_count = len({int(match[1]) for key, record in answers.items()
                                    if (match := re.fullmatch(r"experience\.(\d+)\.title", key))
                                    and record.get("status") == "verified"})
            actions = WorkdayCLI(job["url"], experience_count=experience_count)
        else:
            from .manual_ats import ManualATSCLI
            actions = ManualATSCLI(job["url"], board=board)
        actions.job_hash = job["dedupe_hash"]
        try:
            vault = None
            if board == "workday":
                from .workday import approved_credential_store
                vault = approved_credential_store(job["url"], answers)
            result, actions = await prepare(None, job, answers, planner, vault, cli_actions=actions,
                                            narrative_preferences=policy.get("narrative_style", {}), document_runner=document_runner)
        except Exception as exc:
            result = failure_result(exc, actions, job=job)
        result["review_notes"] = book.get("job_review_notes", {}).get(job["dedupe_hash"], [])
        result["role_fit"] = fit
        result.update(board=board, planner_skill=adapter(board).get("skill"))
        # Keep the persistent browser and the unsaved draft open at handoff.
        packet = await persist(None, directory, job, result, cli_actions=actions)
        if interactive and result["state"] == "waiting_review":
            print(f"Review {packet}. Browser is guarded. No application has been submitted.")
            acknowledgement = await asyncio.to_thread(input, "Type TAKE OVER to continue manually, or Enter to keep the guard: ")
            if acknowledgement == "TAKE OVER":
                await actions.human_takeover(acknowledgement)
                # Do not run any browser actions after control passes to the human.
        return result, packet
    from playwright.async_api import async_playwright
    profile = config.ROOT / "private" / ("demo-browser" if demo_origin else "greenhouse-browser")
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    vault = CredentialStore(config.ROOT / "private" / "demo-vault.json" if demo_origin else None)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(profile), headless=headless,
                         viewport={"width": 1280, "height": 960}, service_workers="block")
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            try:
                result, actions = await prepare(page, job, answers, planner, vault, demo_origin=demo_origin,
                                                narrative_preferences=policy.get("narrative_style", {}))
            except Exception as exc:
                # Exceptions may contain DOM or private values; store only safe class metadata.
                result, actions = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}", "events": [], "filled": []}, None
            packet = await persist(page, directory, job, result)
            if interactive and actions:
                print(f"Review {packet}. Browser is guarded. No application has been submitted.")
                acknowledgement = await asyncio.to_thread(input, "Type TAKE OVER to review/continue manually, or Enter to close: ")
                if acknowledgement == "TAKE OVER":
                    await actions.human_takeover()
                    await asyncio.to_thread(input, "Browser is under your control. Press Enter here when finished: ")
            elif review_seconds:
                print(f"Browser handoff available for {review_seconds}s; packet: {packet}")
                # Bounded local handoff window; no model action occurs while waiting.
                await asyncio.sleep(min(review_seconds, 300))
            return result, packet
        finally:
            await context.close()


def drain_once(conn, book_path, **kwargs):
    path = config.ROOT / "private" / "application-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    with path.open("r+") as lane:
        try:
            fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        item = queue.claim(conn, lease_seconds=1800)
        if not item: return None
        try:
            observed_book = booklet.load(book_path)
            result, packet = asyncio.run(run_job(item["job"], observed_book, book_path=book_path, **kwargs))
            from .questions import collect, reconcile
            collect(item["job"], result, book_path, observed_book=observed_book)
            reconcile(item["job"], result, book_path)
            queue.finish(conn, item["job_hash"], result["state"], packet)
            return result
        except Exception:
            queue.finish(conn, item["job_hash"], "failed")
            raise
