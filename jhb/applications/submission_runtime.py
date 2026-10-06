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

from .authorized_submission import audit_hash, load_gate, private_file
from .booklet import answer, normalize, write_private, annotate_work_country
from .browser import GUARD_SCRIPT
from .cli_runtime import _settled_click, dispatch as guarded_dispatch, option_matches
from .planner import key_for_field
from . import boards
from .manual_runtime import ASHBY_GROUPS, application_scope, dispatch as ashby_dispatch
from .native_ats_runtime import NATIVE_CONTROLS

TERMINAL = re.compile(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)", re.I)
CONFIRMATION = re.compile(r"your application (?:was successfully submitted|has been submitted successfully|has been received)|(?:we have|we've|we) received your application|thank you for applying", re.I)


def _same_native_number(actual, expected):
    """HTML number serialization may drop .0; compare exact finite decimals."""
    from decimal import Decimal, InvalidOperation
    if not isinstance(actual, str) or type(expected) not in {str, int, float}:
        return False
    expected = str(expected)
    number = r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    if any(len(value) > 128 or not re.fullmatch(number, value) for value in (actual, expected)):
        return False
    try:
        first, second = Decimal(actual), Decimal(expected)
        return first.is_finite() and second.is_finite() and first == second
    except InvalidOperation:
        return False


def _retained_question_matches(record, field, inventory):
    """Recognize only the preparer's exact indexed education decoration.

    Custom answers keep the native question in the complete inventory. That
    evidence, including source and observed choices, must still match before
    its display-only row suffix can be removed for planner binding.
    """
    if record.get("ref") != field.get("ref"):
        return False
    question = record.get("question", "")
    if normalize(question) == normalize(field.get("label", "")):
        return True
    row = re.fullmatch(r"(?:school|degree|discipline|start_date|end_date)--(\d+)", str(field.get("ref", "")))
    if not row or question != f"{field.get('label', '')} (education record {int(row[1])+1})":
        return False
    if not str(record.get("key", "")).startswith("custom."):
        return True
    originals = [item for item in inventory if item.get("ref") == field["ref"]]
    if len(originals) != 1:
        return False
    original = originals[0]
    return (original.get("question") == field["label"]
            and original.get("type") == field.get("type")
            and bool(original.get("required")) == bool(field.get("required"))
            and original.get("calendar_format") == field.get("calendar_format")
            and (original.get("description") or "") == (field.get("description") or "")
            and bool(original.get("description_truncated")) == bool(field.get("description_truncated"))
            and original.get("choices", []) == [option["label"] for option in field.get("options", [])]
            and original.get("status") == "answered"
            and original.get("answer_key") == record.get("key")
            and bool(record.get("source")) and original.get("source") == record["source"])


