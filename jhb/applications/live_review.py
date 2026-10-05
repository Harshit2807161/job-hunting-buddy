"""Read the candidate's current form for an explicit portal submission click.

No filling, navigation, document replacement, or candidate-profile updates.
The saved preparation packet supplies identity, never replacement answer values.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import os

from .. import config
from . import boards, booklet

MODE = "candidate_current_form"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def observe(request, helpers):
    """Existing dispatcher hook: read current controls and native File objects."""
    from .submission_runtime import _board_dispatch, _control_state
    target, url = request.get("target_id"), request.get("expected_url")
    identity = boards.job_identity(url)
    if (not target or not identity or identity[0] not in {"ashby", "greenhouse"}
            or helpers["current_tab"]()["targetId"] != target
            or boards.job_identity(helpers["js"]("location.href")) != identity
            or helpers["js"]("window.__jhbGuard===true") is not True):
        raise ValueError("The exact guarded application tab is unavailable")
    snapshot = _board_dispatch({"operation": "observe", "target_id": target,
                               "expected_url": url}, helpers, url)
    if snapshot.get("handoff"):
        raise ValueError("The application requires authentication or verification")
    fields = snapshot.get("fields", [])
    if not fields or len(fields) > 250:
        raise ValueError("Current application questions are unavailable")
    controls = []
    for field in fields:
        state = _control_state(helpers, field, identity[0])
        if not isinstance(state, dict):
            raise ValueError("A current application control is unreadable")
        if field["type"] == "file":
            from .manual_runtime import ASHBY_GROUPS
            if identity[0] == "ashby":
                group = "("+ASHBY_GROUPS+")["+str(field["owner_index"])+"]"
                expr = ("[...("+group+").querySelectorAll('input,textarea,select')].filter(e=>e.closest('[data-field-path]')==="+group+")["+str(field["control_index"])+"]"
                        if field["ref"].startswith("ashby:") else "document.getElementById("+json.dumps(field["ref"])+")")
                result = helpers["cdp"]("Runtime.evaluate", expression="("+expr+")?.files?.[0]", returnByValue=False)
                obj = result.get("result", {}).get("objectId")
                if obj:
                    try:
                        path = helpers["cdp"]("DOM.getFileInfo", objectId=obj).get("path")
                        metadata = helpers["cdp"]("Runtime.callFunctionOn", objectId=obj,
                            functionDeclaration="async function(){const digest=await crypto.subtle.digest('SHA-256',await this.arrayBuffer());return {name:this.name,size:this.size,sha256:[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('')}}",
                            awaitPromise=True, returnByValue=True)
                        value = metadata.get("result", {}).get("value")
                        if not isinstance(value, dict) or not path:
                            raise ValueError("Current uploaded document bytes are unreadable")
                        state["document"] = {**value, "path": path}
                    finally:
                        helpers["cdp"]("Runtime.releaseObject", objectId=obj)
            # Greenhouse removes its native File after upload. Its retained
            # mutation-sensitive receipt is bound to the preparation PDF below.
        controls.append({"field": field, "state": state})
    return {"url": snapshot["url"], "target_id": target, "controls": controls,
            "buttons": snapshot.get("buttons", []), "read_only": True}


def _value(field, state):
    kind = field["type"]
    if state.get("invalid"):
        raise ValueError("The live form has an invalid field: "+field["label"])
    if kind in {"radio", "multiselect"}:
        values = state.get("selected") or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("A selected answer is unreadable")
        offered = [o["label"] for o in field.get("options", []) if not o.get("disabled")]
        if any(v not in offered for v in values) or kind == "radio" and len(values) > 1:
            raise ValueError("Current selected choices are ambiguous")
        value = values if kind == "multiselect" else values[0] if values else None
    elif kind == "checkbox":
        value = state.get("checked")
        if type(value) is not bool:
            raise ValueError("A checkbox is unreadable")
    elif kind in {"combobox", "select"}:
        value = state.get("selected") or None
    elif kind in {"text", "textarea", "email", "tel", "url", "number", "date", "file"}:
        value = state.get("value") or None
        if kind == "text" and field.get("calendar_format") == "MM/DD/YYYY" and value:
            from datetime import datetime
            value = datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    else:
        raise ValueError("Current field type needs adapter support")
    blank = value is None or value == [] or kind == "checkbox" and value is False
    if field.get("required") and blank:
        raise ValueError("The live form has a required blank: "+field["label"])
    return value, blank


def project(packet, observation):
    """Use only observed live values; retain old evidence separately for audit."""
    if boards.job_identity(observation.get("url")) != boards.job_identity(packet["job"]["url"]):
        raise ValueError("Live form belongs to another application")
    filled, inventory, blanks, documents = [], [], [], {}
    seen = set()
    for item in observation["controls"]:
        field, state = item["field"], item["state"]
        ref = field["ref"]
        if ref in seen or field.get("description_truncated"):
            raise ValueError("Question identity or instructions are incomplete")
        seen.add(ref)
        value, blank = _value(field, state)
        key = "custom.live_review."+hashlib.sha256(ref.encode()).hexdigest()[:24]
        source = {"provider": "local_portal_current_form_approval", "method": "browser_use_cli_read_only",
                  "job_hash": packet["job"]["dedupe_hash"], "field_ref": ref,
                  "observed_question_sha256": _digest(field), "target_id": observation["target_id"]}
        if field["type"] == "file" and not blank:
            label = booklet.normalize(field["label"])
            key = "documents.resume" if label in {"resume", "resume/cv"} else "documents.cover_letter" if label == "cover letter" else None
            if key is None or key in documents:
                raise ValueError("Current document control is unsupported or ambiguous")
            native = state.get("document")
            if native:
                path = Path(native.get("path", ""))
                if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                        or path.suffix.lower() != ".pdf" or path.stat().st_size > 10_000_000):
                    raise ValueError("Current uploaded document is not an accessible local PDF")
                raw = path.read_bytes()
                if (not raw.startswith(b"%PDF-") or path.name != native.get("name")
                        or len(raw) != native.get("size") or hashlib.sha256(raw).hexdigest() != native.get("sha256")):
                    raise ValueError("Current uploaded bytes differ from their local source")
                value = str(path)
                source.update(sha256=native["sha256"], document_origin="candidate_current_upload")
            else:
                matches = [r for r in packet.get("filled", []) if r.get("key") == key]
                previous = matches[0] if len(matches) == 1 else {}
                path = Path(previous.get("value", ""))
                receipt = state.get("receipt")
                if (not receipt or receipt != previous.get("upload_receipt") or not path.is_file()
                        or path.name != state.get("value") or not previous.get("document_sha256")
                        or hashlib.sha256(path.read_bytes()).hexdigest() != previous["document_sha256"]):
                    raise ValueError("Current upload needs document verification; it was left unchanged")
                value = str(path)
                source.update(sha256=previous["document_sha256"], document_origin="retained_preparation_receipt")
            documents[key] = booklet.answer(value, source)
        from .review_inventory import candidate_wording_requested
        own_wording = candidate_wording_requested(field["label"]+" "+(field.get("description") or ""))
        row = {"ref": ref, "question": field["label"], "type": field["type"],
               "required": bool(field["required"]), "status": "blank" if blank else "answered",
               "answer_key": key, "category": "document" if field["type"] == "file" else "application_question",
               "source": source, "proposed": False, "step": 0,
               "choices": [o["label"] for o in field.get("options", [])],
               "description": field.get("description") or "", "description_truncated": False,
               "candidate_wording_required": own_wording}
        if own_wording:
            previous = next((r for r in packet.get("filled", []) if r.get("ref") == ref), None)
            source["candidate_edited"] = bool(previous is None or previous.get("value") != value)
            if previous and previous.get("value") == value:
                source["previous_source"] = previous.get("source")
        if field.get("calendar_format"):
            row["calendar_format"] = field["calendar_format"]
        inventory.append(row)
        if blank:
            blanks.append({"ref": ref, "question": field["label"], "required": False, "type": field["type"]})
        else:
            record = {"ref": ref, "question": field["label"], "key": key, "value": value,
                      "source": source, "user_override": True}
            if field["type"] == "file":
                record.update(upload_receipt=state.get("receipt"), document_sha256=source["sha256"])
            filled.append(record)
    if "documents.resume" not in documents:
        raise ValueError("The current application has no verified resume upload")
    if not any(re.fullmatch(r"submit(?: application)?|apply(?: now)?|send application|finish application",
                            booklet.normalize(b.get("label", ""))) for b in observation.get("buttons", [])):
        raise ValueError("The current form is not at its final submission step")
    result = copy.deepcopy(packet)
    for key in ("capture", "created_at", "job", "missing", "verification"):
        result.pop(key, None)
    result.update(state="waiting_review", reason="Current browser answers captured without changing candidate edits",
                  filled=filled, optional_questions=blanks, agent_tasks=[], submitted=False,
                  review_mode=MODE, live_documents=documents,
                  live_review={"target_id": observation["target_id"], "snapshot_sha256": _digest(observation),
                               "captured_at": int(time.time()), "old_packet_sha256": _digest(packet)},
                  review_inventory={"complete": True, "fields": inventory, "observed_at": int(time.time()), "step_count": 1},
                  review_questions=inventory,
                  review_completeness={"schema_version": 1, "inventory_verified": True,
                    "all_observed_count": len(inventory), "answered_count": len(filled), "blank_count": len(blanks),
                    "declined_count": 0, "blank_substantive_count": 0, "candidate_wording_required_count": 0,
                    "requires_explicit_acknowledgment": bool(blanks), "requires_explicit_approval": True})
    result.setdefault("events", []).append({"event": "candidate_current_form_approval", "changed_values_preserved": True})
    return result


async def capture_current(packet_path, *, client=None, acknowledged_blank_refs=()):
    """Called only while processing the candidate's explicit Approve request."""
    from .cli_browser import BrowserUseCLI
    from .authorized_submission import private_file
    from .worker import write_packet
    path = private_file(packet_path)
    packet = json.loads(path.read_bytes())
    from .historical import cached_match
    if cached_match(packet["job"]):
        raise ValueError("This role matches an existing spreadsheet application; reconcile it before reapplying")
    target = packet.get("capture", {}).get("target_id")
    if not target:
        raise ValueError("Saved draft has no exact browser target")
    if client is None:
        if boards.job_identity(packet["job"]["url"])[0] == "ashby":
            from .manual_ats import ManualATSCLI
            client = ManualATSCLI(packet["job"]["url"], board="ashby", timeout=90)
        else:
            client = BrowserUseCLI(timeout=90)
    client.target_id, client.expected_url = target, packet["job"]["url"]
    first = await client.invoke("review_current")
    result = project(packet, first)
    second = await client.invoke("review_current")
    if first != second:
        raise ValueError("The form changed while the approval click was being captured")
    blanks = {field["ref"] for field in result["review_inventory"]["fields"] if field["status"] != "answered"}
    if not blanks.issubset(set(acknowledged_blank_refs)):
        raise ValueError("Explicitly acknowledge each optional blank answer before approval")
    directory = path.parent
    stamp = str(time.time_ns())
    staged = directory / ("current-form-capture-"+stamp)
    await write_packet(None, staged, packet["job"], result, cli_actions=client)
    if result.get("state") != "waiting_review":
        raise ValueError("The current form screenshot could not be verified")
    # A failed capture leaves the prior review packet usable. Recheck the
    # private evidence immediately before promoting the new read-only snapshot.
    if json.loads(path.read_bytes()) != packet:
        raise ValueError("The draft changed while the current form was captured")
    from . import application_discard
    application_discard.check(config.ROOT, packet["job"]["dedupe_hash"])
    archive = directory / ("before-current-form-approval-"+stamp)
    archive.mkdir(mode=0o700)
    for name in ("packet.json", "browser.png", "review.html", "events.json"):
        previous = directory / name
        if previous.is_file():
            shutil.copy2(previous, archive / name)
    for name in ("browser.png", "review.html", "events.json", "packet.json"):
        os.replace(staged / name, directory / name)
    booklet.write_private(directory / "candidate-current-form.json", first)
    return directory / "review.html", result
