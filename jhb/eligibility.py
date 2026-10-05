"""Inspect mandatory employment restrictions before application work.

No candidate citizenship is inferred. The user's standing policy excludes these
requirements, regardless of the resume variant. This module is dependency-free
so the same deterministic checks run in Phase 1's separate environment.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

POLICY_ID = "exclude-incompatible-employment-requirements-v2"
_CITIZEN = re.compile(r"\bcitizen(?:ship|s)?\b|\bnationality\b", re.I)
_CLEARANCE = re.compile(r"\b(?:security\s+clearance|(?:active|current|secret|confidential|government|federal|dod)\s+clearance|clearance|top\s*secret|ts\s*/\s*sci|ts[- ]sci|sci\s+clearance)\b", re.I)
_POLYGRAPH = re.compile(r"\bpolygraph\b", re.I)
_SPONSOR = r"(?:(?:any|employment|immigration|work|visa|h[- ]?1[- ]?b)\s+){0,3}sponsor(?:ship|ing)?\b"
_SPONSORSHIP_DENIAL = re.compile(
    r"\b(?:no|without)\s+" + _SPONSOR + r"|"
    r"\b(?:cannot|can not|can't|unable to|will not|won't|do not|does not|not able to)\s+"
    r"(?:(?:currently|presently|now|be able to)\s+)?(?:offer\s+|provide\s+|support\s+)?" + _SPONSOR + r"|"
    r"\b" + _SPONSOR + r"\s*:?\s*(?:is\s+|will be\s+|is currently\s+)?"
    r"(?:not (?:available|offered|provided|supported)|unavailable|cannot be (?:provided|offered|supported))\b|"
    r"\b(?:not eligible|ineligible)\s+for\s+" + _SPONSOR + r"|"
    r"\b(?:does not|do not|cannot|will not)\s+qualify\s+for\s+"
    r"(?:employer[- ]sponsored|sponsored)\s+(?:work authorization|employment visas?)\b|"
    r"\b(?:cannot|will not|do not|does not|unable to)\s+(?:consider|hire|employ|accept)\b"
    r"[^.;]{0,80}\b(?:need|require|requiring|needing)\s+" + _SPONSOR + r"|"
    r"\b(?:must|should)\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+"
    r"(?:(?:visa|immigration|employment)\s+)?sponsorship\b", re.I)
_NONEMPLOYMENT_SPONSOR = re.compile(r"\b(?:race|event|conference|charity|athletic|sports?)\s+sponsorship\b", re.I)
_SPONSORSHIP_ACCEPTANCE = re.compile(
    r"\b(?:with or without|regardless of)\s+(?:the need for\s+)?(?:visa\s+)?sponsorship\b|"
    r"\b(?:no|without)\s+" + _SPONSOR + r"\s+(?:restrictions?|limitations?|experience)\b|"
    r"\b" + _SPONSOR + r"\s+(?:is\s+)?(?:not required|not necessary|not needed)\b", re.I)
_STUDENT_VISA = r"(?:f[- ]?1|(?:stem\s+)?opt)\b"
_STUDENT_VISA_DENIAL = re.compile(
    r"\b(?:cannot|can't|do not|does not|will not|unable to)\s+(?:currently\s+)?"
    r"(?:consider|hire|employ|accept|support|accommodate)\b[^.;]{0,100}\b" + _STUDENT_VISA + r"|"
    r"\b" + _STUDENT_VISA + r"[^.;]{0,50}\b(?:not eligible|ineligible|not accepted|not supported)\b|"
    r"\bno\s+" + _STUDENT_VISA, re.I)
_MANDATORY = re.compile(r"\b(?:must|shall|require[ds]?|requirement|mandatory|necessary|need(?:s|ed)?|only|restricted\s+to|condition\s+(?:of|for)\s+employment|eligible|eligibility|ability|able|willing|willingness|obtain|maintain|possess|hold|holding)\b", re.I)
_NEGATED_NOUN = r"(?:(?:US|United\s+States|British|Canadian|UK)\s+)?(?:active\s+|security\s+)?(?:clearance|citizenship|nationality|polygraph)"
_NEGATED = re.compile(r"\b(?:not\s+required|not\s+necessary|not\s+needed|need\s+not|(?:do|does)\s+not\s+(?:need|require)|no\s+" + _NEGATED_NOUN + r"(?:\s+or\s+" + _NEGATED_NOUN + r")*(?:\s+(?:is|are))?\s+required|no\s+" + _NEGATED_NOUN + r"\s+requirement|without\s+(?:a\s+)?(?:security\s+)?clearance|not\s+a\s+requirement)\b", re.I)
_OPTIONAL = re.compile(r"\b(?:optional|preferred|desirable|a plus|nice to have)\b", re.I)
_REQUIREMENT_LABEL = re.compile(r"(?:visa sponsorship|sponsorship|security clearance|clearance|US citizenship|citizenship)\s*:", re.I)
_NONDISCRIMINATION = re.compile(r"without\s+regard\s+to|regardless\s+of|(?:do|does|will|shall)\s+not\s+discriminate|non[- ]discrimination", re.I)
_DISCLOSURE = re.compile(r"\b(?:disclosure|demographic|survey|citizenship\s+status\s+(?:question|information|response)|(?:disclose|report|indicate|select|state)\b[^.;]{0,35}\bcitizenship)\b", re.I)
_RESIDENCY_ALTERNATIVE = re.compile(
    r"(?:\bor\s+|,\s*(?:\([ivx]+\)\s*)?)(?:(?:a|an|lawful|legal|US)\s+)*"
    r"(?:permanent\s+residents?|green\s+card\s+holders?|protected\s+(?:persons?|individuals?)|"
    r"refugees?|asylees?|US\s+nationals?)\b|"
    r"\bor\s+(?:(?:be|otherwise)\s+)*(?:eligible\s+(?:to\s+obtain|for)|obtain)\b"
    r"[^.;]{0,100}\b(?:export\s+(?:control\s+)?(?:licen[cs]e|authorization)|"
    r"authorizations?\s+from\s+the\s+US\s+Department\s+of\s+State)\b", re.I)
_REQUIRED_HEADING = re.compile(r"(?:required|requirements|minimum requirements|basic qualifications|"
                               r"required qualifications|qualifications|what you (?:need|must have))\s*:?", re.I)
_OTHER_HEADING = re.compile(r"(?:desired|preferred(?: qualifications)?|nice[- ]to[- ]have|nice to have|"
                            r"responsibilities|benefits|compensation(?: and benefits)?|equal opportunity|"
                            r"about (?:us|the company|the role)|what you(?:'|’)ll do)\s*:?", re.I)
_OPTIONAL_HEADING = re.compile(r"(?:desired(?: qualifications)?|preferred(?: qualifications)?|"
                               r"nice[- ]to[- ]have|nice to have)\s*:?", re.I)
_BARE_QUALIFICATION = re.compile(
    r"(?:(?:US|United States|British|Canadian|UK)\s+citizenship|"
    r"(?:(?:active|current)\s+)?(?:TS\s*/\s*SCI|TS[- ]SCI|top\s*secret|secret|confidential)"
    r"(?:\s+(?:security\s+)?clearance)?(?:\s+with\s+(?:a\s+)?polygraph)?|"
    r"(?:active|current)\s+(?:security\s+)?clearance|polygraph(?:\s+examination)?)\s*", re.I)


def _clauses(text):
    """Retain qualification headings and complete alternative lists.

    Commas within export eligibility lists are not sentence boundaries. Split
    independent requirements at commas only when a new condition begins, so
    an optional citizenship clause cannot mask a separate required clearance.
    """
    required = optional = False
    label = ""
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" *#-")
        if not line:
            continue
        if _REQUIREMENT_LABEL.fullmatch(line):
            label = line
            continue
        if label:
            line, label = label + " " + line, ""
        line = line.strip(":")
        if _REQUIRED_HEADING.fullmatch(line):
            required = True
            optional = False
            continue
        if _OPTIONAL_HEADING.fullmatch(line) or _OTHER_HEADING.fullmatch(line):
            required = False
            optional = bool(_OPTIONAL_HEADING.fullmatch(line))
            continue
        # Dots in common abbreviations and numeric legal citations must not
        # sever an ITAR list before its non-citizenship alternatives.
        line = re.sub(r"\b(?:i\.e\.|e\.g\.|C\.F\.R\.)|(?<=\d)\.(?=\d)",
                      lambda m: m[0].replace(".", "\x00"), line, flags=re.I)
        parts = re.split(
            r"[.;]|\bbut\b|\bhowever\b|,\s*(?=(?:security\s+|active\s+)?clearance\b|"
            r"(?:US\s+)?citizenship\b|polygraph\b|(?:you\s+)?(?:must|shall|need|ability)\b|"
            r"(?:a|an)\s+(?:bachelor|master|degree|certification|experience)\b|"
            r"(?:(?:visa|immigration|employment)\s+)?sponsorship\b)|"
            r"\band\s+(?=(?:you\s+)?(?:must|shall|need|require|ability|able|willing|eligible|"
            r"obtain|maintain|possess|hold)|(?:US\s+)?citizenship|(?:security\s+|active\s+)?clearance|polygraph|"
            r"(?:(?:visa|immigration|employment)\s+)?sponsorship)",
            line, flags=re.I)
        for clause in parts:
            yield clause.replace("\x00", "."), required, optional


def plain_text(value):
    value = html.unescape(html.unescape(str(value or "")))
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "‑": "-", "–": "-", "\u00a0": " "}))
    value = re.sub(r"</?(?:p|li|div|h[1-6]|br|ul|ol)\b[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\bU\.?\s*S\.(?=\s|$)|\bU\.?\s*S\b", "US", value, flags=re.I)
    return value


def restrictions(text, *, title=False):
    """Return auditable requirement snippets, not blanket keyword exclusions."""
    text = plain_text(text)
    hits = []
    for clause, required_section, optional_section in _clauses(text):
        clause = re.sub(r"\s+", " ", clause).strip(" :,-")
        if not clause:
            continue
        sponsorship_denial = _SPONSORSHIP_DENIAL.search(clause)
        # Merely welcoming people who do not need sponsorship excludes nobody.
        without_only = bool(sponsorship_denial and re.match(r"without\b", sponsorship_denial[0], re.I)
                            and not (_MANDATORY.search(clause) or required_section))
        if (sponsorship_denial and not without_only and not _SPONSORSHIP_ACCEPTANCE.search(clause)
                and not clause.endswith("?") and not _NONEMPLOYMENT_SPONSOR.search(clause)):
            hits.append({"category": "visa_sponsorship", "evidence": clause[:600]})
        if (_STUDENT_VISA_DENIAL.search(clause) and not clause.endswith("?")
                and not re.search(r"\b(?:experience|prior|previous)\b", clause, re.I)):
            hits.append({"category": "student_visa_restriction", "evidence": clause[:600]})
        for kind, pattern in [("citizenship", _CITIZEN), ("security_clearance", _CLEARANCE), ("polygraph", _POLYGRAPH)]:
            match = pattern.search(clause)
            if not match:
                continue
            if kind == "security_clearance" and re.search(r"\b(?:medical|drug|health|credit)\s+clearance\b", clause, re.I) and not re.search(r"security\s+clearance|top\s*secret|ts\s*/\s*sci", clause, re.I):
                continue
            if _NEGATED.search(clause):
                continue
            if _OPTIONAL.search(clause) and not re.search(r"\b(?:must|shall|require[ds]?|mandatory)\b", clause, re.I):
                continue
            if optional_section and not re.search(r"\b(?:must|shall|require[ds]?|mandatory)\b", clause, re.I):
                continue
            if kind == "citizenship" and (_NONDISCRIMINATION.search(clause) or _DISCLOSURE.search(clause)):
                continue
            if kind == "citizenship" and _RESIDENCY_ALTERNATIVE.search(clause):
                continue
            explicit = bool(_MANDATORY.search(clause))
            # Qualification labels and existing active clearance are conditions
            # even when their short bullet omits a sentence-level "must".
            label = bool(re.match(r"(?:US\s+citizenship|citizenship|security\s+clearance|clearance|polygraph)\s*:", clause, re.I))
            inactive_label = bool(re.search(r":\s*(?:none|no|n/a|not applicable)\b", clause, re.I))
            active = kind == "security_clearance" and bool(re.search(r"\b(?:active|current)\b", clause, re.I))
            specific_title = title and (kind != "citizenship" or bool(re.search(r"citizens?\s+only|citizenship\s+required", clause, re.I)))
            required_bullet = required_section and bool(_BARE_QUALIFICATION.fullmatch(clause))
            if not inactive_label and (explicit or label or active or specific_title or required_bullet):
                hits.append({"category": kind, "evidence": clause[:600]})
    return hits


def preliminary(job):
    """Inspect available Phase 1 metadata; absence does not verify the full JD."""
    raw = job.get("raw", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    findings = restrictions(job.get("title", ""), title=True)
    if raw.get("sponsorship") == "U.S. Citizenship is Required":
        findings.append({"category": "citizenship", "evidence": "Source explicitly marks U.S. Citizenship is Required"})
    if raw.get("sponsorship") == "Does Not Offer Sponsorship":
        findings.append({"category": "visa_sponsorship", "evidence": "Source explicitly marks Does Not Offer Sponsorship"})
    for field in ("description", "job_description", "content"):
        findings += restrictions(job.get(field) or raw.get(field) or "")
    if description := verified_description(job):
        findings += restrictions(description.get("title", ""), title=True)
        findings += restrictions(description["text"])
    return findings


def description_url(job):
    from .applications.job_context import description_source
    return description_source(job.get("url", ""))


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urlsplit(req.full_url), urlsplit(newurl)
        if after.scheme != "https" or after.hostname != before.hostname or after.username or after.password or after.port not in {None, 443}:
            raise ValueError("Official description redirected outside its API host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_description(job, *, timeout=15):
    from .applications.boards import board_type
    if board_type(job.get("url")) != "greenhouse":
        from .applications.job_context import fetch_public_description
        return fetch_public_description(job.get("url"), timeout=timeout)
    url = description_url(job)
    if url is None:
        raise ValueError("No supported official job-description source")
    opener = urllib.request.build_opener(_OfficialRedirects())
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "job-hunting-buddy/eligibility"}), timeout=timeout) as response:
        body = response.read(1_048_577)
    if len(body) > 1_048_576:
        raise ValueError("Official description exceeds size limit")
    data = json.loads(body)
    from .applications.queue import greenhouse_identity
    if str(data.get("id")) != greenhouse_identity(job["url"])[2] or not isinstance(data.get("content"), str) or not data["content"].strip():
        raise ValueError("Official description is absent or identifies a different job")
    text = plain_text(data["content"])
    if not text.strip():
        raise ValueError("Official description contains no readable job text")
    from .applications.job_context import country_context
    location = data.get("location", {})
    location = location.get("name", "") if isinstance(location, dict) else ""
    return {"text": text, "source_url": url, "retrieved_at": int(time.time()),
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "status": "verified",
            "title": data.get("title", ""), "country_context": country_context(location)}


def verified_description(job):
    """Reuse only a fresh, integrity-checked snapshot of this exact official job."""
    item = job.get("verified_job_description", {})
    from .applications.job_context import valid_description
    return item if valid_description(item, job.get("url")) else None


def assess_job(job):
    findings = preliminary(job)
    if findings:
        result = {"state": "skipped", "policy": POLICY_ID, "reason": "Job has an incompatible citizenship, clearance or visa requirement", "findings": findings}
        if description := verified_description(job):
            result["description"] = description
        return result
    try:
        description = verified_description(job) or fetch_description(job)
    except Exception as exc:
        return {"state": "waiting_input", "policy": POLICY_ID,
                "reason": "Official job description unavailable; eligibility must be verified before opening an application",
                "verification": {"kind": "job_description", "source_url": description_url(job), "error_type": type(exc).__name__}, "findings": []}
    findings = restrictions(description.get("title", ""), title=True) + restrictions(description["text"])
    return {"state": "skipped" if findings else "eligible", "policy": POLICY_ID,
            "reason": "Job has an incompatible citizenship, clearance or visa requirement" if findings else "No excluded requirement found in verified official job description",
            "findings": findings, "description": description}