def _native_form_submit(helpers, button, approved_refs, board="greenhouse"):
    """Read the exact AX node's native form owner, never infer from its label."""
    if not str(button.get("ref", "")).isdigit() or int(button["ref"]) <= 0:
        return False
    remote = helpers["cdp"]("DOM.resolveNode", backendNodeId=int(button["ref"]))
    object_id = remote.get("object", {}).get("objectId")
    if not object_id:
        return False
    try:
        result = helpers["cdp"]("Runtime.callFunctionOn", objectId=object_id,
            functionDeclaration="""function(refs,board) {
                const e=this,w=e.ownerDocument?.defaultView;
                if(!w || !(e instanceof w.HTMLButtonElement || e instanceof w.HTMLInputElement)
                    || e.matches(':disabled') || e.getAttribute('aria-disabled')==='true'
                    || !e.getClientRects().length || w.getComputedStyle(e).visibility==='hidden') return false;
                if(board==='ashby'){
                    // Current Ashby puts Submit beside its field container,
                    // inside the application's named tab panel (not a form).
                    const panel=e.closest('#form[role="tabpanel"]');
                    const owner=e.closest('.ashby-application-form-container,form') ||
                        (e.matches('.ashby-application-form-submit-button') && panel &&
                         panel.querySelectorAll('.ashby-application-form-submit-button').length===1 ? panel : null);
                    if(!owner || !refs.length) return false;
                    return refs.every(ref=>{
                        if(ref.startsWith('ashby:')){
                            const path=ref.slice(6).replace(/:control:\\d+$|:communicationConsent$/,'');
                            return [...owner.querySelectorAll('[data-field-path]')].some(g=>g.getAttribute('data-field-path')===path);
                        }
                        const control=e.ownerDocument.getElementById(ref);
                        return !!control&&owner.contains(control);
                    });
                }
                if(e.type!=='submit') return false;
                const form=e.form;
                if(!(form instanceof w.HTMLFormElement)) return false;
                if(board==='workable'||board==='lever'){
                    const controls=NATIVE_OWNED_CONTROLS;
                    return refs.length>0&&refs.every(ref=>{
                        if(ref.startsWith('native-name:')){
                            const matches=controls.filter(c=>c.name===ref.slice(12));
                            return matches.length===1&&matches[0].form===form;
                        }
                        if(ref.startsWith('native:')){const c=controls[Number(ref.slice(7))];return !!c&&c.form===form;}
                        if(ref.startsWith('native-radio:')||ref.startsWith('native-multiselect:')){
                            const name=ref.split(':').slice(1).join(':');
                            return controls.some(c=>c.form===form&&c.name===name&&(c.type==='radio'||c.type==='checkbox'));
                        }
                        const c=e.ownerDocument.getElementById(ref);return !!c&&c.form===form;
                    });
                }
                return refs.some(ref=>{
                    const control=e.ownerDocument.getElementById(ref);
                    return !!control && (control.form===form || form.contains(control));
                });
            }""".replace("NATIVE_OWNED_CONTROLS", "("+NATIVE_CONTROLS+")"), arguments=[{"value": approved_refs}, {"value": board}], returnByValue=True)
        return not result.get("exceptionDetails") and result.get("result", {}).get("value") is True
    finally:
        helpers["cdp"]("Runtime.releaseObject", objectId=object_id)


def _visible_application_submit(helpers, approved_refs, board="greenhouse"):
    nodes = helpers["cdp"]("Accessibility.getFullAXTree")["nodes"]
    return any(_native_form_submit(helpers, {"ref": str(node.get("backendDOMNodeId", "")),
                                            "label": node.get("name", {}).get("value", "")}, approved_refs, board)
               for node in nodes if not node.get("ignored") and node.get("role", {}).get("value") == "button"
               and TERMINAL.fullmatch(normalize(node.get("name", {}).get("value", ""))))


def _board_dispatch(request, helpers, url):
    identity = boards.job_identity(url)
    if identity and identity[0] == "greenhouse":
        return guarded_dispatch(request, helpers)
    if identity and identity[0] in {"ashby", "workable", "lever"}:
        scope = application_scope(boards.canonical_url(url) + ("/application" if identity[0] == "ashby" else "/apply"), identity[0])
        return ashby_dispatch({**request, "scope": scope, "foreground": True}, helpers)
    raise ValueError("No reviewed terminal adapter for this job board")


def _ashby_control_state(helpers, field):
    group = "("+ASHBY_GROUPS+")["+str(field["owner_index"])+"]"
    if field.get("widget") in {"yesno", "radio", "checkboxes", "communicationConsent"}:
        return helpers["js"]("(()=>{const g="+group+";if(!g)return null;const owned=e=>e.closest('[data-field-path]')===g;"
            "const opts="+json.dumps(field.get("options", []))+";const selected=opts.filter(o=>{let e;"
            "if("+json.dumps(field["widget"])+"==='yesno'){e=[...g.querySelectorAll('button.ashby-application-form-input-yesno-option')].find(e=>owned(e)&&e.getAttribute('data-option')===o.value);return e?.getAttribute('aria-pressed')==='true'}"
            "e=o.id?document.getElementById(o.id):[...g.querySelectorAll('input[type=radio]')].find(e=>owned(e)&&e.name==='communicationConsent'&&e.value===o.value);return e?.checked===true});"
            "return {selected:selected.map(o=>o.label),value:'',invalid:[...g.querySelectorAll('input')].filter(owned).some(e=>e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid))}})()")
    expression = ("[...("+group+").querySelectorAll('input,textarea,select')].filter(e=>e.closest('[data-field-path]')==="+group+")["+str(field["control_index"])+"]"
                  if field["ref"].startswith("ashby:") else "document.getElementById("+json.dumps(field["ref"])+")")
    state = helpers["js"]("(()=>{const e="+expression+";return e?{value:e.type==='file'?e.files?.[0]?.name||'':e.value,checked:e.checked,"
        "selected:e.tagName==='SELECT'?e.selectedOptions[0]?.label||'':e.getAttribute('role')==='combobox'&&e.getAttribute('aria-expanded')==='false'?e.value:'',"
        "receipt:e.__jhbUploadReceipt||null,invalid:e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid)}:null})()")
    if state and field.get("type") == "file":
        from .ashby_uploads import saved_file
        observed_file = saved_file(helpers, field)
        if observed_file is not None:
            state["ashby_saved_file"] = observed_file
    from .ashby_education import school_control, retained_school_catalog
    if state and school_control(field):
        proof = helpers["js"]("(()=>{const e="+expression+";return e?{proof:e.__jhbSchoolSelection||null,expanded:e.getAttribute('aria-expanded')}:null})()")
        if proof:
            catalog = retained_school_catalog(proof["proof"], field, application_scope(helpers["js"]("location.href")),
                                              {**state, "expanded": proof["expanded"]})
            if catalog:
                state["selected"] = catalog["selected_choice"]
    return state


