"""Authenticated local source fallback after isolated LinkedIn classification."""
import asyncio

from .cli_browser import BrowserUseCLI
from .linkedin_runtime import linkedin_id


class LinkedInSourceCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.linkedin_runtime"


async def resolve_source(job, *, isolated_outcome=None, timeout=90, client=None):
    if not linkedin_id(job.get("url")):
        return isolated_outcome or {"state": "ambiguous", "board_type": "unknown", "evidence": []}
    cli = client or LinkedInSourceCLI(timeout=min(timeout, 120))
    result = await cli.invoke("resolve", approved_url=job["url"])
    if result.get("state") != "destination":
        return result
    from .greenhouse_source import resolve_job
    resolved = await resolve_job({**job, "url": result["application_url"]}, timeout=timeout)
    return {**resolved, "resolution_transport": "authenticated_browser_use_cli",
            "evidence": result.get("evidence", []) + resolved.get("evidence", [])}
