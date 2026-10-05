"""Exact official job identities and independently reviewed adapter capabilities.

Recognizing an ATS host does not imply that a job, preparation adapter, or
submission adapter is supported. Identity matching never follows arbitrary
company links, accepts login pages, or treats a board index as an individual job.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SLUG = r"[A-Za-z0-9_-]+"
_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
# This NS2 endpoint was observed through the isolated source checker. Other
# SuccessFactors datacenters remain unregistered until their URL rules are reviewed.
_SUCCESSFACTORS_HOSTS = {"career-hcm03.ns2cloud.com"}
# Exact public UKG host observed during interactive evaluation. Other tenants
# and hosting families are not inferred from a UKG brand or suffix alone.
_UKG_HOSTS = {"wbdus.rec.pro.ukg.net"}
ADAPTERS = {
    "greenhouse": {"prep_enabled": True, "submit_enabled": True, "skill": "skills/prepare-greenhouse/SKILL.md"},
    "ashby": {"prep_enabled": True, "submit_enabled": True, "skill": "skills/prepare-ashby/SKILL.md"},
    "workable": {"prep_enabled": True, "submit_enabled": False, "skill": "skills/prepare-workable/SKILL.md"},
    "workday": {"prep_enabled": False, "submit_enabled": False, "skill": "skills/prepare-workday/SKILL.md"},
    "lever": {"prep_enabled": True, "submit_enabled": False, "skill": "skills/prepare-lever/SKILL.md"},
    "smartrecruiters": {"prep_enabled": False, "submit_enabled": False, "skill": None},
    "icims": {"prep_enabled": False, "submit_enabled": False, "skill": None},
    "successfactors": {"prep_enabled": False, "submit_enabled": False, "skill": None},
    "ukg": {"prep_enabled": False, "submit_enabled": False, "skill": None},
    "linkedin": {"prep_enabled": False, "submit_enabled": False, "skill": "skills/prepare-linkedin/SKILL.md"},
    "linkedin_easy_apply": {"prep_enabled": False, "submit_enabled": False, "skill": "skills/prepare-linkedin/SKILL.md"},
}


def _parts(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.username or p.password or not p.hostname
                or p.port not in {None, 443} or "\\" in p.path
                or re.search(r"%(?:2f|5c|2e)|(?:^|/)\.{1,2}(?:/|$)", p.path, re.I)):
            return None
        return p
    except (TypeError, ValueError):
        return None


def board_type(url):
    p = _parts(url)
    if not p:
        return "unknown"
    host = p.hostname
    if host in _GH_HOSTS:
        return "greenhouse"
    if host in _SUCCESSFACTORS_HOSTS:
        return "successfactors"
    if host in _UKG_HOSTS:
        return "ukg"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if host == "apply.workable.com":
        return "workable"
    if re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host) or host == "jobs.myworkdaysite.com":
        return "workday"
    if host in {"jobs.lever.co", "apply.lever.co", "jobs.eu.lever.co"}:
        return "lever"
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        return "smartrecruiters"
    if re.fullmatch(r"[a-z0-9-]+\.icims\.com", host):
        return "icims"
    if host in {"www.linkedin.com", "linkedin.com"}:
        return "linkedin"
    return "unknown"


def greenhouse_identity(url):
    p = _parts(url)
    if not p or p.hostname not in _GH_HOSTS:
        return None
    match = re.fullmatch(r"/(" + _SLUG + r")/jobs/(\d+)/?", p.path)
    if match:
        board, job_id = match[1], match[2]
    elif p.path.rstrip("/") == "/embed/job_app":
        q = parse_qs(p.query)
        boards, jobs = q.get("for", []), q.get("token", [])
        if len(boards) != 1 or len(jobs) != 1 or not re.fullmatch(_SLUG, boards[0]) or not jobs[0].isdigit():
            return None
        board, job_id = boards[0], jobs[0]
    else:
        return None
    return ("eu" if ".eu." in p.hostname else "global", board.lower(), job_id)


def job_identity(url):
    p, board = _parts(url), board_type(url)
    if not p:
        return None
    if board == "ukg":
        # Preserve tenant case: observed routes do not establish tenant aliases.
        # UUID spelling, unlike a tenant slug, denotes the same GUID identity.
        match = re.fullmatch(r"/([A-Za-z0-9_-]{1,128})/JobBoard/(" + _UUID
                             + r")/(?:OpportunityDetail|OpportunityApply)", p.path)
        if not match or p.fragment or len(p.query) > 8192 or re.search(r"%(?![0-9a-fA-F]{2})", p.query):
            return None
        try:
            query = parse_qs(p.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        except ValueError:
            return None
        # Only the exact observed parameter is supported. Reject duplicate,
        # case-variant, unrelated and malformed parameters rather than guessing
        # their effect on the selected application.
        if set(query) != {"opportunityId"} or len(query["opportunityId"]) != 1:
            return None
        opportunity = query["opportunityId"][0]
        if not re.fullmatch(_UUID, opportunity):
            return None
        return (board, p.hostname, match[1], match[2].lower(), opportunity.lower())
    if board == "successfactors":
        if p.path != "/sfcareer/jobreqcareer" or p.fragment or len(p.query) > 8192:
            return None
        if re.search(r"%(?![0-9a-fA-F]{2})", p.query):
            return None
        try:
            query = parse_qs(p.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        except ValueError:
            return None
        # Duplicate or case-variant parameters have ambiguous server meaning.
        if (any(len(values) != 1 for values in query.values())
                or len({key.casefold() for key in query}) != len(query)):
            return None
        company, job_id = query.get("company", [""])[0], query.get("jobId", [""])[0]
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", company)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", job_id)):
            return None
        # Keep the exact tenant ID: case equivalence is not established by the
        # observed route or SAP's published URL documentation.
        return (board, p.hostname, company, job_id)
    if board == "greenhouse":
        gh = greenhouse_identity(url)
        return (board, *gh) if gh else None
    if board == "ashby":
        m = re.fullmatch(r"/(" + _SLUG + r")/(" + _UUID + r")(?:/application)?/?", p.path)
        return (board, m[1].lower(), m[2].lower()) if m else None
    if board == "workable":
        m = re.fullmatch(r"/(" + _SLUG + r")/j/([A-Za-z0-9]{6,20})(?:/apply)?/?", p.path)
        return (board, m[1].lower(), m[2].upper()) if m else None
    if board == "lever":
        m = re.fullmatch(r"/(" + _SLUG + r")/(" + _UUID + r")(?:/apply)?/?", p.path)
        return (board, "eu" if p.hostname == "jobs.eu.lever.co" else "global", m[1].lower(), m[2].lower()) if m else None
    if board == "smartrecruiters":
        m = re.fullmatch(r"/(" + _SLUG + r")/(\d{6,})(?:-[A-Za-z0-9_-]+)?/?", p.path)
        return (board, m[1].lower(), m[2]) if m else None
    if board == "icims":
        m = re.fullmatch(r"/jobs/(\d+)/[^/]+/job/?", p.path)
        return (board, p.hostname, m[1]) if m else None
    if board == "linkedin":
        m = re.fullmatch(r"/jobs/view/(?:[A-Za-z0-9_-]+-)?(\d+)/?", p.path)
        return (board, m[1]) if m else None
    if board == "workday":
        m = re.fullmatch(r"/(?:[a-z]{2}-[A-Z]{2}/)?(" + _SLUG + r")/job/([^/]+)/([^/]+)(?:/apply)?/?", p.path)
        if p.hostname == "jobs.myworkdaysite.com":
            m = re.fullmatch(r"/recruiting/(" + _SLUG + r")/(" + _SLUG + r")/job/([^/]+)/([^/]+)(?:/apply)?/?", p.path)
            if not m:
                return None
            tenant, site, slug = m[1].lower(), m[2].lower(), m[4]
        elif m:
            tenant, site, slug = p.hostname.split(".")[0], m[1].lower(), m[3]
        else:
            return None
        job_id = re.search(r"_([A-Za-z]{0,8}\d{3,})(?:-\d+)?$", slug)
        return (board, tenant, site, job_id[1].upper()) if job_id else None
    return None


identity = job_identity


def canonical_url(url):
    item, p = job_identity(url), _parts(url)
    if not item:
        return None
    if item[0] == "greenhouse":
        host = "job-boards.eu.greenhouse.io" if item[1] == "eu" else "job-boards.greenhouse.io"
        return f"https://{host}/{item[2]}/jobs/{item[3]}"
    if item[0] == "linkedin":
        return f"https://www.linkedin.com/jobs/view/{item[1]}/"
    if item[0] == "ukg":
        path = f"/{item[2]}/JobBoard/{item[3]}/OpportunityDetail"
        return urlunsplit(("https", item[1], path, urlencode({"opportunityId": item[4]}), ""))
    if item[0] == "successfactors":
        query = urlencode({"jobId": item[3], "company": item[2]})
        return urlunsplit(("https", item[1], "/sfcareer/jobreqcareer", query, ""))
    # Keep observed slugs and locale paths; those may be required by the ATS.
    path = re.sub(r"/(?:application|apply)/?$", "", p.path).rstrip("/")
    return urlunsplit(("https", p.hostname, path, "", ""))


def application_hash(url):
    item = job_identity(url)
    if not item:
        return None
    # Preserve the published Greenhouse application's existing durable key.
    parts = item[1:] if item[0] == "greenhouse" else item
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def adapter(board):
    return {"board": board, **ADAPTERS.get(board, {"prep_enabled": False, "submit_enabled": False, "skill": None})}


def route_board(url, observed_board=None):
    """Easy Apply is an observed capability, never inferred from a LinkedIn URL."""
    actual = board_type(url)
    return "linkedin_easy_apply" if actual == "linkedin" and observed_board == "linkedin_easy_apply" else actual


def preparation_supported(board):
    return adapter(board)["prep_enabled"] is True


def submission_supported(board):
    return adapter(board)["submit_enabled"] is True
