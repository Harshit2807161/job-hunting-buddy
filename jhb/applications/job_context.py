"""Public job location metadata for conservative jurisdiction context."""
from __future__ import annotations

import html
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

from .queue import greenhouse_identity
from . import boards

MAX_BYTES = 1024 * 1024
_COUNTRIES = {
    "United States": r"\bunited states(?: of america)?\b|\busa\b|(?<![\w.])u\.s\.?(?![\w.])",
    "Canada": r"\bcanada\b",
    "United Kingdom": r"\bunited kingdom\b|\buk\b",
}


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.records, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            try:
                self.records.append(json.loads("".join(self.current)))
            except ValueError:
                pass
            self.current = None


def _job_records(value):
    if isinstance(value, list):
        for item in value:
            yield from _job_records(item)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            yield value
        yield from _job_records(value.get("@graph"))


def _metadata_country(record):
    countries = []
    locations = record.get("jobLocation", [])
    for location in locations if isinstance(locations, list) else [locations]:
        address = location.get("address", {}) if isinstance(location, dict) else {}
        country = address.get("addressCountry") if isinstance(address, dict) else None
        if isinstance(country, dict):
            country = country.get("name")
        if isinstance(country, str) and country.strip():
            countries.append(country.strip())
    applicants = record.get("applicantLocationRequirements", [])
    for item in applicants if isinstance(applicants, list) else [applicants]:
        if isinstance(item, dict) and item.get("@type") == "Country" and isinstance(item.get("name"), str):
            countries.append(item["name"].strip())
    codes = {"US": "United States", "CA": "Canada", "GB": "United Kingdom"}
    countries = {codes.get(country, country) for country in countries if country}
    contexts = {country_context(country) for country in countries}
    return next(iter(contexts)) if len(contexts) == 1 and None not in contexts else None


class _ExactJobRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if boards.job_identity(newurl) != boards.job_identity(req.full_url) or not boards.job_identity(newurl):
            raise ValueError("Official job page redirected outside its exact job identity")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def description_source(joburl):
    identity = boards.job_identity(joburl)
    if not identity:
        return None
    if identity[0] == "greenhouse":
        _, region, board, job_id = identity
        host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
        return f"https://{host}/v1/boards/{board}/jobs/{job_id}"
    return boards.canonical_url(joburl)


def valid_description(item, joburl):
    """Bind fresh public/MCP description evidence to one official job, never a slug guess."""
    identity = boards.job_identity(joburl)
    if not identity or not isinstance(item, dict) or item.get("status") != "verified":
        return False
    source = item.get("source_url")
    if identity[0] == "greenhouse":
        if source != description_source(joburl):
            return False
    elif boards.job_identity(source) != identity or item.get("job_identity") != list(identity):
        return False
    text, retrieved = item.get("text"), item.get("retrieved_at")
    return (isinstance(text, str) and bool(text.strip()) and len(text.encode()) <= MAX_BYTES
            and isinstance(retrieved, (int, float)) and not isinstance(retrieved, bool)
            and 0 <= time.time() - retrieved <= 86400
            and hashlib.sha256(text.encode()).hexdigest() == item.get("sha256"))


def fetch_public_description(joburl, *, opener=None, timeout=15):
    """Read only official-page JobPosting JSON-LD with an exact matching identifier.

    This GET has no account cookies/credentials. JavaScript-only descriptions
    require isolated MCP evidence instead; empty or ambiguous metadata stops.
    """
    identity, url = boards.job_identity(joburl), boards.canonical_url(joburl)
    if not identity or identity[0] == "greenhouse":
        raise ValueError("No supported official metadata page")
    request = Request(url, headers={"Accept": "text/html"}, method="GET")
    with (opener or build_opener(_ExactJobRedirects()).open)(request, timeout=timeout) as response:
        if boards.job_identity(getattr(response, "geturl", lambda: url)()) != identity:
            raise ValueError("Official job response changed identity")
        body = response.read(MAX_BYTES + 1)
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError("Official description exceeds size limit")
    parser = _Metadata()
    parser.feed(body.decode("utf-8"))
    matching = []
    for record in _job_records(parser.records):
        identifier = record.get("identifier")
        value = identifier.get("value") if isinstance(identifier, dict) else identifier
        node_url = record.get("url")
        if ((node_url and boards.job_identity(node_url) == identity)
                or isinstance(value, (str, int)) and str(value).lower() == identity[-1].lower()):
            # A stated URL must agree even if its identifier appears to match.
            if node_url and boards.job_identity(node_url) != identity:
                continue
            matching.append(record)
    if len(matching) != 1:
        raise ValueError("Official job metadata is absent or ambiguous")
    record = matching[0]
    if not isinstance(record.get("description"), str) or not record["description"].strip():
        raise ValueError("Official metadata contains no job description")
    from ..eligibility import plain_text
    text = plain_text(record["description"])
    if not text.strip():
        raise ValueError("Official metadata contains no readable description")
    return {"text": text, "source_url": url, "retrieved_at": int(time.time()),
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "status": "verified",
            "job_identity": list(identity), "method": "official_exact_job_jsonld",
            "title": record.get("title") if isinstance(record.get("title"), str) else "",
            "country_context": _metadata_country(record)}


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


def greenhouse_country(data):
    """Use attached offices only when every explicit country agrees.

    Greenhouse's main location may contain just a city while its exact-job
    office records name the country. Unknown or conflicting office countries
    cannot establish one jurisdiction, nor override a different primary one.
    """
    location = data.get("location", {})
    if not isinstance(location, dict) or not isinstance(location.get("name", ""), str):
        return None
    label = location.get("name", "")
    primary = country_context(label)
    offices = data.get("offices")
    if offices is None or offices == []:
        return primary
    if not isinstance(offices, list) or any(not isinstance(office, dict) for office in offices):
        return None
    countries = {country_context(office.get("location")) for office in offices}
    if None in countries or len(countries) != 1:
        return None
    country = next(iter(countries))
    if primary is not None:
        return primary if primary == country else None
    # An ambiguous/negated primary country is different from a city-only label.
    if any(re.search(pattern, label, re.I) for pattern in _COUNTRIES.values()):
        return None
    # An unrecognized primary label might name an unsupported country. Only
    # accept it as the city/region prefix of an attached office, or a generic
    # remote/empty label; never replace an unrelated primary location.
    normalized = re.sub(r"\s+", " ", label).strip().casefold()
    if normalized not in {"", "remote"} and not any(
            re.sub(r"\s+", " ", office["location"]).strip().casefold().startswith(normalized + ",")
            for office in offices):
        return None
    return country


def fetch(joburl, *, opener=None):
    """GET a validated global Greenhouse job; failures provide no metadata.

    No account, environment credentials, cookies, or API tokens are used.
    The injectable opener receives a stdlib Request and timeout=10.
    """
    board = boards.board_type(joburl)
    if board != "greenhouse" and boards.preparation_supported(board) and boards.job_identity(joburl):
        try:
            description = fetch_public_description(joburl, opener=opener, timeout=10)
            from .salary import advertised_ranges
            return {"name": description["title"], "source_url": description["source_url"],
                    "country_context": description.get("country_context"),
                    "verified_job_description": description,
                    "advertised_salary_ranges": advertised_ranges(description["text"])}
        except Exception:
            return {}
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
                  "country_context": greenhouse_country(data), "source_url": api_url}
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
