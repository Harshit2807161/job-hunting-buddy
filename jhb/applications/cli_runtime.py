"""Fixed actions executed inside Browser Use's CLI helper namespace.

The planner cannot supply scripts, URLs, coordinates, or values. Interactive
targets come from AX; DOM inspection handles file inputs and React's selected
values, which are not represented by the combobox input's empty value.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .browser import GUARD_SCRIPT
from .booklet import normalize
from .planner import safe_next
from .queue import greenhouse_identity, is_greenhouse

FIELD_DATA = r"""[...document.querySelectorAll('input,textarea,select')]
 .filter(e=>e.id && !e.disabled && (e.type==='file' ||
   (e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')))
 .filter(e=>!['hidden','password','submit','button','reset'].includes(e.type))
 .map(e=>({id:e.id,type:e.type,tag:e.tagName,role:e.getAttribute('role'),
   required:e.required||e.getAttribute('aria-required')==='true',
   label:(e.getAttribute('aria-label')||[...(e.labels||[])].map(l=>l.innerText).join(' ')||
     (e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||'').trim(),
   value:e.value,selected:e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||'',
   invalid:e.getAttribute('aria-invalid')==='true',
   options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value})):[]}))"""


def option_matches(label, value, *, field_id=""):
    """Closed, auditable display translations; no fuzzy screening answers."""
    label = normalize(label)
    if isinstance(value, bool):
        if field_id == "veteran_status" and value is False:
            return label in {"i am not a protected veteran", "i am not a veteran", "not a veteran"}
        return label in ({"yes", "true"} if value else {"no", "false"})
    expected = normalize(str(value))
    if label == expected:
        return True
    if field_id.startswith("school--"):
        return re.sub(r"[^\w]", "", label) == re.sub(r"[^\w]", "", expected)
    if expected == "ca" and label == "california":
        return True
    if expected == "united states" and label in {"united states of america", "united states +1", "united states (+1)"}:
        return True
    # Location providers expand the explicitly supplied state/country components.
    if field_id == "candidate-location":
        parts = [part.strip() for part in expected.split(",")]
        if len(parts) in {2, 3} and parts[1] == "ca" and (len(parts) == 2 or parts[2] in {"usa", "united states"}):
            city = parts[0]
            return label in {city+", california, united states", city+", ca, united states", city+", ca, usa"}
    if expected == "male" and label == "man":
        return True
    if expected == "master of science" and label in {"master's degree", "master's", "masters"}:
        return True
    if expected == "bachelor of science" and label in {"bachelor's degree", "bachelor's", "bachelors"}:
        return True
    return False


def dispatch(request, helpers):
    cdp, js, wait = helpers["cdp"], helpers["js"], helpers["wait"]

    def ax():
        return cdp("Accessibility.getFullAXTree")["nodes"]

    def click(backend):
        cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
        q = cdp("DOM.getBoxModel", backendNodeId=backend)["model"]["content"]
        helpers["click_at_xy"](sum(q[0::2])/4, sum(q[1::2])/4)

    def identifier(node):
        attrs = cdp("DOM.describeNode", backendNodeId=node["backendDOMNodeId"])["node"].get("attributes", [])
        return dict(zip(attrs[0::2], attrs[1::2])).get("id")

    def find(field):
        for node in ax():
            if node.get("role", {}).get("value") not in {"textbox", "combobox", "checkbox", "radio"}:
                continue
            if identifier(node) == field["ref"]:
                return node["backendDOMNodeId"]
        raise ValueError("Observed field is no longer available")

    def control_value(ref):
        return js("(()=>{const e=document.getElementById("+json.dumps(ref)+
                  ");return e?{value:e.value,selected:e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||'',countryCode:e.closest('.select__value-container')?.querySelector('.iti__flag')?.className||'',invalid:e.getAttribute('aria-invalid')==='true'}:null})()")

    def type_text(backend, value):
        cdp("DOM.focus", backendNodeId=backend)
        cdp("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=4,
            commands=["selectAll"])
        cdp("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=0)
        cdp("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace")
        cdp("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace")
        cdp("Input.insertText", text=str(value))
        wait(0.15)

    operation = request["operation"]
    if request.get("target_id"):
        helpers["switch_tab"](request["target_id"])
    if operation == "open":
        url = request["url"]
        identity = greenhouse_identity(url)
        if identity is None:
            raise ValueError("Only HTTPS Greenhouse-hosted job forms are supported")
        matching = [t for t in helpers["list_tabs"]() if greenhouse_identity(t["url"]) == identity]
        if matching:
            helpers["switch_tab"](matching[0]["targetId"])
        else:
            helpers["new_tab"](url)
            helpers["wait_for_load"]()
            wait(1)
        cdp("Page.addScriptToEvaluateOnNewDocument", source=GUARD_SCRIPT)
        js(GUARD_SCRIPT)
        return {"url": js("location.href"), "reused_tab": bool(matching), "guarded": True,
                "target_id": helpers["current_tab"]()["targetId"]}

    if not is_greenhouse(js("location.href")):
        return {"handoff": "unsupported", "reason": "Current page is outside the Greenhouse v1 scope"}
    if operation == "education":
        count = request["count"]
        if not isinstance(count, int) or not 1 <= count <= 5:
            raise ValueError("Education preparation supports at most five approved records")
        row_count = lambda: js("document.querySelectorAll('.education--container input[id^=school--]').length")
        current = row_count()
        if not current:
            return {"supported": False, "reason": "No supported education section"}
        for _ in range(max(0, count-current)):
            buttons = [n for n in ax() if n.get("role", {}).get("value") == "button"
                       and n.get("name", {}).get("value") == "Add another"]
            eligible = []
            for button in buttons:
                obj = cdp("DOM.resolveNode", backendNodeId=button["backendDOMNodeId"])["object"]["objectId"]
                inside = cdp("Runtime.callFunctionOn", objectId=obj,
                             functionDeclaration="function(){return this.parentElement.classList.contains('education--container')}",
                             returnByValue=True)["result"].get("value")
                if inside:
                    eligible.append(button)
            if len(eligible) != 1:
                raise ValueError("Education add-record control is ambiguous")
            click(eligible[0]["backendDOMNodeId"])
            wait(0.25)
            if row_count() != current+1:
                raise ValueError("Another education row did not appear")
            current += 1
        return {"supported": True, "rows": current}
    if operation == "observe":
        nodes = ax()
        text = "\n".join(n.get("name", {}).get("value", "") for n in nodes
                         if n.get("role", {}).get("value") == "StaticText")
        # Invisible reCAPTCHA response inputs are not a verification challenge.
        if js("[...document.querySelectorAll('iframe')].some(e=>/recaptcha|hcaptcha|challenge/i.test(e.src)&&e.getClientRects().length&&e.getBoundingClientRect().height>90)"):
            return {"handoff": "waiting_captcha", "reason": "A visible verification challenge requires a handoff"}
        if re.search(r"verify (?:you are human|your email)|verification code|check your inbox|access denied", text, re.I):
            return {"handoff": "waiting_login", "reason": "Website verification is required"}
        fields = []
        for item in js(FIELD_DATA):
            if not item["label"] and item["type"] != "file":
                continue
            kind = "combobox" if item["role"] == "combobox" else ("select" if item["tag"] == "SELECT" else item["type"])
            fields.append({"ref": item["id"], "label": item["label"] or item["id"],
                           "type": kind, "required": item["required"], "options": item["options"]})
        for upload in js("[...document.querySelectorAll('.file-upload')].filter(e=>e.querySelector('.file-upload__filename')).map(e=>({label:e.innerText.split('\\n')[0].trim(),filename:e.querySelector('.file-upload__filename p')?.innerText||''}))"):
            if upload["label"] in {"Resume/CV", "Resume", "Cover Letter"}:
                fields.append({"ref": "uploaded:"+upload["label"], "label": upload["label"],
                               "type": "file", "required": upload["label"] != "Cover Letter", "options": []})
        buttons = [{"ref": str(n["backendDOMNodeId"]), "label": n.get("name", {}).get("value", "")}
                   for n in nodes if n.get("role", {}).get("value") == "button" and n.get("backendDOMNodeId")]
        return {"url": js("location.href"), "title": js("document.title"), "fields": fields, "buttons": buttons}

    if operation == "fill":
        field, value = request["field"], request["value"]
        ref, kind = field["ref"], field["type"]
        if kind == "file":
            path = Path(str(value))
            if not path.is_file() or path.suffix.casefold() != ".pdf":
                raise ValueError("Approved PDF is unavailable")
            if ref.startswith("uploaded:"):
                # A filename alone cannot identify SDE vs ML resumes with the same
                # basename. Replace it from the approved role-specific source.
                remove = [n for n in ax() if n.get("role", {}).get("value") == "button"
                          and n.get("name", {}).get("value") == "Remove file"]
                if len(remove) != 1:
                    raise ValueError("Multiple uploaded files need an explicit review")
                click(remove[0]["backendDOMNodeId"])
                wait(0.2)
                ref = "cover_letter" if field["label"] == "Cover Letter" else "resume"
            # Hidden upload input is absent from AX: use the documented DOM fallback.
            doc = cdp("DOM.getDocument")["root"]["nodeId"]
            node = cdp("DOM.querySelector", nodeId=doc, selector="[id="+json.dumps(ref)+"]")["nodeId"]
            if not node:
                raise ValueError("Upload input is unavailable")
            cdp("DOM.setFileInputFiles", nodeId=node, files=[str(path.resolve())])
            wait(1)
            for _ in range(20):
                if path.name in js("document.body.innerText"):
                    return {"verified": True, "filename": path.name}
                wait(0.25)
            raise ValueError("Uploaded filename did not appear in the form")
        backend = find(field)
        if kind == "combobox":
            before = control_value(ref)
            if ref == "country" and value == "United States" and before and "iti__us" in before["countryCode"]:
                return {"verified": True, "selected": "United States (+1)"}
            if before and option_matches(before["selected"], value, field_id=ref) and not before["invalid"]:
                return {"verified": True}
            click(backend)
            wait(0.15)
            options = [n for n in ax() if n.get("role", {}).get("value") == "option"]
            match = [n for n in options if option_matches(n.get("name", {}).get("value", ""), value, field_id=ref)]
            if not match:
                query = str(value).split(",")[0] if ref == "candidate-location" else str(value)
                queries = [query]
                if ref.startswith("school--"):
                    queries.append(str(value).split()[-1])
                for query in dict.fromkeys(queries):
                    type_text(backend, query)
                    for _ in range(12):
                        wait(0.25)
                        options = [n for n in ax() if n.get("role", {}).get("value") == "option"]
                        match = [n for n in options if option_matches(n.get("name", {}).get("value", ""), value, field_id=ref)]
                        if match:
                            break
                    if match:
                        break
            if len(match) != 1:
                type_text(backend, "")
                cdp("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
                cdp("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
                raise ValueError("Stored answer does not uniquely match a dropdown option")
            label = match[0].get("name", {}).get("value", "")
            click(match[0]["backendDOMNodeId"])
            wait(0.2)
            after = control_value(ref)
            if not after or normalize(after["selected"]) != normalize(label) or after["invalid"]:
                raise ValueError("Dropdown did not retain the selected answer")
            return {"verified": True, "selected": label}
        if kind in {"text", "email", "tel", "textarea", "url", "number", "date"}:
            type_text(backend, value)
            after = control_value(ref)
            actual = after["value"] if after else None
            expected = str(value)
            if kind == "tel":
                actual = re.sub(r"\D", "", actual or "")
                expected = re.sub(r"\D", "", expected)
            if actual != expected or after["invalid"]:
                raise ValueError("Form did not retain the exact answer")
            return {"verified": True}
        raise ValueError("Unsupported live form control")

    if operation == "next":
        button = request["button"]
        if not safe_next(button):
            raise ValueError("Terminal submission is prohibited")
        node = next((n for n in ax() if str(n.get("backendDOMNodeId")) == button["ref"]
                     and n.get("name", {}).get("value") == button["label"]
                     and n.get("role", {}).get("value") == "button"), None)
        if not node:
            raise ValueError("Observed continuation button is unavailable")
        click(node["backendDOMNodeId"])
        wait(0.3)
        return {"continued": True}
    if operation == "screenshot":
        path = Path(request["path"])
        helpers["capture_screenshot"](str(path))
        path.chmod(0o600)
        return {"screenshot": str(path)}
    if operation == "takeover":
        if request.get("acknowledgement") != "TAKE OVER":
            raise ValueError("Explicit human takeover acknowledgement is required")
        js("window.__jhbGuard=false")
        return {"human_control": True, "submitted": False}
    raise ValueError("Unsupported CLI operation")
