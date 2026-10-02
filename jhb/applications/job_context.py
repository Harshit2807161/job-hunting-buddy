"""Public job location metadata for conservative jurisdiction context."""
from __future__ import annotations

import html
import json
import re
from urllib.request import Request, urlopen

from .queue import greenhouse_identity

MAX_BYTES = 1024 * 1024
_COUNTRIES = {
    "United States": r"\bunited states(?: of america)?\b|\busa\b|(?<![\w.])u\.s\.?(?![\w.])",
    "Canada": r"\bcanada\b",
    "United Kingdom": r"\bunited kingdom\b|\buk\b",
}


def country_context(text):
    """Recognize an explicit single supported country; city-only means unknown.

    A separator can describe a second unsupported region (e.g. Canada/Mexico).
    Reject such labels rather than selecting whichever supported name matches.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    found = [name for name, pattern in _COUNTRIES.items() if re.search(pattern, text, re.I)]
    if len(found) != 1:
        return None
    if re.search(r"[/|;]|\s(?:and|or|&)\s", text, re.I):
        return None
    # Bare US is deliberately not an affirmative match. Its presence beside
    # another country's name is still evidence of multiple jurisdictions.
    if found[0] != "United States" and re.search(r"\bus\b", text, re.I):
        return None
    if re.search(r"\b(?:except|excluding|not|outside)\b", text, re.I):
        return None
    return found[0]


def fetch(joburl, *, opener=None):
    """GET a validated global Greenhouse job; failures provide no metadata.

    No account, environment credentials, cookies, or API tokens are used.
    The injectable opener receives a stdlib Request and timeout=10.
    """
    identity = greenhouse_identity(joburl)
    if not identity or identity[0] != "global":
        return {}
    _, board, job_id = identity
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    request = Request(api_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with (opener or urlopen)(request, timeout=10) as response:
            if getattr(response, "geturl", lambda: api_url)() != api_url:
                return {}
            raw = response.read(MAX_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or str(data.get("id")) != job_id:
            return {}
        location = data.get("location", {})
        if not isinstance(location, dict):
            return {}
        location = location.get("name", "")
        name = data.get("title", data.get("name", ""))
        if not isinstance(location, str) or not isinstance(name, str):
            return {}
        result = {"location": location.strip(), "name": name.strip(),
                  "country_context": country_context(location), "source_url": api_url}
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            from .salary import advertised_ranges
            text = re.sub(r"<[^>]*>", " ", html.unescape(html.unescape(content)))
            result["advertised_salary_ranges"] = advertised_ranges(text)
        return result
    except Exception:
        # This enrichment cannot block application question handoffs. Never
        # print response content, request headers, or exception messages.
        return {}
