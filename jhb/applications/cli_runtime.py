"""Fixed actions executed inside Browser Use's CLI helper namespace.

The planner cannot supply scripts, URLs, coordinates, or values. Interactive
targets come from AX; DOM inspection handles file inputs and React's selected
values, which are not represented by the combobox input's empty value.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path

from .browser import GUARD_SCRIPT
from .booklet import normalize
from .planner import safe_next
from .queue import greenhouse_identity, is_greenhouse


def _settled_click(backend, cdp, wait, click_at_xy):
    """Click only after bounded, viewport-relative CDP geometry has settled."""
    cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
    previous, stable, wheel_attempts, obstructed = None, 0, 0, False
    for _ in range(20):
        # A completed scroll request does not guarantee a completed reflow or
        # animation triggered by that scroll. Observe several rendered frames.
        wait(0.05)
        quad = cdp("DOM.getBoxModel", backendNodeId=backend)["model"]["content"]
        if len(quad) != 8 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in quad):
            raise ValueError("Observed control has invalid click geometry")
        stable = stable+1 if previous is not None and max(abs(a-b) for a, b in zip(quad, previous)) <= 1 else 0
        previous = quad
        if stable < 2:
            continue
        metrics = cdp("Page.getLayoutMetrics")
        viewport = metrics.get("cssVisualViewport") or metrics["visualViewport"]
        x, y = sum(quad[0::2])/4, sum(quad[1::2])/4
        if not (0 < x < viewport["clientWidth"] and 0 < y < viewport["clientHeight"]):
            # A later layout expansion can move the settled control outside
            # the viewport. Retry the documented CDP scroll, never JS scrolling.
            cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
            previous, stable = None, 0
            continue
        final = cdp("DOM.getBoxModel", backendNodeId=backend)["model"]["content"]
        if len(final) != 8 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in final):
            raise ValueError("Observed control has invalid click geometry")
        if max(abs(a-b) for a, b in zip(final, quad)) > 1:
            previous, stable = None, 0
            continue
        if max(final[0::2]) <= min(final[0::2]) or max(final[1::2]) <= min(final[1::2]):
            raise ValueError("Observed control has no clickable area")
        x, y = sum(final[0::2])/4, sum(final[1::2])/4
        obj = cdp("DOM.resolveNode", backendNodeId=backend)["object"]["objectId"]
        try:
            hit = cdp("Runtime.callFunctionOn", objectId=obj, returnByValue=True,
                arguments=[{"value": value} for value in (x, y, viewport["clientWidth"], viewport["clientHeight"])],
                functionDeclaration=r"""function(x,y,width,height){
                  const doc=this.ownerDocument, view=doc.defaultView, blocker=doc.elementFromPoint(x,y);
                  const correct=!!blocker&&(blocker===this||this.contains(blocker)||
                    [...(this.labels||[])].some(label=>label===blocker||label.contains(blocker)));
                  if(correct)return {hit:true};
                  const candidates=[[width/2,height/2],[x,height/2],[width/2,height*.35],[width/2,height*.65]];
                  for(const [cx,cy] of candidates){
                    const e=doc.elementFromPoint(cx,cy);
                    if(!e||e===blocker||blocker?.contains(e)||e.closest('input,textarea,select,button,a,iframe,[contenteditable=true]'))continue;
                    let overlay=false;
                    for(let p=e;p;p=p.parentElement){
                      if(['fixed','sticky'].includes(view.getComputedStyle(p).position)){overlay=true;break;}
                    }
                    if(!overlay)return {hit:false,wheel:{x:cx,y:cy}};
                  }
                  return {hit:false};
                }""")["result"].get("value")
        finally:
            cdp("Runtime.releaseObject", objectId=obj)
        if not isinstance(hit, dict) or hit.get("hit") is not True:
            obstructed = True
            point = hit.get("wheel") if isinstance(hit, dict) else None
            if (wheel_attempts >= 3 or not isinstance(point, dict)
                    or not all(isinstance(point.get(k), (int, float)) and math.isfinite(point[k]) for k in ("x", "y"))
                    or not (0 < point["x"] < viewport["clientWidth"] and 0 < point["y"] < viewport["clientHeight"])):
                raise ValueError("Observed control is obstructed at its click position")
            delta = max(120, min(viewport["clientHeight"]*.4, abs(y-viewport["clientHeight"]*.45)))
            cdp("Input.dispatchMouseEvent", type="mouseWheel", x=point["x"], y=point["y"],
                deltaX=0, deltaY=delta if y >= viewport["clientHeight"]/2 else -delta)
            wheel_attempts += 1
            wait(0.1)
            previous, stable = None, 0
            continue
        click_at_xy(x, y)
        return
    raise ValueError("Observed control remains obstructed after scrolling" if obstructed else "Observed control did not settle in the viewport")

FIELD_DATA = r"""[...document.querySelectorAll('input,textarea,select')]
 .filter(e=>e.id && !e.disabled && (e.type==='file' ||
   (e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')))
 .filter(e=>!['hidden','password','submit','button','reset'].includes(e.type))
 .map(e=>({id:e.id,type:e.type,tag:e.tagName,role:e.getAttribute('role'),
   required:e.required||e.getAttribute('aria-required')==='true'||(e.type==='file'&&e.closest('.file-upload')?.getAttribute('aria-required')==='true'),
   label:((e.type==='file'?((e.closest('.file-upload')?.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.closest('.file-upload')?.querySelector('.upload-label')?.innerText):'')||e.getAttribute('aria-label')||[...(e.labels||[])].map(l=>l.innerText).join(' ')||
     (e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||'').trim(),
   description:e.getAttribute('description')||'',
   separate_phone_country:e.type==='tel' && !!e.closest('.iti') &&
     [...(e.closest('.phone-input')||e.closest('.iti')).querySelectorAll('input[role="combobox"],select,[role="combobox"]')]
       .some(c=>c!==e && !c.disabled && !c.matches('.iti__search-input,[id$="__search-input"]') &&
         c.getClientRects().length && getComputedStyle(c).visibility!=='hidden' &&
         (c.id==='country' || /^(?:phone[-_])?country(?:[-_]code)?$/i.test(c.name||'') ||
          /^(?:country|calling code|country code)$/i.test((c.getAttribute('aria-label')||[...(c.labels||[])].map(l=>l.innerText).join(' ')).trim()))),
   value:e.value,checked:e.checked,selected:e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||'',
   invalid:e.getAttribute('aria-invalid')==='true',
   options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value,disabled:o.disabled})):[]}))"""


def option_matches(label, value, *, field_id=""):
    """Closed, auditable display translations; no fuzzy screening answers."""
    label = normalize(label)
    if isinstance(value, bool):
        if field_id == "veteran_status" and value is False:
            return label in {"i am not a protected veteran", "i am not a veteran", "not a veteran"}
        return label in ({"yes", "true"} if value else {"no", "false", "no, i am not a veteran or active member"})
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
        _settled_click(backend, cdp, wait, helpers["click_at_xy"])

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

    def options_for(field):
        # Closed native selects also expose AX options. Only the opened
        # combobox's own listbox may supply candidates for matching/clicking.
        nodes = ax()
        by_id = {n["nodeId"]: n for n in nodes}
        controls = js("(()=>{const e=document.getElementById("+json.dumps(field["ref"])+
                      ");return (e?.getAttribute('aria-controls')||e?.getAttribute('aria-owns')||'').split(' ').filter(Boolean)})()")
        eligible = []
        for node in nodes:
            if node.get("role", {}).get("value") != "listbox" or node.get("ignored"):
                continue
            if controls:
                if identifier(node) in controls:
                    eligible.append(node["nodeId"])
            elif node.get("backendDOMNodeId"):
                obj = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
                same = cdp("Runtime.callFunctionOn", objectId=obj,
                           functionDeclaration="function(){const e=document.getElementById("+json.dumps(field["ref"])+
                           ");const p=e?.closest('.select')||e?.closest('.select__container')||e?.closest('.select__value-container');return !!p?.contains(this)}",
                           returnByValue=True)["result"].get("value")
                if same:
                    eligible.append(node["nodeId"])
        if len(eligible) != 1:
            return []
        result = []
        for node in nodes:
            if node.get("role", {}).get("value") != "option" or node.get("ignored"):
                continue
            parent, seen = node.get("parentId"), set()
            while parent in by_id and parent not in seen:
                if parent == eligible[0]:
                    result.append(node)
                    break
                seen.add(parent)
                parent = by_id[parent].get("parentId")
        return result

    def control_value(ref):
        return js("(()=>{const e=document.getElementById("+json.dumps(ref)+
                  ");return e?{value:e.value,checked:e.checked,selected:e.tagName==='SELECT'?e.selectedOptions[0]?.label||'':e.closest('.select__value-container')?.querySelector('.select__single-value')?.innerText||[...(e.closest('.select__value-container')?.querySelectorAll('.select__multi-value__label')||[])].map(e=>e.innerText).join(', ')||'',countryCode:e.closest('.select__value-container')?.querySelector('.iti__flag')?.className||'',invalid:e.getAttribute('aria-invalid')==='true'}:null})()")

    def keypress(key, code=None):
        virtual = {"Home": 36, "End": 35, "ArrowDown": 40, "ArrowUp": 38, "Escape": 27}.get(key, 0)
        cdp("Input.dispatchKeyEvent", type="keyDown", key=key, code=code or key, windowsVirtualKeyCode=virtual)
        cdp("Input.dispatchKeyEvent", type="keyUp", key=key, code=code or key, windowsVirtualKeyCode=virtual)

    def upload_container(label):
        # Inspect each filename only inside its own labeled upload control.
        return "(()=>{const wanted="+json.dumps(normalize(label))+";return [...document.querySelectorAll('.file-upload')].find(e=>{const l=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.querySelector('.upload-label')?.innerText||e.querySelector('label')?.innerText||e.innerText.split('\\n')[0]||'';return l.trim().replace(/[ *]+$/,'').toLowerCase()===wanted})})()"

    def upload_state(label):
        return js("(()=>{const e="+upload_container(label)+";return e?{filename:e.querySelector('.file-upload__filename p')?.innerText||'',receipt:e.__jhbUploadReceipt||null}:null})()")

    def mark_upload(label):
        receipt = uuid.uuid4().hex
        js("(()=>{const e="+upload_container(label)+";if(!e)return; e.__jhbUploadReceipt="+json.dumps(receipt)+";"
           "e.__jhbUploadWatcher?.disconnect();"
           "e.__jhbUploadWatcher=new MutationObserver(()=>{delete e.__jhbUploadReceipt;e.__jhbUploadWatcher.disconnect()});"
           "e.__jhbUploadWatcher.observe(e,{childList:true,subtree:true,characterData:true});"
           "e.addEventListener('change',()=>{delete e.__jhbUploadReceipt},{once:true,capture:true})})()")
        return receipt

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
            for _ in range(10):
                wait(0.2)
                if row_count() == current+1:
                    break
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
            label = item["label"] or item["id"]
            if kind == "checkbox" and normalize(label) in {"accept", "agree", "yes", "no"} and item.get("description"):
                label = item["description"] + " (" + label + ")"
            fields.append({"ref": item["id"], "label": label,
                           "type": kind, "required": item["required"], "options": item["options"],
                           **({"separate_phone_country": item["separate_phone_country"]} if kind == "tel" else {})})
        for upload in js("[...document.querySelectorAll('.file-upload')].filter(e=>e.querySelector('.file-upload__filename')).map(e=>({label:((e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.querySelector('.upload-label')?.innerText||e.innerText.split('\\n')[0]).trim(),filename:e.querySelector('.file-upload__filename p')?.innerText||'',required:e.getAttribute('aria-required')==='true'}))"):
            if upload["label"] in {"Resume/CV", "Resume", "Cover Letter"}:
                fields.append({"ref": "uploaded:"+upload["label"], "label": upload["label"],
                               "type": "file", "required": upload["required"], "options": []})
        buttons = [{"ref": str(n["backendDOMNodeId"]), "label": n.get("name", {}).get("value", "")}
                   for n in nodes if n.get("role", {}).get("value") == "button" and n.get("backendDOMNodeId")]
        from .salary import advertised_ranges
        return {"url": js("location.href"), "title": js("document.title"), "fields": fields, "buttons": buttons,
                "salary_ranges": advertised_ranges(text)}

    if operation == "describe":
        field = request["field"]
        ref, kind = field["ref"], field["type"]
        if kind == "select":
            item = next((item for item in js(FIELD_DATA) if item["id"] == ref), None)
            if not item:
                raise ValueError("Observed field is no longer available")
            labels = [o["label"] for o in item["options"] if not o["disabled"] and o["value"] != ""]
            return {"choices": labels[:50], "truncated": len(labels)>50, "type": kind}
        if kind == "checkbox":
            return {"choices": [True, False], "truncated": False, "type": kind}
        if kind != "combobox":
            return {"choices": [], "truncated": False, "type": kind}
        if ref.startswith("school--"):
            return {"choices": [], "truncated": True, "type": kind, "reason": "Institution catalog requires an exact user-supplied institution"}
        before = control_value(ref)
        if before and before["value"] and not before["selected"]:
            return {"choices": [], "truncated": False, "type": kind, "reason": "Preserved uncommitted draft query"}
        backend = find(field)
        try:
            click(backend)
            wait(0.2)
            labels = [n.get("name", {}).get("value", "") for n in options_for(field)]
            return {"choices": list(dict.fromkeys(labels))[:50], "truncated": len(labels)>50, "type": kind}
        finally:
            keypress("Escape")

    if operation == "fill":
        field, value = request["field"], request["value"]
        ref, kind = field["ref"], field["type"]
        if kind == "file":
            path = Path(str(value))
            if not path.is_file() or path.suffix.casefold() != ".pdf":
                raise ValueError("Approved PDF is unavailable")
            previous = upload_state(field["label"])
            if (request.get("upload_receipt") and previous and
                    previous["receipt"] == request["upload_receipt"] and previous["filename"] == path.name):
                return {"verified": True, "filename": path.name, "upload_receipt": previous["receipt"], "cached": True}
            if ref.startswith("uploaded:"):
                # A filename alone cannot identify SDE vs ML resumes with the same
                # basename. Replace it from the approved role-specific source.
                remove = [n for n in ax() if n.get("role", {}).get("value") == "button"
                          and n.get("name", {}).get("value") == "Remove file"]
                eligible = []
                for node in remove:
                    obj = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
                    inside = cdp("Runtime.callFunctionOn", objectId=obj,
                                 functionDeclaration="function(){return this.closest('.file-upload')==="+upload_container(field["label"])+"}",
                                 returnByValue=True)["result"].get("value")
                    if inside:
                        eligible.append(node)
                if len(eligible) != 1:
                    raise ValueError("Matching uploaded-file removal control is ambiguous")
                click(eligible[0]["backendDOMNodeId"])
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
                observed = upload_state(field["label"])
                if observed and observed["filename"] == path.name:
                    return {"verified": True, "filename": path.name, "upload_receipt": mark_upload(field["label"])}
                wait(0.25)
            raise ValueError("Uploaded filename did not appear in the form")
        backend = find(field)
        if kind == "checkbox":
            if not isinstance(value, bool):
                raise ValueError("Checkbox answers require an explicit boolean")
            before = control_value(ref)
            if before is None:
                raise ValueError("Observed checkbox is no longer available")
            if before["checked"] != value:
                click(backend)
                wait(0.15)
            after = control_value(ref)
            if not after or after["checked"] != value or after["invalid"]:
                raise ValueError("Checkbox did not retain the approved answer")
            return {"verified": True, "checked": value}
        if kind == "select":
            item = next((item for item in js(FIELD_DATA) if item["id"] == ref), None)
            options = [o for o in (item or {}).get("options", []) if not o["disabled"]]
            matches = [i for i, o in enumerate(options) if option_matches(o["label"], value, field_id=ref)]
            if len(matches) != 1:
                raise ValueError("Stored answer does not uniquely match a native select option")
            selected = options[matches[0]]
            before = control_value(ref)
            if not before or before["value"] != selected["value"] or before["invalid"]:
                cdp("DOM.focus", backendNodeId=backend)
                keypress("Home")
                for _ in range(matches[0]):
                    keypress("ArrowDown")
                wait(0.15)
                interim = control_value(ref)
                if not interim or interim["value"] != selected["value"]:
                    # macOS native selects can ignore navigation keys while
                    # accepting typeahead. Clear its bounded typeahead window,
                    # then send actual keyboard text for the exact option.
                    wait(1.05)
                    for char in selected["label"]:
                        virtual = ord(char.upper()) if len(char.upper()) == 1 and char.isascii() else 0
                        cdp("Input.dispatchKeyEvent", type="keyDown", key=char, text=char,
                            unmodifiedText=char, windowsVirtualKeyCode=virtual)
                        cdp("Input.dispatchKeyEvent", type="keyUp", key=char, windowsVirtualKeyCode=virtual)
                    wait(0.15)
            after = control_value(ref)
            if not after or after["value"] != selected["value"] or after["invalid"]:
                raise ValueError("Native select did not retain the approved answer")
            return {"verified": True, "selected": selected["label"]}
        if kind == "combobox":
            before = control_value(ref)
            if ref == "country" and value == "United States" and before and "iti__us" in before["countryCode"]:
                return {"verified": True, "selected": "United States (+1)"}
            if before and option_matches(before["selected"], value, field_id=ref) and not before["invalid"]:
                return {"verified": True}
            click(backend)
            wait(0.15)
            options = options_for(field)
            match = [n for n in options if option_matches(n.get("name", {}).get("value", ""), value, field_id=ref)]
            if not match:
                query = ("Yes" if value else "No") if isinstance(value, bool) else (str(value).split(",")[0] if ref == "candidate-location" else str(value))
                queries = [query]
                if ref.startswith("school--"):
                    queries.append(str(value).split()[-1])
                for query in dict.fromkeys(queries):
                    type_text(backend, query)
                    for _ in range(12):
                        wait(0.25)
                        options = options_for(field)
                        match = [n for n in options if option_matches(n.get("name", {}).get("value", ""), value, field_id=ref)]
                        if match:
                            break
                    if match:
                        break
            if len(match) != 1:
                type_text(backend, "")
                cdp("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
                cdp("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
                raise ValueError("Stored answer is absent from dropdown options" if not match else "Stored answer matches multiple dropdown options")
            label = match[0].get("name", {}).get("value", "")
            click(match[0]["backendDOMNodeId"])
            wait(0.2)
            after = control_value(ref)
            if ref == "country" and value == "United States" and after and "iti__us" in after["countryCode"] and not after["invalid"]:
                return {"verified": True, "selected": "United States (+1)"}
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
