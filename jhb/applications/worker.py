"""Observe → plan → validate → act → candidate review, bounded at every stage."""
from __future__ import annotations

import asyncio
import fcntl
import html
import json
import os
import re
import time
from pathlib import Path

from .. import config, notify
from . import booklet, queue
from .browser import BrowserActions
from .credentials import CredentialStore
from .planner import CodexPlanner, deterministic_plan, key_for_field, validate_plan


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
            "choices": [o["label"] for o in field.get("options", [])], "reason": reason}


def role_for_job(job):
    classes = set(str(job.get("role_classes", "")).split(","))
    if classes == {"swe"}: return "sde"
    if classes == {"ml"}: return "ml"
    return None


async def prepare(page, job, answers, planner, vault, *, demo_origin=None, max_steps=8, cli_actions=None):
    if not demo_origin and cli_actions is None:
        raise ValueError("Live preparation requires the Browser Use CLI")
    actions = cli_actions or BrowserActions(page, demo_origin=demo_origin)
    if not cli_actions:
        await actions.install()
    if not actions.allowed_url(job["url"]):
        return {"state": "unsupported", "reason": "Only Greenhouse-hosted forms are supported", "events": [], "filled": []}, actions
    try:
        if cli_actions:
            await actions.open(job["url"])
        else:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    except Exception:
        if getattr(actions, "redirected_to", None):
            return {"state": "unsupported", "reason": "Employer ATS redirect is outside v1 scope", "events": [], "filled": []}, actions
        raise
    if not cli_actions:
        await page.wait_for_timeout(350)
    else:
        indexes = [int(match[1]) for key in answers
                   if (match := re.fullmatch(r"education\.(\d+)\.school", key)) and answers[key]["status"] == "verified"]
        if indexes:
            await actions.ensure_education(max(indexes)+1)
    async def observe():
        snapshot = await actions.observe()
        if job.get("work_country"):
            for field in snapshot.get("fields", []):
                if booklet.normalize(field["label"]) in {"are you authorized to work lawfully in the location posted for this position?", "work authorization"}:
                    field["country_context"] = booklet.normalize(job["work_country"])
        return snapshot
    events, filled, previous = [], {}, None
    optional_questions = {}
    resolved_optional_refs = set()
    authenticated = False
    deadline = time.monotonic() + 480
    for step in range(max_steps):
        if time.monotonic() > deadline: break
        snapshot = await observe()
        events.append({"step": step, "event": "observed", "time": int(time.time())})
        if snapshot.get("handoff"):
            if snapshot["handoff"] == "waiting_login" and not authenticated:
                authenticated = True
                if await actions.authenticate(answers, vault):
                    events.append({"step": step, "event": "authenticated", "credential_storage": "local vault" if vault.demo_path else "OS keyring"})
                    continue
            return {"state": snapshot["handoff"], "reason": snapshot["reason"], "events": events, "filled": list(filled.values())}, actions
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
        fingerprint = json.dumps(snapshot, sort_keys=True)
        if fingerprint == previous:
            return {"state": "unsupported", "reason": "Continue did not reveal a new supported step; check blocked draft-save requests", "events": events, "filled": list(filled.values())}, actions
        plan = validate_plan(await asyncio.to_thread(planner, snapshot, answers), snapshot, answers)
        bindings = {b["ref"]: b["answer_key"] for b in plan["bindings"]}
        missing = []
        for field in snapshot["fields"]:
            # Exact deterministic bindings remain usable if the planner omits one.
            key = bindings.get(field["ref"]) or key_for_field(field, answers)
            record = answers.get(key, {})
            if record.get("status") != "verified":
                if field["required"]:
                    missing.append(_question(field, key))
                elif record.get("status") != "declined" and not _inapplicable_optional(field, answers):
                    optional_questions[field["ref"]] = _question(field, key)
                else:
                    resolved_optional_refs.add(field["ref"])
                continue
            try:
                await actions.fill(field, record["value"])
                education_row = re.fullmatch(r"(?:school|degree|discipline|start_date|end_date)--(\d+)", field["ref"])
                display_label = field["label"] + (f" (education record {int(education_row[1])+1})" if education_row else "")
                filled[(field["label"], field["ref"])] = {"question": display_label, "ref": field["ref"], "key": key, "value": record["value"], "source": record["source"]}
                events.append({"step": step, "event": "filled", "question": field["label"], "answer_key": key})
            except ValueError as exc:
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
                if field["required"]:
                    missing.append(_question(field, key, "Stored answer unavailable or incompatible with field"))
                else:
                    optional_questions[field["ref"]] = _question(field, key, "Stored answer unavailable or incompatible with field")
        if missing:
            for item in missing + list(optional_questions.values()):
                if hasattr(actions, "describe") and item["type"] in {"combobox", "select"}:
                    try:
                        choices = await actions.describe({"ref": item["ref"], "label": item["question"], "type": item["type"]})
                        item["choices"] = choices.get("choices", [])
                    except (ValueError, RuntimeError):
                        pass
            return {"state": "waiting_input", "reason": "Required answers or documents need candidate input", "missing": missing,
                    "optional_questions": list(optional_questions.values()), "resolved_optional_refs": sorted(resolved_optional_refs), "events": events, "filled": list(filled.values())}, actions
        updated = await observe()
        if updated.get("handoff"):
            return {"state": updated["handoff"], "reason": updated["reason"], "events": events, "filled": list(filled.values())}, actions
        if {(f["ref"], f["label"], f["type"], f["required"], f.get("country_context")) for f in updated["fields"]} != {(f["ref"], f["label"], f["type"], f["required"], f.get("country_context")) for f in snapshot["fields"]}:
            events.append({"step": step, "event": "fields_revealed"})
            previous = None
            continue
        if plan["next_ref"] is None:
            terminal = any(re.fullmatch(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)",
                                       booklet.normalize(b["label"])) for b in snapshot["buttons"])
            return {"state": "waiting_review" if terminal else "unsupported",
                    "reason": "Final submission is ready for candidate review" if terminal else "Unrecognized application controls; open the application form manually",
                    "events": events, "filled": list(filled.values()), "optional_questions": list(optional_questions.values()),
                    "resolved_optional_refs": sorted(resolved_optional_refs), "blocked_requests": actions.blocked_requests}, actions
        button = next(b for b in snapshot["buttons"] if b["ref"] == plan["next_ref"])
        await actions.click_next(button)
        previous = fingerprint
        events.append({"step": step, "event": "continued", "button": button["label"]})
    return {"state": "unsupported", "reason": "Step or time budget reached", "events": events, "filled": list(filled.values())}, actions