def _control_state(helpers, field, board="greenhouse"):
    if board == "ashby":
        return _ashby_control_state(helpers, field)
    if board in {"workable", "lever"}:
        if field["type"] in {"radio", "multiselect"}:
            return helpers["js"]("(()=>{const controls=("+NATIVE_CONTROLS+");const opts="+json.dumps(field.get("options", []))+";"
                "return {selected:opts.filter(o=>controls[o.native_index]?.checked).map(o=>o.label),value:'',invalid:opts.some(o=>{const e=controls[o.native_index];return !e||e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid)})}})()")
        expression = "("+NATIVE_CONTROLS+")["+str(field["native_index"])+"]"
        return helpers["js"]("(()=>{const e="+expression+";if(!e)return null;let selected=e.tagName==='SELECT'?e.selectedOptions[0]?.label||'':"
            "e.getAttribute('role')==='combobox'&&e.getAttribute('aria-expanded')==='false'?e.value:'';"
            "let locationToken=null;if("+json.dumps(field.get("widget"))+"==='lever-location'){const hidden=e.closest('.application-field')?.querySelector('#selected-location[name=selectedLocation]');locationToken=hidden?.value||'';selected=locationToken?e.value:''}"
            "return {value:e.type==='file'?e.files?.[0]?.name||'':e.value,checked:e.checked,selected,receipt:e.__jhbUploadReceipt||null,"
            "...(locationToken!==null?{locationToken}:{}),invalid:e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid)}})()")
    if field["type"] == "file":
        return helpers["js"]("(()=>{const label="+json.dumps(normalize(field["label"]))+";const groups=[...document.querySelectorAll('.file-upload')].filter(e=>{const labelText=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.querySelector('.upload-label')?.innerText||e.innerText.split('\\n')[0];return labelText.trim().replace(/[ *]+$/,'').toLowerCase()===label});return groups.length===1?{value:groups[0].querySelector('.file-upload__filename p')?.innerText||'',receipt:groups[0].__jhbUploadReceipt||null}:null})()")
    return helpers["js"]("(()=>{const e=document.getElementById("+json.dumps(field["ref"])+
        ");return e?{value:e.value,checked:e.checked,selected:e.tagName==='SELECT'?e.selectedOptions[0]?.label||'':e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||[...(e.closest('.select__value-container')?.querySelectorAll('.select__multi-value__label')||[])].map(e=>e.innerText).join(', ')||'',countryCode:e.closest('.select__value-container')?.querySelector('.iti__flag')?.className||'',invalid:e.getAttribute('aria-invalid')==='true'||(e.willValidate&&!e.validity.valid)}:null})()")


