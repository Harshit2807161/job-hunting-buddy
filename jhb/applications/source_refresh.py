"""Bounded, isolated exact-job description refresh before candidate preparation."""
from __future__ import annotations

import asyncio
import json
from urllib.error import URLError

from .. import config
from . import boards, job_context


def _context(description):
    from .salary import advertised_ranges
    ranges = advertised_ranges(description["text"]) + job_context.description_salary_ranges(description)
    ranges = list({(r["lower"], r["upper"], r["currency"], r["period"]): r for r in ranges}.values())
    return {"name": description.get("title", ""), "source_url": description["source_url"],
            "country_context": description.get("country_context"),
            "verified_job_description": description,
            "advertised_salary_ranges": ranges}


def _failure(reason, *, state="unsupported", retryable=False, evidence=None):
    return {"state": state, "reason": reason, "retryable": retryable,
            "error_kind": "job_description_transport" if retryable else "job_description_unavailable",
            "missing": [], "filled": [], "events": [{"event": "official_description_refresh_handoff"}],
            "source_refresh": evidence or {}}


async def refresh(job, *, resolver=None, fetcher=None, timeout=60):
    """Reuse fresh proof; otherwise GET once then inspect one isolated exact job.

    This module never uses candidate Chrome, fills fields or asks candidate facts.
    The caller persists evidence and injects the verified description into run_job.
    """
    url = job.get("url")
    identity = boards.job_identity(url)
    if identity is None:
        return _failure("Official job identity is unavailable")
    cached = job.get("verified_job_description")
    if not job_context.valid_description(cached, url):
        path = config.ROOT / "private" / "applications" / boards.application_hash(url) / "public-job-context.json"
        try:
            if (not path.is_symlink() and not any(parent.is_symlink() for parent in path.parents)
                    and path.stat().st_size <= 2_000_000 and not path.stat().st_mode & 0o077):
                persisted = json.loads(path.read_bytes())
                cached = persisted.get("verified_job_description")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if job_context.valid_description(cached, url):
        return {"state": "verified", "context": _context(cached), "source_refresh": {"method": "fresh_cached_description"}}
    if fetcher is None:
        from ..eligibility import fetch_description
        fetcher = fetch_description
    public_error = None
    try:
        description = await asyncio.wait_for(asyncio.to_thread(fetcher, job, timeout=min(15, timeout)), timeout=min(15, timeout)+1)
        if job_context.valid_description(description, url):
            return {"state": "verified", "context": _context(description),
                    "source_refresh": {"method": "official_public_description"}}
    except (TimeoutError, ConnectionError, OSError, URLError) as exc:
        public_error = type(exc).__name__
    except (ValueError, TypeError, KeyError):
        public_error = "metadata_unavailable"
    if resolver is None:
        from .greenhouse_source import resolve_job
        resolver = resolve_job
    try:
        outcome = await asyncio.wait_for(resolver(job, timeout=timeout), timeout=timeout+1)
    except (TimeoutError, ConnectionError, OSError) as exc:
        return _failure("Isolated official-description transport failed", state="failed", retryable=True,
                        evidence={"method": "isolated_playwright_mcp", "error_type": type(exc).__name__, "public_error": public_error})
    except Exception as exc:
        return _failure("Isolated official-description inspection could not be validated",
                        evidence={"method": "isolated_playwright_mcp", "error_type": type(exc).__name__})
    if not isinstance(outcome, dict):
        return _failure("Isolated official-description inspection returned invalid evidence")
    evidence = {"method": "isolated_playwright_mcp", "outcome": outcome, "public_error": public_error}
    # Explicit different destinations are rejected even when accompanied by a
    # plausible JD or closed flag. A board error page may have no job identity.
    observed = outcome.get("application_url") or outcome.get("final_url")
    observed_identity = boards.job_identity(observed)
    reported_board = outcome.get("board_type") or outcome.get("ats")
    if (outcome.get("source_url") not in {None, url}
            or reported_board and reported_board not in {identity[0], "unknown"}):
        return _failure("Isolated source refresh does not match the requested job board/source", evidence=evidence)
    if observed_identity is not None and observed_identity != identity:
        return _failure("Isolated source refresh changed the exact job identity", evidence=evidence)
    if outcome.get("closed"):
        if outcome.get("source_url") != url:
            return _failure("Closed-job evidence is not bound to the requested source", evidence=evidence)
        return _failure("The official job is closed or unavailable", state="skipped", evidence=evidence)
    if outcome.get("state") == "blocked":
        handoff = outcome.get("handoff")
        state = handoff if handoff in {"waiting_login", "waiting_captcha"} else "unsupported"
        return _failure("Isolated job-description inspection requires site access review", state=state, evidence=evidence)
    if outcome.get("state") == "error":
        # The resolver's sanitized error represents setup/transport failure;
        # retries remain finite under the application queue's existing budget.
        return _failure("Isolated official-description transport failed", state="failed", retryable=True, evidence=evidence)
    description = outcome.get("verified_job_description")
    if (outcome.get("state") in {"greenhouse", "not_greenhouse"}
            and observed_identity == identity and job_context.valid_description(description, url)):
        return {"state": "verified", "context": _context(description), "source_refresh": evidence}
    return _failure("Official job description remains unverified after isolated inspection", evidence=evidence)