async def write_packet(page, directory: Path, job, result, *, cli_actions=None):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    data = {"job": job, **result, "submitted": False, "created_at": int(time.time())}
    booklet.write_private(directory / "packet.json", data)
    booklet.write_private(directory / "events.json", result["events"])
    captured = False
    if cli_actions and cli_actions.target_id:
        try:
            await cli_actions.screenshot(directory / "browser.png")
            captured = True
        except (RuntimeError, ValueError):
            pass  # A disconnected browser must not suppress the failure packet.
    elif page is not None:
        await page.screenshot(path=str(directory / "browser.png"), full_page=True)
        captured = True
    if captured:
        (directory / "browser.png").chmod(0o600)
    esc = lambda value: html.escape(str(value), quote=True)
    notes = "".join(f"<li>{esc(note)}</li>" for note in result.get("review_notes", []))
    rows = "".join(f'<tr><td>{esc(r["question"])}</td><td><pre>{esc(r["value"])}</pre></td><td>{esc(r["source"])}</td></tr>' for r in result.get("filled", []))
    missing = "".join(f'<li>Required: {esc(r["question"])}</li>' for r in result.get("missing", []))
    missing += "".join(f'<li>Optional unanswered question: {esc(r["question"])}</li>' for r in result.get("optional_questions", []))
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Application review</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px}}td,th{{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;max-width:550px}}img{{max-width:100%}}.state{{padding:14px;background:#eef4ff}}a{{color:#1463bc}}</style>
<h1>{esc(job['title'])} · {esc(job['company'])}</h1><p class="state">{esc(result['state'])}: {esc(result['reason'])}</p>
<p>Application has not been submitted. Review the answers and documents before taking over the browser.</p>
<p><a href="{esc(job['url'])}">Original posting</a> · <a href="packet.json">Structured packet</a></p>
<ul>{notes}{missing}</ul><table><tr><th>Question</th><th>Answer</th><th>Evidence</th></tr>{rows}</table>
<h2>Browser at handoff</h2>{'<img src="browser.png" alt="Browser screenshot at handoff">' if captured else '<p>Browser screenshot unavailable.</p>'}
</html>'''
    target = directory / "review.html"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o600)
    return target


def notify_pending(conn, *, send_email=False):
    queue.initialize(conn)
    for row in conn.execute("SELECT * FROM applications WHERE state NOT IN ('queued','running','submitted') AND notified_at IS NULL").fetchall():
        job = json.loads(row["job_json"])
        notification = {"job_hash": row["job_hash"], "state": row["state"], "packet": row["packet"], "submitted": False}
        path = config.ROOT / "private" / "notifications" / (row["job_hash"] + ".json")
        booklet.write_private(path, notification)
        if send_email:
            details = f"Application status: {row['state']}\nLocal review packet: {row['packet']}\n" \
                      f"Application ID: {row['job_hash']}\nNo application was submitted.\n" \
                      "Open the local packet and use jhb-apply review <ID> to refill and take over."
            if not notify.send([job], subject_prefix=f"[application {row['state']}] ", details=details):
                continue
        if not send_email:
            continue
        conn.execute("UPDATE applications SET notified_at=? WHERE job_hash=?", (int(time.time()), row["job_hash"]))
        conn.commit()


async def run_job(job, book, *, planner_name="codex", demo_origin=None, headless=False,
                  interactive=False, review_seconds=0, role=None, artifacts=None):
    choice = book.get("job_role_answers", {}).get(job["dedupe_hash"], {})
    explicit_role = choice.get("value") if choice.get("status") == "verified" and choice.get("value") in {"sde", "ml"} else None
    selected_role = role or explicit_role or role_for_job(job)
    if not re.fullmatch(r"[a-f0-9]{64}", job["dedupe_hash"]): raise ValueError("Invalid job identity")
    directory = (artifacts or config.ROOT / "private" / "applications") / job["dedupe_hash"]
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if not selected_role:
        result = {"state": "waiting_input", "reason": "Ambiguous role; choose --role sde or --role ml",
                  "missing": [{"question": "Choose the SDE or ML resume variant"}], "events": [], "filled": []}
        return result, await write_packet(None, directory, job, result)
    answers = booklet.for_role(book, selected_role)
    policy = book.get("workflow_preferences", {})
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
    answers.update({key: item for key, item in book.get("custom_answers", {}).items()
                    if identity and item.get("scope") == {"region": identity[0], "board": identity[1]}
                    and (not item.get("job_hash") or item["job_hash"] == job["dedupe_hash"])})
    planner = CodexPlanner(directory) if planner_name == "codex" else deterministic_plan
    if not demo_origin:
        from .cli_browser import BrowserUseCLI
        actions = BrowserUseCLI()
        try:
            result, actions = await prepare(None, job, answers, planner, None, cli_actions=actions)
        except Exception as exc:
            result = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}", "events": [], "filled": []}
        result["review_notes"] = book.get("job_review_notes", {}).get(job["dedupe_hash"], [])
        # Keep the persistent browser and the unsaved draft open at handoff.
        packet = await write_packet(None, directory, job, result, cli_actions=actions)
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
                result, actions = await prepare(page, job, answers, planner, vault, demo_origin=demo_origin)
            except Exception as exc:
                # Exceptions may contain DOM or private values; store only safe class metadata.
                result, actions = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}", "events": [], "filled": []}, None
            packet = await write_packet(page, directory, job, result)
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
            result, packet = asyncio.run(run_job(item["job"], observed_book, **kwargs))
            from .questions import collect, reconcile
            collect(item["job"], result, book_path, observed_book=observed_book)
            reconcile(item["job"], result, book_path)
            queue.finish(conn, item["job_hash"], result["state"], packet)
            return result
        except Exception:
            queue.finish(conn, item["job_hash"], "failed")
            raise
