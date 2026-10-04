"""Exact-job local LinkedIn navigation, using the registered Browser Use CLI."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .cli_runtime import _settled_click


def linkedin_id(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"www.linkedin.com", "linkedin.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            return None
        match = re.fullmatch(r"/jobs/view/(?:[A-Za-z0-9%-]+-)?(\d+)/?", parsed.path)
        return match[1] if match else None
    except (TypeError, ValueError):
        return None


def outbound(url):
    try:
        parsed = urlsplit(url)
        if parsed.hostname in {"www.linkedin.com", "linkedin.com"} and parsed.path == "/safety/go/":
            values = parse_qs(parsed.query).get("url", [])
            if len(values) != 1:
                return None
            url = values[0]
            parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname in {"www.linkedin.com", "linkedin.com", "localhost", "127.0.0.1"}):
            return None
        return url
    except (TypeError, ValueError):
        return None


def dispatch(request, helpers):
    if request.get("operation") != "resolve":
        raise ValueError("LinkedIn source navigation does not fill or submit applications")
    job_id = linkedin_id(request.get("approved_url"))
    if not job_id:
        raise ValueError("An exact LinkedIn job is required")
    cdp, js, wait = (helpers[key] for key in ("cdp", "js", "wait"))
    tabs = [tab for tab in helpers["list_tabs"]() if linkedin_id(tab.get("url")) == job_id]
    if len(tabs) > 1:
        return {"state": "blocked", "board_type": "linkedin", "handoff": "waiting_login",
                "reason": "Multiple exact LinkedIn job tabs need disambiguation", "evidence": []}
    if tabs:
        helpers["switch_tab"](tabs[0]["targetId"])
    else:
        helpers["new_tab"]("https://www.linkedin.com/jobs/view/" + job_id + "/")
        helpers["wait_for_load"]()
    wait(0.5)
    if linkedin_id(js("location.href")) != job_id:
        return {"state": "blocked", "board_type": "linkedin", "handoff": "waiting_login",
                "reason": "LinkedIn requires authentication or verification", "evidence": []}
    source_target = helpers["current_tab"]()["targetId"]
    source_url = js("location.href")
    nodes = cdp("Accessibility.getFullAXTree")["nodes"]
    controls = [node for node in nodes if not node.get("ignored") and node.get("backendDOMNodeId")
                and node.get("role", {}).get("value") in {"button", "link"}
                and re.fullmatch(r"(?:Apply on company website|Apply|Easy Apply)(?: to .*)?",
                                 node.get("name", {}).get("value", ""), re.I)]
    if len(controls) != 1:
        return {"state": "ambiguous", "board_type": "linkedin", "reason": "Exact job Apply control is unavailable or ambiguous",
                "evidence": [{"url": js("location.href"), "apply_controls": len(controls)}]}
    node = controls[0]
    evidence = [{"url": js("location.href"), "operation": "observed_authenticated_apply",
                 "label": node.get("name", {}).get("value", "")}]
    if re.search(r"easy apply", node.get("name", {}).get("value", ""), re.I):
        return {"state": "not_greenhouse", "board_type": "linkedin_easy_apply",
                "application_url": "https://www.linkedin.com/jobs/view/" + job_id + "/",
                "reason": "Verified LinkedIn Easy Apply job", "evidence": evidence}
    obj = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
    try:
        href = cdp("Runtime.callFunctionOn", objectId=obj,
                   functionDeclaration="function(){return this.href||null}", returnByValue=True)["result"].get("value")
    finally:
        cdp("Runtime.releaseObject", objectId=obj)
    expected = outbound(href) if href else None
    from .boards import job_identity
    expected_identity = job_identity(expected) if expected else None
    reusable = [tab for tab in helpers["list_tabs"]()
                if expected_identity and job_identity(tab.get("url")) == expected_identity]
    if len(reusable) == 1:
        helpers["switch_tab"](reusable[0]["targetId"])
        final = js("location.href")
        if job_identity(final) == expected_identity:
            evidence.append({"url": final, "operation": "reused_observed_apply_destination"})
            return {"state": "destination", "application_url": final, "evidence": evidence,
                    "tab_navigation": {"native_apply_clicked": False, "source_target_id": source_target,
                        "source_url": source_url, "destination_target_id": reusable[0]["targetId"],
                        "before_target_ids": [t["targetId"] for t in helpers["list_tabs"]()],
                        "expected_identity": list(expected_identity)}}
    before = {tab["targetId"] for tab in helpers["list_tabs"]()}
    # Preserve all page IDs, including filtered startup placeholders. Ownership
    # requires a genuinely new popup from this exact source's native action.
    before.update(t["targetId"] for t in cdp("Target.getTargets").get("targetInfos", []) if t.get("type") == "page")
    if helpers.get("jhb_before_apply_click"):
        helpers["jhb_before_apply_click"]()
    _settled_click(node["backendDOMNodeId"], cdp, wait, helpers["click_at_xy"])
    if helpers.get("jhb_after_apply_click"):
        helpers["jhb_after_apply_click"]()
    found = None
    for _ in range(20):
        wait(0.25)
        changed = [tab for tab in helpers["list_tabs"]() if tab["targetId"] not in before]
        external = [tab for tab in changed if outbound(tab.get("url"))]
        if len(external) == 1:
            found = external[0]
            break
        current = helpers["current_tab"]()
        if outbound(current.get("url")):
            found = current
            break
    if not found:
        # A native click that produced no navigation demonstrates stalled
        # background input. Wake only this exact owned job, then retry once.
        if linkedin_id(js("location.href")) != job_id:
            return {"state": "ambiguous", "board_type": "linkedin", "reason": "Job changed during Apply navigation", "evidence": evidence}
        helpers["activate_tab"](helpers["current_tab"]()["targetId"])
        wait(0.2)
        if helpers.get("jhb_before_apply_click"):
            helpers["jhb_before_apply_click"]()
        _settled_click(node["backendDOMNodeId"], cdp, wait, helpers["click_at_xy"])
        if helpers.get("jhb_after_apply_click"):
            helpers["jhb_after_apply_click"]()
        for _ in range(40):
            wait(0.25)
            changed = [tab for tab in helpers["list_tabs"]() if tab["targetId"] not in before]
            external = [tab for tab in changed if outbound(tab.get("url"))]
            if len(external) == 1:
                found = external[0]
                break
        if not found:
            return {"state": "ambiguous", "board_type": "linkedin", "reason": "Apply destination did not finish navigation", "evidence": evidence}
    helpers["switch_tab"](found["targetId"])
    helpers["wait_for_load"]()
    final = js("location.href")
    for _ in range(40):
        if outbound(final) and urlsplit(final).hostname not in {"www.linkedin.com", "linkedin.com"}:
            break
        wait(0.25)
        final = js("location.href")
    if not outbound(final) or urlsplit(final).hostname in {"www.linkedin.com", "linkedin.com"}:
        return {"state": "ambiguous", "board_type": "linkedin", "reason": "Apply destination is not a public HTTPS page",
                "evidence": evidence}
    evidence.append({"url": final, "operation": "observed_apply_destination", "expected_href": expected})
    return {"state": "destination", "application_url": final, "evidence": evidence,
            "tab_navigation": {"native_apply_clicked": True, "source_target_id": source_target,
                "source_url": source_url, "destination_target_id": found["targetId"],
                "before_target_ids": sorted(before), "expected_identity": list(expected_identity) if expected_identity else None}}
