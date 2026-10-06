"""A candidate may knowingly choose one exact stretch role for portal review.

Selection is not approval and never changes the independent fit verdict. Only a
fresh explicit portal acknowledgment can use this additional, immutable binding.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..eligibility import POLICY_ID, restrictions, verified_description
from . import boards, booklet, role_fit

POLICY = "candidate-selected-stretch-role-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _selection(job, book):
    key = job.get("dedupe_hash")
    selections = book.get("candidate_selected_jobs", {})
    record = selections.get(key) if isinstance(selections, dict) else None
    if not isinstance(record, dict):
        return None
    identity = boards.job_identity(job.get("url"))
    content = record.get("content")
    if (not identity or boards.application_hash(job.get("url")) != key
            or record.get("job_hash") != key or record.get("status") != "verified"
            or record.get("role") != "user" or record.get("action") != "apply"
            or not isinstance(record.get("source"), str) or not record["source"].strip()
            or boards.job_identity(record.get("url")) != identity
            or not isinstance(content, str) or not 1 <= len(content) <= 10000
            or not any(boards.job_identity(url.rstrip(".,;)")) == identity
                       for url in re.findall(r"https://[^\s<>]+", content))):
        return None
    return record


def context(packet_path, job, book, role, document_sha256):
    """Read verified negative-fit evidence without changing files or facts."""
    selection = _selection(job, book)
    resume = document_sha256.get("documents.resume")
    if (selection is None or role not in {"sde", "ml"} or booklet.job_excluded(book, job)
            or not isinstance(resume, str) or not re.fullmatch(r"[a-f0-9]{64}", resume)):
        return None
    try:
        from .authorized_submission import private_file
        fit_bytes = private_file(packet_path.with_name("role-fit.json")).read_bytes()
        fit = json.loads(fit_bytes)
        eligibility = json.loads(private_file(packet_path.with_name("eligibility.json")).read_bytes())
        description = verified_description({**job, "verified_job_description": eligibility.get("description", {})})
        if (eligibility.get("state") != "eligible" or eligibility.get("policy") != POLICY_ID
                or not description or restrictions(description.get("title", ""), title=True)
                or restrictions(description["text"])):
            return None
        if (fit.get("state") != "skipped" or fit.get("verdict") != "not_fit"
                or fit.get("review_status") != "complete" or fit.get("mode") != "independent_codex"
                or fit.get("retryable") is True or fit.get("error_kind")
                or fit.get("source") != role_fit.POLICY or fit.get("selected_role") != role
                or not role_fit.evidence({**job, "verified_job_description": description}, book, role)["candidate"]
                or fit.get("evidence_hash") != role_fit.evidence_hash(
                    {**job, "verified_job_description": description}, book, role)
                or not isinstance(fit.get("reason"), str) or not fit["reason"].strip()
                or not all(isinstance(fit.get(key), list) and all(isinstance(v, str) for v in fit[key])
                           for key in ("matched_requirements", "unsupported_core_requirements", "review_notes"))):
            return None
        return {"policy": POLICY, "job_hash": job["dedupe_hash"], "selected_role": role,
                "selection_sha256": _digest(selection), "fit_sha256": hashlib.sha256(fit_bytes).hexdigest(),
                "fit_evidence_hash": fit["evidence_hash"], "fit_verdict": "not_fit",
                "description_sha256": description["sha256"], "resume_sha256": resume,
                "reason": fit["reason"][:2000],
                "gaps": [v[:2000] for v in (fit["unsupported_core_requirements"] + fit["review_notes"])[:20]]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def allowed(auth, current):
    from .overnight import PORTAL_SCOPE
    return (current is not None and auth.get("scope") == PORTAL_SCOPE
            and auth.get("source") == "local_review_portal" and auth.get("action") == "approve"
            and auth.get("job_hash") == current["job_hash"]
            and auth.get("acknowledge_role_fit_warning") is True
            and auth.get("binding", {}).get("candidate_selected_stretch") == current)