def _review_matches(request, attempt, snapshot):
    path = private_file(Path(request["attempt_path"]).parent / "independent-review.json")
    token = json.loads(path.read_text())
    review = token.get("review", {})
    book_path = private_file(token.get("approved_book_path", ""))
    book_digest = hashlib.sha256(book_path.read_bytes()).hexdigest()
    from .application_review import snapshot_digest
    return (token.get("verdict") == "approved" and token.get("source") == "independent_application_review"
            and token.get("job_hash") == attempt["job_hash"] and token.get("authorization_id") == attempt["authorization_id"]
            and token.get("packet_sha256") == attempt["packet_sha256"]
            and token.get("audit_sha256") == audit_hash(snapshot, request["documents"])
            and token.get("approved_book_path") == review.get("approved_book_path")
            and token.get("approved_book_sha256") == review.get("approved_book_sha256") == book_digest
            and review.get("verdict") == "approved" and review.get("reviewer") == "codex-readonly"
            and review.get("source") == "independent_application_review" and review.get("issues") == []
            and review.get("snapshot_sha256") == snapshot_digest(snapshot)
            and review.get("authorization_id") == attempt["authorization_id"] and review.get("job_hash") == attempt["job_hash"])


def _reviewed_probe_answers(request, packet, attempt, approved):
    """Restore only review-bound prerequisites for read-only native catalogs.

    Approved values remain the separate binding/retention catalog. A derived
    answer alone cannot establish the facts needed to reopen a closed menu.
    """
    derived = [r for r in packet.get("filled", [])
               if str(r.get("key", "")).startswith("standing.observed.")]
    from .education_categories import projection, scoped
    categories = [r for r in packet.get("filled", []) if projection(r)]
    from .observed_question import bases_valid, guarded
    profiles = [r for r in packet.get("filled", [])
                if str(r.get("key", "")).startswith("custom.profile.") and guarded(r["key"], r)]
    if not derived and not categories and not profiles:
        return approved
    binding = attempt.get("review_binding")
    if not binding and request.get("authorization_path"):
        from .overnight import PORTAL_SCOPE
        raw = private_file(request["authorization_path"]).read_bytes()
        authority = json.loads(raw)
        if (authority.get("scope") == PORTAL_SCOPE
                and hashlib.sha256(raw).hexdigest() == attempt.get("authorization_id")
                and authority.get("job_hash") == attempt.get("job_hash")):
            binding = authority.get("binding")
    if not binding:
        return approved  # Legacy evidence cannot acquire a new fact source.
    from . import booklet
    from .approvals import _facts
    job, role = packet.get("job", {}), binding.get("selected_role")
    packet_file = private_file(binding.get("packet_path", ""))
    raw_packet = packet_file.read_bytes()
    if (role not in {"sde", "ml"} or role != packet.get("selected_role", job.get("selected_role"))
            or str(packet_file) != attempt.get("packet_path")
            or binding.get("packet_sha256") != attempt.get("packet_sha256")
            or hashlib.sha256(raw_packet).hexdigest() != binding.get("packet_sha256")
            or json.loads(raw_packet) != packet
            or job.get("dedupe_hash") != attempt.get("job_hash")
            or boards.application_hash(job.get("url")) != attempt.get("job_hash")
            or boards.job_identity(job.get("url")) != boards.job_identity(attempt.get("application_url"))):
        raise ValueError("Native catalog facts do not bind this reviewed job and role")
    book = booklet.load(private_file(binding.get("book_path", "")))
    if _facts(book, job, role) != binding.get("facts_sha256"):
        raise ValueError("Reviewed catalog facts changed")
    catalog, probes = booklet.for_role(book, role, job=job), dict(approved)
    if profiles:
        from .questions import _scope
        from .worker import _scoped_custom_answers
        saved_profiles = _scoped_custom_answers(book, job, _scope(job))
        for row in profiles:
            saved = saved_profiles.get(row["key"], {})
            if (not guarded(row["key"], saved) or saved.get("status") != "verified"
                    or saved.get("value") != row.get("value") or saved.get("source") != row.get("source")
                    or saved.get("field_ref") != row.get("ref") or not bases_valid(saved, catalog)):
                raise ValueError("Derived profile differs from its reviewed base facts")
            probes[row["key"]] = saved
            for key in saved["source"]["basis_values"]:
                if key in probes and (probes[key].get("value") != catalog[key]["value"]
                                      or probes[key].get("status") != "verified" or not probes[key].get("source")):
                    raise ValueError("Derived profile conflicts with a retained base fact")
                probes[key] = catalog[key]
    for row in categories:
        saved = book.get("custom_answers", {}).get(row.get("key"), {})
        if (not str(row.get("key", "")).startswith("custom.") or not projection(saved)
                or saved.get("value") != row.get("value") or saved.get("source") != row.get("source")
                or saved.get("field_ref") != row.get("ref") or saved.get("proposed") is not True
                or row.get("proposed") is not True):
            raise ValueError("Employer category differs from its reviewed original facts")
        probes[row["key"]] = scoped(saved, book, job)
    for row in derived:
        source = row.get("source", {})
        records = source.get("records") if isinstance(source, dict) else None
        if (not isinstance(source, dict) or source.get("method") != "verified_observed_form_derivation"
                or row["key"] != "standing.observed." + str(source.get("observation_sha256"))
                or not isinstance(records, dict) or not records):
            raise ValueError("Derived answer lacks reviewed base facts")
        for key, record in records.items():
            if (not isinstance(record, dict) or record.get("status") != "verified"
                    or not record.get("source") or catalog.get(key) != record):
                raise ValueError("Derived answer differs from its reviewed base facts")
            if key not in probes:
                probes[key] = record
    return probes


