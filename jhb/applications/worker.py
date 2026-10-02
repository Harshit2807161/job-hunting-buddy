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
from .planner import CodexPlanner, deterministic_plan, validate_plan


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
    events, filled, previous = [], {}, None
    authenticated = False
    deadline = time.monotonic() + 480
    for step in range(max_steps):
        if time.monotonic() > deadline: break
        snapshot = await actions.observe()
        events.append({"step": step, "event": "observed", "time": int(time.time())})
        if snapshot.get("handoff"):
            if snapshot["handoff"] == "waiting_login" and not authenticated:
                authenticated = True
                if await actions.authenticate(answers, vault):
                    events.append({"step": step, "event": "authenticated", "credential_storage": "local vault" if vault.demo_path else "OS keyring"})
                    continue
            return {"state": snapshot["handoff"], "reason": snapshot["reason"], "events": events, "filled": list(filled.values())}, actions
        fingerprint = json.dumps(snapshot, sort_keys=True)
        if fingerprint == previous:
            return {"state": "unsupported", "reason": "Continue did not reveal a new supported step; check blocked draft-save requests", "events": events, "filled": list(filled.values())}, actions
        plan = validate_plan(await asyncio.to_thread(planner, snapshot, answers), snapshot, answers)
        bindings = {b["ref"]: b["answer_key"] for b in plan["bindings"]}
        missing = []
        for field in snapshot["fields"]:
            key = bindings.get(field["ref"])
            record = answers.get(key, {})
            if record.get("status") != "verified":
                if field["required"]: missing.append({"question": field["label"], "answer_key": key})
                continue
            try:
                await actions.fill(field, record["value"])
                education_row = re.fullmatch(r"(?:school|degree|discipline|start_date|end_date)--(\d+)", field["ref"])
                display_label = field["label"] + (f" (education record {int(education_row[1])+1})" if education_row else "")
                filled[(field["label"], field["ref"])] = {"question": display_label, "ref": field["ref"], "key": key, "value": record["value"], "source": record["source"]}
                events.append({"step": step, "event": "filled", "question": field["label"], "answer_key": key})
            except ValueError:
                events.append({"step": step, "event": "unfilled", "question": field["label"], "answer_key": key})
                if field["required"]:
                    missing.append({"question": field["label"], "answer_key": key, "reason": "Stored answer unavailable or incompatible with field"})
        if missing:
            return {"state": "waiting_input", "reason": "Required answers or documents need candidate input", "missing": missing, "events": events, "filled": list(filled.values())}, actions
        updated = await actions.observe()
        if updated.get("handoff"):
            return {"state": updated["handoff"], "reason": updated["reason"], "events": events, "filled": list(filled.values())}, actions
        if {(f["ref"], f["required"]) for f in updated["fields"]} != {(f["ref"], f["required"]) for f in snapshot["fields"]}:
            events.append({"step": step, "event": "fields_revealed"})
            previous = None
            continue
        if plan["next_ref"] is None:
            terminal = any(re.fullmatch(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)",
                                       booklet.normalize(b["label"])) for b in snapshot["buttons"])
            return {"state": "waiting_review" if terminal else "unsupported",
                    "reason": "Final submission is ready for candidate review" if terminal else "Unrecognized application controls; open the application form manually",
                    "events": events, "filled": list(filled.values()), "blocked_requests": actions.blocked_requests}, actions
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
    rows = "".join(f'<tr><td>{esc(r["question"])}</td><td><pre>{esc(r["value"])}</pre></td><td>{esc(r["source"])}</td></tr>' for r in result.get("filled", []))
    missing = "".join(f'<li>{esc(r["question"])}</li>' for r in result.get("missing", []))
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Application review</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px}}td,th{{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;max-width:550px}}img{{max-width:100%}}.state{{padding:14px;background:#eef4ff}}a{{color:#1463bc}}</style>
<h1>{esc(job['title'])} · {esc(job['company'])}</h1><p class="state">{esc(result['state'])}: {esc(result['reason'])}</p>
<p>Application has not been submitted. Review the answers and documents before taking over the browser.</p>
<p><a href="{esc(job['url'])}">Original posting</a> · <a href="packet.json">Structured packet</a></p>
<ul>{missing}</ul><table><tr><th>Question</th><th>Answer</th><th>Evidence</th></tr>{rows}</table>
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
        conn.execute("UPDATE applications SET notified_at=? WHERE job_hash=?", (int(time.time()), row["job_hash"]))
        conn.commit()


async def run_job(job, book, *, planner_name="codex", demo_origin=None, headless=False,
                  interactive=False, review_seconds=0, role=None, artifacts=None):
    selected_role = role or role_for_job(job)
    if not re.fullmatch(r"[a-f0-9]{64}", job["dedupe_hash"]): raise ValueError("Invalid job identity")
    directory = (artifacts or config.ROOT / "private" / "applications") / job["dedupe_hash"]
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if not selected_role:
        result = {"state": "waiting_input", "reason": "Ambiguous role; choose --role sde or --role ml",
                  "missing": [{"question": "Choose the SDE or ML resume variant"}], "events": [], "filled": []}
        return result, await write_packet(None, directory, job, result)
    answers = booklet.for_role(book, selected_role)
    identity = queue.greenhouse_identity(job["url"])
    if identity:
        for index, record in enumerate(book.get("education_records", [])):
            for mapping in record.get("form_mappings", []):
                if mapping.get("region") == identity[0] and mapping.get("board") == identity[1]:
                    answers[f"education.{index}.school"] = booklet.answer(mapping["school_option"],
                        {"actual_institution": record["school"], "resume_source": record["source"],
                         "form_mapping": mapping})
    answers.update({key: item for key, item in book.get("custom_answers", {}).items()
                    if not item.get("scope") or (identity and item["scope"] == {"region": identity[0], "board": identity[1]})})
    planner = CodexPlanner(directory) if planner_name == "codex" else deterministic_plan
    if not demo_origin:
        from .cli_browser import BrowserUseCLI
        actions = BrowserUseCLI()
        try:
            result, actions = await prepare(None, job, answers, planner, None, cli_actions=actions)
        except Exception as exc:
            result = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}", "events": [], "filled": []}
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
            result, packet = asyncio.run(run_job(item["job"], booklet.load(book_path), **kwargs))
            queue.finish(conn, item["job_hash"], result["state"], packet)
            return result
        except Exception:
            queue.finish(conn, item["job_hash"], "failed")
            raise
