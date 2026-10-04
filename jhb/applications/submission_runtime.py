"""Fixed, finite-authority terminal action in the official Browser Use CLI.

Only this dispatcher can release a guard for automation. It never evaluates
model-supplied scripts or accepts arbitrary clicks. Normal CLI actions remain
incapable of submitting. All checks occur again inside the serialized lane.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .authorized_submission import load_gate
from .booklet import answer, normalize, write_private
from .browser import GUARD_SCRIPT
from .cli_runtime import _settled_click, dispatch as guarded_dispatch, option_matches
from .planner import key_for_field
from .queue import greenhouse_identity

TERMINAL = re.compile(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)", re.I)
CONFIRMATION = re.compile(r"your application (?:was successfully submitted|has been submitted successfully|has been received)|(?:we have|we've|we) received your application|thank you for applying", re.I)


def _control_state(helpers, field):
    if field["type"] == "file":
        return helpers["js"]("(()=>{const label="+json.dumps(normalize(field["label"]))+";const groups=[...document.querySelectorAll('.file-upload')].filter(e=>{const labelText=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.querySelector('.upload-label')?.innerText||e.innerText.split('\\n')[0];return labelText.trim().replace(/[ *]+$/,'').toLowerCase()===label});return groups.length===1?{value:groups[0].querySelector('.file-upload__filename p')?.innerText||'',receipt:groups[0].__jhbUploadReceipt||null}:null})()")
    return helpers["js"]("(()=>{const e=document.getElementById("+json.dumps(field["ref"])+
        ");return e?{value:e.value,checked:e.checked,selected:e.tagName==='SELECT'?e.selectedOptions[0]?.label||'':e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||[...(e.closest('.select__value-container')?.querySelectorAll('.select__multi-value__label')||[])].map(e=>e.innerText).join(', ')||'',countryCode:e.closest('.select__value-container')?.querySelector('.iti__flag')?.className||'',invalid:e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid)}:null})()")


def _checks(request, helpers, packet, attempt):
    if (helpers["current_tab"]()["targetId"] != request.get("target_id")
            or greenhouse_identity(helpers["js"]("location.href")) != greenhouse_identity(attempt["application_url"])
            or helpers["js"]("window.__jhbGuard===true") is not True):
        raise ValueError("Owned application identity or submission guard changed")
    snapshot = guarded_dispatch({"operation": "observe", "target_id": request["target_id"],
                                 "expected_url": attempt["application_url"]}, helpers)
    if snapshot.get("handoff"):
        return {"state": snapshot["handoff"], "reason": snapshot["reason"], "click_started": False}
    if packet.get("job", {}).get("work_country"):
        for field in snapshot["fields"]:
            if normalize(field["label"]) in {"are you authorized to work lawfully in the location posted for this position?", "work authorization"}:
                field["country_context"] = normalize(packet["job"]["work_country"])
    if helpers["js"]("[...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length)"):
        return {"state": "waiting_login", "reason": "Website authentication is required", "click_started": False}
    records = packet.get("filled", [])
    approved = {r["key"]: answer(r["value"], r.get("source")) for r in records}
    for r in records:
        if str(r["key"]).startswith("custom."):
            approved[r["key"]].update(question=r["question"], field_ref=r["ref"])
    documents = request.get("documents", {})
    checked, retained = [], []
    attached_labels = {normalize(f["label"]) for f in snapshot["fields"] if f["ref"].startswith("uploaded:")}
    for field in snapshot["fields"]:
        if field["type"] == "file" and not field["ref"].startswith("uploaded:") and normalize(field["label"]) in attached_labels:
            continue
        matches = [r for r in records if r.get("ref") == field["ref"] and
                   normalize(re.sub(r" \(education record \d+\)$", "", r.get("question", ""))) == normalize(field["label"])]
        # Uploading changes the native ref into an attached-file virtual ref.
        if field["type"] == "file":
            key = key_for_field(field, approved)
            matches = [r for r in records if r.get("key") == key] if key else []
            if matches and all(r.get("value") == matches[0].get("value") and r.get("source") == matches[0].get("source")
                               and normalize(r.get("question", "")) == normalize(matches[0].get("question", "")) for r in matches):
                matches = matches[:1]
        state = _control_state(helpers, field)
        if len(matches) != 1:
            if field["required"] or (state and (state.get("selected") or state.get("value") or state.get("checked"))):
                return {"state": "waiting_input", "reason": "A fresh field lacks an exact approved answer", "click_started": False,
                        "missing": [{"question": field["label"], "ref": field["ref"], "type": field["type"],
                                     "required": field["required"], "choices": [o["label"] for o in field.get("options", [])],
                                     **({"country_context": field["country_context"]} if field.get("country_context") else {})}]}
            continue
        record = matches[0]
        key = key_for_field(field, approved)
        catalog = re.fullmatch(r"standing\.catalog\.(\d+)\.(school|major)", record["key"])
        catalog_ref = f"{'discipline' if catalog and catalog[2] == 'major' else 'school'}--{catalog[1]}" if catalog else None
        catalog_match = catalog and field["ref"] == catalog_ref
        if (not record.get("source") or (key != record["key"] and not catalog_match)
                or not state or state.get("invalid")):
            return {"state": "waiting_review", "reason": "Approved binding or retained field validity changed", "click_started": False}
        value, kind = record["value"], field["type"]
        national = request.get("approved_phone_national", {})
        if (kind == "tel" and field.get("separate_phone_country") is True
                and record["key"] in {"identity.phone", "identity.phone_national"}
                and national.get("status") == "verified" and national.get("source")
                and isinstance(national.get("value"), str)):
            value = national["value"]
        if kind == "file":
            document = documents.get(record["key"], {})
            path = Path(document.get("path", ""))
            valid = (path.is_file() and str(path) == record["value"] and path.suffix.lower() == ".pdf"
                     and hashlib.sha256(path.read_bytes()).hexdigest() == document.get("sha256"))
            if request.get("require_receipts", True):
                valid = valid and state["value"] == path.name and bool(document.get("receipt")) and state.get("receipt") == document["receipt"]
        elif kind == "checkbox":
            valid = isinstance(value, bool) and state["checked"] == value
        elif kind in {"combobox", "select"}:
            valid = option_matches(state["selected"], value, field_id=field["ref"], field_label=field["label"])
            if field["ref"] == "country" and value == "United States" and "iti__us" in state.get("countryCode", ""):
                valid = True
        elif kind in {"text", "email", "tel", "textarea", "url", "number", "date"}:
            actual, expected = state["value"], str(value)
            if kind == "tel":
                actual, expected = re.sub(r"\D", "", actual), re.sub(r"\D", "", expected)
            valid = actual == expected
        else:
            valid = False
        if not valid:
            return {"state": "waiting_review", "reason": "An approved answer or document did not remain intact", "click_started": False}
        checked.append({**field, "answer_key": record["key"]})
        retained.append({"ref": field["ref"], "answer_key": record["key"], "state": state})
    # No approved indexed education row or other prepared control may disappear.
    visible_keys = {(f["ref"], f["answer_key"]) for f in checked}
    for record in records:
        if record["key"].startswith("documents."):
            present = any(f["answer_key"] == record["key"] for f in checked)
        else:
            present = (record["ref"], record["key"]) in visible_keys
        if not present:
            return {"state": "waiting_review", "reason": "A prepared application record disappeared", "click_started": False}
    terminals = [b for b in snapshot["buttons"] if TERMINAL.fullmatch(normalize(b["label"]))]
    if len(terminals) != 1:
        return {"state": "waiting_review", "reason": "Final submission control is unavailable or ambiguous", "click_started": False}
    return {"fields": checked, "retained": retained, "button": terminals[0], "double_check_count": len(checked)}


def dispatch(request, helpers):
    operation = request.get("operation")
    if operation not in {"locate", "check", "document", "submit"}:
        raise ValueError("Unsupported authorized submission operation")
    authority, attempt, packet = load_gate(request["authorization_path"], request["attempt_path"])
    if operation == "locate":
        identity = greenhouse_identity(attempt["application_url"])
        tabs = [t for t in helpers["list_tabs"]() if greenhouse_identity(t["url"]) == identity]
        if len(tabs) != 1:
            return {"state": "waiting_review", "reason": "An exact existing draft tab must be unambiguous", "click_started": False}
        target = tabs[0]["targetId"]
        helpers["switch_tab"](target)
        if greenhouse_identity(helpers["js"]("location.href")) != identity:
            raise ValueError("Existing application draft changed identity")
        helpers["cdp"]("Page.addScriptToEvaluateOnNewDocument", source=GUARD_SCRIPT)
        helpers["js"](GUARD_SCRIPT)
        return {"target_id": target, "url": helpers["js"]("location.href"), "guarded": True}
    if not request.get("target_id"):
        raise ValueError("Authorized submission requires an owned existing target")
    helpers["switch_tab"](request["target_id"])
    if operation == "document":
        # This operation replaces approved PDFs while the guard remains enabled.
        field, value = request["field"], request["value"]
        matches = [r for r in packet.get("filled", []) if r.get("key") in {"documents.resume", "documents.cover_letter"}
                   and r.get("value") == value and normalize(r.get("question", "")) == normalize(field["label"])]
        if matches and all(r.get("key") == matches[0].get("key") and r.get("value") == matches[0].get("value")
                           and normalize(r.get("question", "")) == normalize(matches[0].get("question", ""))
                           and r.get("source") == matches[0].get("source") for r in matches):
            matches = matches[:1]
        if len(matches) != 1 or field["type"] != "file":
            raise ValueError("Only an approved packet document may be uploaded")
        if helpers["js"]("window.__jhbGuard===true") is not True:
            raise ValueError("Document replacement requires the application guard")
        return guarded_dispatch({**request, "operation": "fill", "expected_url": attempt["application_url"]}, helpers)
    first = _checks(request, helpers, packet, attempt)
    if operation == "check" or first.get("state"):
        return first
    # Hold an attempt lock across final checks and the terminal click. A second
    # controller cannot replay even if the caller's durable queue lease expires.
    lock = Path(request["attempt_path"]).with_suffix(".lock")
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, attempt, packet = load_gate(request["authorization_path"], request["attempt_path"])
        helpers["wait"](0.15)
        second = _checks(request, helpers, packet, attempt)
        if second.get("state"):
            return second
        if first != second:
            return {"state": "waiting_review", "reason": "The draft changed between the two checks", "click_started": False}
        checks_path = Path(request["attempt_path"]).parent / "checks.json"
        write_private(checks_path, {"check_count": 2, "checks": [first, second], "documents": request["documents"],
                                   "authorization_id": attempt["authorization_id"], "job_hash": attempt["job_hash"],
                                   "target_id": request["target_id"], "checked_at": datetime.now(timezone.utc).isoformat()})
        before_text = helpers["js"]("document.body.innerText")
        node = next((n for n in helpers["cdp"]("Accessibility.getFullAXTree")["nodes"]
                     if str(n.get("backendDOMNodeId")) == second["button"]["ref"]
                     and n.get("role", {}).get("value") == "button"
                     and normalize(n.get("name", {}).get("value", "")) == normalize(second["button"]["label"])), None)
        if not node:
            raise ValueError("The verified terminal button changed")
        clicked = False
        def native_submit(x, y):
            nonlocal clicked
            # Geometry is now settled. Re-read authority immediately before
            # consuming the one-shot attempt; expiry cannot be bypassed by work.
            _, current, _ = load_gate(request["authorization_path"], request["attempt_path"])
            if greenhouse_identity(helpers["js"]("location.href")) != greenhouse_identity(current["application_url"]):
                raise ValueError("Application identity changed before submission")
            final_check = _checks(request, helpers, packet, current)
            if final_check != second:
                raise ValueError("Retained application changed during final click geometry")
            current.update(runtime_click_started=True, click_started_at=datetime.now(timezone.utc).isoformat(),
                           double_check_count=2, checked_fields=second["double_check_count"])
            write_private(Path(request["attempt_path"]), current)
            clicked = True
            helpers["js"]("window.__jhbGuard=false")
            cdp = helpers["cdp"]
            cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
            cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, buttons=0)
            cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
            cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
        try:
            _settled_click(node["backendDOMNodeId"], helpers["cdp"], helpers["wait"], native_submit)
            for _ in range(40):
                helpers["wait"](0.25)
                body = helpers["js"]("document.body.innerText")
                current_url = helpers["js"]("location.href")
                visible_terminal = helpers["js"]("[...document.querySelectorAll('button,input[type=submit],[role=button]')].some(e=>!e.disabled&&e.getClientRects().length&&/^(submit(?: application)?|apply(?: now)?|send application|finish application)$/i.test((e.innerText||e.value||'').trim()))")
                parsed = urlsplit(current_url)
                host_allowed = parsed.scheme == "https" and parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
                confirmation = CONFIRMATION.search(body)
                if host_allowed and confirmation and not CONFIRMATION.search(before_text) and not visible_terminal:
                    receipt = {"state": "submitted", "confirmed": True, "job_hash": attempt["job_hash"],
                               "application_url": attempt["application_url"], "url": attempt["application_url"], "observed_url": current_url,
                               "authorization_id": attempt["authorization_id"], "target_id": request["target_id"],
                               "confirmed_at": datetime.now(timezone.utc).isoformat(), "confirmation": confirmation[0],
                               "source": "Live Browser Use CLI success page", "check_count": 2,
                               "body": body, "double_check_count": 2, "checks_path": str(checks_path)}
                    path = Path(request["attempt_path"]).parent / "receipt.json"
                    write_private(path, receipt)
                    return {"state": "submitted", "confirmed": True, "receipt_path": str(path), "click_started": True,
                            "check_count": 2, "checks_path": str(checks_path)}
                if re.search(r"verify (?:you are human|your email)|verification code|check your inbox", body, re.I):
                    return {"state": "uncertain", "handoff": "waiting_login", "reason": "Post-submit verification requires review; do not retry", "click_started": True}
                if helpers["js"]("[...document.querySelectorAll('iframe')].some(e=>/recaptcha|hcaptcha|challenge/i.test(e.src)&&e.getClientRects().length&&e.getBoundingClientRect().height>90)"):
                    return {"state": "uncertain", "handoff": "waiting_captcha", "reason": "Post-submit verification requires review; do not retry", "click_started": True}
            return {"state": "uncertain", "reason": "No positive submission receipt was observed; do not retry", "click_started": True}
        finally:
            # Only the owned document was released; all other draft guards remain.
            helpers["js"](GUARD_SCRIPT)
            if clicked:
                write_private(Path(request["attempt_path"]).parent / "final-browser.json", {
                    "url": helpers["js"]("location.href"), "body": helpers["js"]("document.body.innerText"),
                    "guarded": helpers["js"]("window.__jhbGuard===true")})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