def _checks(request, helpers, packet, attempt):
    if packet.get("review_mode") == "candidate_current_form":
        from .live_review import observe, project
        if request.get("target_id") != packet.get("live_review", {}).get("target_id"):
            raise ValueError("The candidate-approved browser target changed")
        observation = observe({**request, "expected_url": attempt["application_url"]}, helpers)
        current = project(packet, observation)
        expected = [(r["ref"], r["value"]) for r in packet["filled"]]
        actual = [(r["ref"], r["value"]) for r in current["filled"]]
        metadata = ("ref", "question", "type", "required", "choices", "description", "description_truncated", "calendar_format")
        old_fields = [{k:f.get(k) for k in metadata} for f in packet["review_inventory"]["fields"]]
        new_fields = [{k:f.get(k) for k in metadata} for f in current["review_inventory"]["fields"]]
        if expected != actual or old_fields != new_fields or current["optional_questions"] != packet.get("optional_questions", []):
            return {"state": "waiting_review", "reason": "The live form changed after your current-form approval click", "click_started": False}
        if {k:v["source"]["sha256"] for k,v in current["live_documents"].items()} != {k:v["sha256"] for k,v in request["documents"].items()}:
            return {"state": "waiting_review", "reason": "An uploaded file changed after your current-form approval click", "click_started": False}
        refs = [f["ref"] for f in current["filled"] if not f["ref"].startswith("uploaded:")]
        board = boards.job_identity(attempt["application_url"])[0]
        terminals = [b for b in observation["buttons"] if TERMINAL.fullmatch(normalize(b["label"]))
                     and _native_form_submit(helpers, b, refs, board)]
        if len(terminals) != 1:
            return {"state": "waiting_review", "reason": "Final submission control is unavailable or ambiguous", "click_started": False}
        keys = {r["ref"]:r["key"] for r in current["filled"]}
        return {"fields": [{**x["field"], "answer_key": keys.get(x["field"]["ref"])} for x in observation["controls"]],
                "retained": [{"ref":x["field"]["ref"],"answer_key":keys.get(x["field"]["ref"]),"state":x["state"]} for x in observation["controls"]],
                "button": terminals[0], "double_check_count": len(current["filled"])}
    board = boards.job_identity(attempt["application_url"])[0]
    if (helpers["current_tab"]()["targetId"] != request.get("target_id")
            or boards.job_identity(helpers["js"]("location.href")) != boards.job_identity(attempt["application_url"])
            or helpers["js"]("window.__jhbGuard===true") is not True):
        raise ValueError("Owned application identity or submission guard changed")
    snapshot = _board_dispatch({"operation": "observe", "target_id": request["target_id"],
                                 "expected_url": attempt["application_url"]}, helpers, attempt["application_url"])
    if snapshot.get("handoff"):
        return {"state": snapshot["handoff"], "reason": snapshot["reason"], "click_started": False}
    annotate_work_country(snapshot, packet.get("job", {}))
    records = packet.get("filled", [])
    inventory = packet.get("review_inventory", {}).get("fields", [])
    approved = {r["key"]: answer(r["value"], r.get("source")) for r in records}
    for r in records:
        if str(r["key"]).startswith("custom."):
            native = [field for field in snapshot["fields"] if _retained_question_matches(r, field, inventory)]
            question = native[0]["label"] if len(native) == 1 else r["question"]
            approved[r["key"]].update(question=question, field_ref=r["ref"])
            if r.get("user_override") is True:
                approved[r["key"]]["user_override"] = True
            if r.get("country_context"):
                approved[r["key"]]["country_context"] = r["country_context"]
    if (attempt.get("authorization_scope") == "one exact application explicitly approved in the local review portal"
            or attempt.get("review_binding")):
        for field in snapshot["fields"]:
            matches = [f for f in inventory if
                       (f.get("ref") == field.get("ref") or field.get("type") == "file" and f.get("type") == "file")
                       and normalize(f.get("question", "")) == normalize(field.get("label", ""))
                       and f.get("type") == field.get("type") and bool(f.get("required")) == bool(field.get("required"))
                       and f.get("calendar_format") == field.get("calendar_format")
                       and (f.get("description") or "") == (field.get("description") or "")
                       and bool(f.get("description_truncated")) == bool(field.get("description_truncated"))]
            if len(matches) != 1:
                return {"state": "waiting_review", "reason": "Application questions changed after review; review the updated form",
                        "click_started": False}
    from .native_question_context import enrich_sync
    probe_answers = _reviewed_probe_answers(request, packet, attempt, approved)
    enrich_sync(snapshot, packet.get("job", {}), probe_answers,
                lambda field, **payload: _board_dispatch({"operation": "describe", "field": field, **payload,
                    "target_id": request["target_id"], "expected_url": attempt["application_url"]},
                    helpers, attempt["application_url"]))
    if helpers["js"]("[...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length)"):
        return {"state": "waiting_login", "reason": "Website authentication is required", "click_started": False}
    documents = request.get("documents", {})
    checked, retained = [], []
    attached_labels = {normalize(f["label"]) for f in snapshot["fields"] if f["ref"].startswith("uploaded:")}
    for field in snapshot["fields"]:
        if field["type"] == "file" and not field["ref"].startswith("uploaded:") and normalize(field["label"]) in attached_labels:
            continue
        matches = [r for r in records if _retained_question_matches(r, field, inventory)]
        # Uploading changes the native ref into an attached-file virtual ref.
        if field["type"] == "file":
            key = key_for_field(field, approved)
            matches = [r for r in records if r.get("key") == key] if key else []
            if matches and all(r.get("value") == matches[0].get("value") and r.get("source") == matches[0].get("source")
                               and normalize(r.get("question", "")) == normalize(matches[0].get("question", "")) for r in matches):
                matches = matches[:1]
        state = _control_state(helpers, field, board)
        if len(matches) != 1:
            if field["required"] or (state and (state.get("selected") or state.get("value") or state.get("checked"))):
                return {"state": "waiting_input", "reason": "A fresh field lacks an exact approved answer", "click_started": False,
                        "missing": [{"question": field["label"], "ref": field["ref"], "type": field["type"],
                                     "required": field["required"], "choices": [o["label"] for o in field.get("options", [])],
                                     **({"country_context": field["country_context"]} if field.get("country_context") else {})}]}
            continue
        record = matches[0]
        from .known_answers import PROFILE_QUESTIONS
        # These projections additionally recheck their original source facts.
        # probe_answers contains only facts validated against this exact review
        # binding by _reviewed_probe_answers; retained values remain immutable.
        from .education_categories import projection
        from .observed_question import guarded
        key = key_for_field(field, probe_answers if normalize(field['label']) in PROFILE_QUESTIONS or projection(record)
                            or guarded(record['key'], record) and record['key'].startswith('custom.profile.') else approved)
        catalog = re.fullmatch(r"standing\.catalog\.(\d+)\.(school|major)", record["key"])
        catalog_ref = f"{'discipline' if catalog and catalog[2] == 'major' else 'school'}--{catalog[1]}" if catalog else None
        catalog_match = catalog and field["ref"] == catalog_ref
        replacing_document = field["type"] == "file" and request.get("require_receipts") is False
        if (not record.get("source") or (key != record["key"] and not catalog_match)
                or not state or (state.get("invalid") and not replacing_document)):
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
            expected_choice = value["choice"] if board != "greenhouse" and isinstance(value, dict) and set(value) == {"query", "choice"} else value
            if board == "greenhouse":
                from .cli_runtime import phone_country_value
                expected_choice = phone_country_value(expected_choice, field)
            choice_ref = "candidate-location" if field.get("widget") == "lever-location" else field["ref"]
            valid = option_matches(state["selected"], expected_choice, field_id=choice_ref, field_label=field["label"])
            if field["ref"] == "country" and expected_choice == "United States" and "iti__us" in state.get("countryCode", ""):
                valid = True
        elif kind in {"radio", "multiselect"} and board in {"ashby", "workable", "lever"}:
            values = value if isinstance(value, list) else [value]
            choices = [[o["label"] for o in field.get("options", []) if option_matches(
                o["label"], v, field_id=field["ref"], field_label=field["label"])] for v in values]
            valid = (all(len(options) == 1 for options in choices)
                     and (kind != "radio" or len(choices) == 1)
                     and sorted(state.get("selected", [])) == sorted(options[0] for options in choices))
        elif kind == "number":
            valid = _same_native_number(state["value"], value)
        elif kind in {"text", "email", "tel", "textarea", "url", "date"}:
            actual, expected = state["value"], str(value)
            if board == "ashby" and kind == "text" and field.get("calendar_format") == "MM/DD/YYYY":
                from .calendar_dates import retained_day
                valid = retained_day(actual, expected)
            elif kind == "tel":
                actual, expected = re.sub(r"\D", "", actual), re.sub(r"\D", "", expected)
                valid = actual == expected
            else:
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
    approved_refs = [r["ref"] for r in records if not str(r.get("ref", "")).startswith("uploaded:")]
    terminals = [b for b in snapshot["buttons"] if TERMINAL.fullmatch(normalize(b["label"]))
                 and _native_form_submit(helpers, b, approved_refs, board)]
    if len(terminals) != 1:
        return {"state": "waiting_review", "reason": "Final submission control is unavailable or ambiguous", "click_started": False}
    return {"fields": checked, "retained": retained, "button": terminals[0], "double_check_count": len(checked)}


