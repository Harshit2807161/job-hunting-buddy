"""Authenticated local source fallback after isolated LinkedIn classification."""
import asyncio
import hashlib
import time
import uuid

from .. import config
from . import boards, booklet, job_context

from .cli_browser import BrowserUseCLI
from .linkedin_runtime import linkedin_id


class LinkedInSourceCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.linkedin_runtime"


async def resolve_source(job, *, isolated_outcome=None, timeout=90, client=None):
    if not linkedin_id(job.get("url")):
        return isolated_outcome or {"state": "ambiguous", "board_type": "unknown", "evidence": []}
    cli = client or LinkedInSourceCLI(timeout=min(timeout, 120))
    result = await cli.invoke("resolve_link", approved_url=job["url"])
    if result.get("native_apply_required"):
        # Button-only controls retain the existing native-click/capacity guard.
        result = await cli.invoke("resolve", approved_url=job["url"])
    if result.get("state") not in {"destination", "observed_link"}:
        return result
    from .greenhouse_source import resolve_job
    resolved = await resolve_job({**job, "url": result["application_url"]}, timeout=timeout)
    if result.get("state") == "observed_link":
        url = resolved.get("application_url")
        if (resolved.get("state") in {"greenhouse", "not_greenhouse"} and not resolved.get("closed")
                and boards.job_identity(url)):
            if not job_context.valid_description(resolved.get("verified_job_description"), url):
                try:
                    description = await asyncio.to_thread(job_context.fetch_public_description, url, timeout=min(15, timeout))
                    if job_context.valid_description(description, url):
                        resolved["verified_job_description"] = description
                except (OSError, ValueError, TimeoutError):
                    pass  # Preserve the observed source; lack of JD cannot justify closing its tab.
            if job_context.valid_description(resolved.get("verified_job_description"), url):
                proof = {"provider": "readonly_linkedin_apply_href_and_isolated_mcp", "recorded_at": time.time(),
                         "native_apply_clicked": False, "source_url": result["source_url"],
                         "source_target_id": result["source_target_id"], "observed_apply_url": result["application_url"],
                         "resolved": resolved}
                path = config.ROOT / "private" / "source-checks" / ("readonly-linkedin-"+uuid.uuid4().hex+".json")
                booklet.write_private(path, proof)
                try:
                    cleanup = await cli.invoke("cleanup_source_verified", approved_url=job["url"],
                                               evidence_path=str(path), evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    cleanup = {"closed_targets": [], "reason": "Verified source cleanup needs technical retry"}
                resolved["source_cleanup"] = cleanup
                resolved["readonly_source_evidence"] = str(path)
    return {**resolved, "resolution_transport": "authenticated_browser_use_cli_readonly_href" if result.get("state") == "observed_link" else "authenticated_browser_use_cli",
            "evidence": result.get("evidence", []) + resolved.get("evidence", [])}