def dispatch(request, helpers):
    if request.get("operation") == "observe_receipt":
        # Recording a completed historic click needs no current submit power.
        # This fixed path exposes no input, navigation or guard mutation.
        from .late_receipts import observe
        return observe(request, helpers)
    raw_cdp = helpers["cdp"]
    input_stalled = None
    def input_cdp(method, **params):
        nonlocal input_stalled
        # Browser Use's IPC timeout is separate from Chrome's protocol fields.
        # Preserve the controller's bounded input budget for wheel/hover/press.
        if method.startswith("Input.") and helpers.get("jhb_cdp_timeout"):
            params["_response_timeout"] = helpers["jhb_cdp_timeout"]
        try:
            return raw_cdp(method, **params)
        except (TimeoutError, RuntimeError) as exc:
            # Only a native hover/wheel timeout demonstrates stalled rendering.
            # Generic CLI failures and terminal presses never authorize a wake.
            if (method == "Input.dispatchMouseEvent" and params.get("type") in {"mouseMoved", "mouseWheel"}
                    and (isinstance(exc, TimeoutError) or "Input.dispatchMouseEvent timed out" in str(exc))):
                input_stalled = exc
            raise
    operation = request.get("operation")
    if operation not in {"locate", "check", "document", "submit"}:
        raise ValueError("Unsupported authorized submission operation")
    authority, attempt, packet = load_gate(request["authorization_path"], request["attempt_path"])
    identity = boards.job_identity(attempt["application_url"])
    board = identity[0]
    if operation == "locate":
        tabs = [t for t in helpers["list_tabs"]() if boards.job_identity(t["url"]) == identity]
        if len(tabs) != 1:
            return {"state": "waiting_review", "reason": "An exact existing draft tab must be unambiguous", "click_started": False}
        target = tabs[0]["targetId"]
        helpers["switch_tab"](target)
        if boards.job_identity(helpers["js"]("location.href")) != identity:
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
        return _board_dispatch({**request, "operation": "fill", "expected_url": attempt["application_url"]}, helpers, attempt["application_url"])
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
        if authority.get("require_independent_review") is True:
            if not _review_matches(request, attempt, second):
                return {"state": "waiting_review", "reason": "Independent review does not bind this fresh application audit", "click_started": False}
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
        review_digest = None
        def native_press(x, y):
            nonlocal clicked, review_digest
            from .. import config
            from .application_discard import action_lock
            with action_lock(config.ROOT, attempt["job_hash"]):
                # Geometry is now settled. Re-read authority immediately before
                # consuming the one-shot attempt; expiry cannot be bypassed by work.
                _, current, _ = load_gate(request["authorization_path"], request["attempt_path"])
                if boards.job_identity(helpers["js"]("location.href")) != boards.job_identity(current["application_url"]):
                    raise ValueError("Application identity changed before submission")
                final_check = _checks(request, helpers, packet, current)
                if final_check != second:
                    raise ValueError("Retained application changed during final click geometry")
                if authority.get("require_independent_review") is True and not _review_matches(request, current, final_check):
                    raise ValueError("Independent review changed before submission")
                if authority.get("require_independent_review") is True:
                    review_digest = hashlib.sha256(private_file(Path(request["attempt_path"]).parent / "independent-review.json").read_bytes()).hexdigest()
                current.update(runtime_click_started=True, click_started_at=datetime.now(timezone.utc).isoformat(),
                               double_check_count=2, checked_fields=second["double_check_count"])
                write_private(Path(request["attempt_path"]), current)
                clicked = True
                helpers["js"]("window.__jhbGuard=false")
                input_cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
                input_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
        def prime_pointer(x, y):
            # Clear a retained pointer press and hover only while submission is
            # guarded. IPC failure here cannot consume the one-shot click.
            if helpers["js"]("window.__jhbGuard===true") is not True:
                raise ValueError("Pointer priming requires the application guard")
            input_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
            input_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, buttons=0)
            helpers["wait"](0.05)
            # Hover can move or cover the button. Settle its exact DOM node
            # again, then re-read authority and retained fields before pressing.
            _settled_click(node["backendDOMNodeId"], input_cdp, helpers["wait"], native_press)
        def verify_guarded_owned_draft():
            _, current, _ = load_gate(request["authorization_path"], request["attempt_path"])
            owned = helpers["current_tab"]()
            identity = boards.job_identity(current["application_url"])
            if (owned.get("targetId") != request["target_id"]
                    or boards.job_identity(owned.get("url", "")) != identity
                    or boards.job_identity(helpers["js"]("location.href")) != identity
                    or helpers["js"]("window.__jhbGuard===true") is not True):
                raise ValueError("Owned application identity or guard changed during pointer recovery")
            if _checks(request, helpers, packet, current) != second:
                raise ValueError("Retained application changed during pointer recovery")
        try:
            try:
                _settled_click(node["backendDOMNodeId"], input_cdp, helpers["wait"], prime_pointer)
            except (TimeoutError, RuntimeError):
                if clicked or input_stalled is None or not callable(helpers.get("activate_tab")):
                    raise
                # A hidden renderer may stop servicing native input. Wake only
                # this verified, guarded draft once, before consuming the click.
                verify_guarded_owned_draft()
                helpers["activate_tab"](request["target_id"])
                helpers["wait"](0.5)
                verify_guarded_owned_draft()
                input_stalled = None
                _settled_click(node["backendDOMNodeId"], input_cdp, helpers["wait"], prime_pointer)
            for _ in range(40):
                helpers["wait"](0.25)
                body = helpers["js"]("document.body.innerText")
                current_url = helpers["js"]("location.href")
                visible_terminal = _visible_application_submit(helpers, [r["ref"] for r in packet.get("filled", [])
                                                                        if not str(r.get("ref", "")).startswith("uploaded:")], board)
                parsed = urlsplit(current_url)
                host_allowed = (parsed.scheme == "https" and not parsed.username and not parsed.password
                                and (parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
                                     if board == "greenhouse" else boards.job_identity(current_url) == identity))
                confirmation = CONFIRMATION.search(body)
                if host_allowed and confirmation and not CONFIRMATION.search(before_text) and not visible_terminal:
                    receipt = {"state": "submitted", "confirmed": True, "job_hash": attempt["job_hash"],
                               "application_url": attempt["application_url"], "url": attempt["application_url"], "observed_url": current_url,
                               "authorization_id": attempt["authorization_id"], "target_id": request["target_id"],
                               "confirmed_at": datetime.now(timezone.utc).isoformat(), "confirmation": confirmation[0],
                               "source": "Live Browser Use CLI success page", "check_count": 2,
                               "board": board, "document_sha256": {k: v["sha256"] for k, v in request["documents"].items()},
                               "resume_sha256": request["documents"]["documents.resume"]["sha256"],
                               "independent_review_sha256": review_digest,
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
