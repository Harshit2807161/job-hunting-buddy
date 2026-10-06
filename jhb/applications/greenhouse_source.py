"""Read-only, bounded ATS/source resolution through official Playwright MCP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from .mcp_client import PlaywrightMCPClient, is_public_url
from .queue import greenhouse_identity

_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io",
             "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
_ATS_HOSTS = {
    "greenhouse": ("greenhouse.io",), "lever": ("jobs.lever.co", "apply.lever.co"),
    "ashby": ("jobs.ashbyhq.com",), "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "workable": ("apply.workable.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"),
    "icims": ("icims.com",), "taleo": ("taleo.net",),
    "linkedin": ("linkedin.com",), "indeed": ("indeed.com",),
    "amazon": ("amazon.jobs",), "adp": ("adp.com",), "bamboohr": ("bamboohr.com",),
    "successfactors": ("successfactors.com", "successfactors.eu", "successfactors.jobs"),
    "phenom": ("phenompeople.com",),
}

# Fixed DOM inspection only: no page-provided code, fetches, clicks or writes.
_OBSERVE = r"""() => ({
  url: location.href, title: document.title,
  headings: [...document.querySelectorAll('h1,h2')].slice(0,20).map(x => x.textContent.trim()),
  text: (document.body?.innerText || '').slice(0,18000),
  links: [...document.querySelectorAll('a[href]')].slice(0,350).map(x => ({
    url: x.href, text: (x.innerText || x.getAttribute('aria-label') || '').trim().slice(0,200),
    className: x.className || '', tracking: x.getAttribute('data-tracking-control-name') || ''})),
  frames: [...document.querySelectorAll('iframe[src]')].map(x => ({url: x.src, title: x.title})),
  scripts: [...document.querySelectorAll('script[src]')].map(x => x.src),
  resources: performance.getEntriesByType('resource').slice(0,350).map(x => x.name),
  form: !!document.querySelector('#application_form, #application, form[action*="greenhouse"], .application--form'),
  fields: document.querySelectorAll('input:not([type="hidden"]),textarea,select').length,
  buttons: [...document.querySelectorAll('button,[role="button"]')].slice(0,80).map(x => (x.innerText || x.getAttribute('aria-label') || '').trim()),
  data: [...document.querySelectorAll('[data-gh-job-id],[data-gh-board],[data-greenhouse-job-id]')].slice(0,10).map(x => ({
    job: x.getAttribute('data-gh-job-id') || x.getAttribute('data-greenhouse-job-id'), board: x.getAttribute('data-gh-board')})),
  descriptions: (()=>{const title=document.querySelector('h1')?.innerText||document.title;
    if(location.hostname==='apply.workable.com'){
      const description=document.querySelector('[data-ui="job-description"]');
      if(!description)return [];
      const root=description.closest('main');
      const requirements=root?.querySelector('[data-ui="job-requirements"]');
      return [{text:[description.innerText,requirements?.innerText].filter(Boolean).join('\n'),url:location.href,title}]}
    return [...document.querySelectorAll('.ashby-job-posting-description,.posting-content,[data-automation-id="jobPostingDescription"],[data-testid="job-description"]')]
      .slice(0,5).map(e=>({text:e.innerText,url:location.href,title}))})(),
  job_postings: (()=>{const jobs=[];const visit=(value,depth=0)=>{if(!value||depth>5||jobs.length>=20)return;
    if(Array.isArray(value)){for(const item of value)visit(item,depth+1);return}
    if(typeof value!=='object')return;
    if(value['@type']==='JobPosting'||Array.isArray(value['@type'])&&value['@type'].includes('JobPosting'))jobs.push({title:value.title,description:value.description,url:value.url||value.mainEntityOfPage?.['@id']||location.href,identifier:value.identifier,baseSalary:value.baseSalary,jobLocation:value.jobLocation,applicantLocationRequirements:value.applicantLocationRequirements});
    if(value['@graph'])visit(value['@graph'],depth+1)};
    for(const e of document.querySelectorAll('script[type="application/ld+json"]')){try{visit(JSON.parse(e.textContent))}catch(_){}}
    return jobs})()
})"""


def classify_ats(url: str) -> str:
    from .boards import board_type
    if board_type(url) == "successfactors":
        return "successfactors"
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (TypeError, ValueError):
        return "unknown"
    for name, suffixes in _ATS_HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    return "unknown"


def _observed_description(observation):
    """Only an exact official job and an observed JD container/JobPosting qualify."""
    from .boards import job_identity
    url = observation.get("url", "")
    identity = job_identity(url)
    if not identity or identity[0] in {"greenhouse", "linkedin"}:
        return None
    candidates = []
    postings = observation.get("job_postings", [])
    # Exact JobPosting is the primary description. Its HTML and the rendered
    # container can differ in whitespace, headings and site chrome without
    # describing different jobs; never combine them as competing postings.
    records = postings if postings else observation.get("descriptions", [])
    for record in records:
        if not isinstance(record, dict) or job_identity(record.get("url", "")) != identity:
            continue
        identifier = record.get("identifier")
        identifier = identifier.get("value") if isinstance(identifier, dict) else identifier
        if identifier is not None and str(identifier).lower() != identity[-1].lower():
            continue
        title = record.get("title")
        content = record.get("description", record.get("text"))
        if not isinstance(content, str) or not isinstance(title, str) or not title.strip():
            continue
        from ..eligibility import plain_text
        # Requirements and preferred qualifications need separate lines. A
        # flattened page can let a later "preferred" mask a mandatory bullet.
        text = "\n".join(re.sub(r"\s+", " ", line).strip()
                         for line in plain_text(content).splitlines() if line.strip())
        if len(text) < 100 or len(text.encode()) > 1024*1024:
            continue
        from .job_context import _metadata_country, structured_base_salary
        candidates.append({"status": "verified", "source_url": url, "job_identity": list(identity),
                           "retrieved_at": time.time(), "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "title": title.strip(), "country_context": _metadata_country(record),
                           "advertised_salary_ranges": structured_base_salary(record, url)})
    # Conflicting JobPosting records are not an invitation to choose one role.
    unique = {(item["title"], item["sha256"]): item for item in candidates}
    primary = next(iter(unique.values())) if len(unique) == 1 else None
    if primary:
        # Multiple explicit ranges remain alternatives; do not silently choose
        # the last otherwise-identical JobPosting or a location-specific band.
        ranges = [r for item in candidates for r in item["advertised_salary_ranges"]]
        primary["advertised_salary_ranges"] = list({(r["lower"], r["upper"]): r for r in ranges}.values())
    if primary and postings:
        # JSON-LD can omit a site's separate Qualifications/Employment sections.
        # Retain observed job-container text only for this exact identity/title;
        # generic page chrome and related postings cannot donate requirements.
        same_title = lambda value: re.sub(r"\s+", " ", str(value or "")).strip().casefold() == primary["title"].casefold()
        rendered_records = [r for r in observation.get("descriptions", []) if isinstance(r, dict)
                            and job_identity(r.get("url", "")) == identity and same_title(r.get("title"))]
        if rendered_records:
            rendered = _observed_description({"url": url, "descriptions": rendered_records})
            if rendered is None:
                return None  # Conflicting/empty observed job containers need review.
            if re.sub(r"\s+", " ", primary["text"]) in re.sub(r"\s+", " ", rendered["text"]):
                combined = rendered["text"]
            elif re.sub(r"\s+", " ", rendered["text"]) in re.sub(r"\s+", " ", primary["text"]):
                combined = primary["text"]
            else:
                combined = primary["text"] + "\n\n" + rendered["text"]
            if len(combined.encode()) > 1024*1024:
                return None
            primary = {**primary, "text": combined, "sha256": hashlib.sha256(combined.encode()).hexdigest(),
                       "method": "exact_job_jsonld_and_observed_container"}
            if rendered.get("country_context") and rendered["country_context"] != primary.get("country_context"):
                primary["country_context"] = None
    return primary


def canonical_greenhouse(url: str, *, context_url="") -> str | None:
    """Recognize an observed individual job or an explicitly identified embed."""
    try:
        p = urlsplit(url)
        if p.hostname not in _GH_HOSTS or p.scheme != "https" or p.username or p.password or p.port not in {None, 443}:
            return None
        identity = greenhouse_identity(url)
        query = parse_qs(p.query)
        if not identity and p.path.rstrip("/") in {"/embed/job_app", "/embed/job_board", "/embed/job_board/js"}:
            board = (query.get("for") or [None])[0]
            job_id = (query.get("token") or query.get("gh_jid") or [None])[0]
            if not job_id and context_url:
                job_id = (parse_qs(urlsplit(context_url).query).get("gh_jid") or [None])[0]
            if board and re.fullmatch(r"[A-Za-z0-9_-]+", board) and job_id and str(job_id).isdigit():
                identity = ("eu" if ".eu." in p.hostname else "global", board.lower(), str(job_id))
        if not identity:
            return None
        region, board, job_id = identity
        host = "job-boards.eu.greenhouse.io" if region == "eu" else "job-boards.greenhouse.io"
        return f"https://{host}/{board}/jobs/{job_id}"
    except (TypeError, ValueError):
        return None


def _greenhouse_api_job(url):
    """An observed individual public job API request identifies an inline embed."""
    try:
        p = urlsplit(url)
        hosts = {"boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"}
        match = re.fullmatch(r"/v1/boards/([A-Za-z0-9_-]+)/jobs/(\d+)/?", p.path)
        if p.scheme == "https" and p.hostname in hosts and not p.username and not p.password and p.port in {None, 443} and match:
            host = "job-boards.eu.greenhouse.io" if ".eu." in p.hostname else "job-boards.greenhouse.io"
            return f"https://{host}/{match[1].lower()}/jobs/{match[2]}"
    except (ValueError, TypeError):
        pass
    return None


def _decode_outbound(url: str) -> str:
    """Decode observed LinkedIn redirect destinations; never manufacture one."""
    try:
        p = urlsplit(url)
        if classify_ats(url) == "linkedin" and p.path in {"/redir/redirect", "/safety/go", "/jobs/redirect"}:
            query = parse_qs(p.query)
            for key in ("url", "target", "destination", "redirectUrl"):
                if query.get(key):
                    return query[key][0]
    except ValueError:
        pass
    return url


def _json_result(result):
    if "url" in result:  # Injected fake transport may return the observation directly.
        return result
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "url" in structured:
        return structured
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        match = re.search(r"### Result\s*(.*?)(?=\n### |\Z)", text, re.S)
        candidates = [match.group(1).strip() if match else text.strip()]
        candidates += re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict) and "url" in value:
                    return value
            except ValueError:
                pass
    raise RuntimeError("Playwright MCP did not return a structured source observation")


def _challenge(observation):
    text = (observation.get("title", "") + "\n" + observation.get("text", "")).lower()
    for term in ("verify you are human", "verify you're human", "checking your browser", "access denied", "unusual traffic",
                 "performing security verification", "website verifies you are not a bot"):
        if term in text:
            return "waiting_captcha", "The isolated source browser encountered a verification challenge"
    if observation.get("title", "").lower().strip() in {"just a moment...", "just a moment…"}:
        return "waiting_captcha", "The isolated source browser encountered a security verification page"
    path = urlsplit(observation.get("url", "")).path
    if classify_ats(observation.get("url", "")) == "linkedin" and re.match(r"/(authwall|signup|login|uas/login|checkpoint)(/|$)", path):
        return "waiting_login", "LinkedIn's observed Apply destination requires authentication"
    if re.search(r"\b(sign in|log in|join linkedin)\b", text) and not observation.get("form"):
        host = classify_ats(observation.get("url", ""))
        heading_gate = any(re.fullmatch(r"\s*(sign in|log in|join linkedin|sign in to linkedin)\s*", h, re.I)
                           for h in observation.get("headings", []))
        if host in {"linkedin", "indeed"} and ("/authwall" in observation.get("url", "") or heading_gate or
                                                    "sign in to view" in text):
            return "waiting_login", "The job source requires a signed-in session; no user browser was attached"
    return None


class ApplicationSourceResolver:
    def __init__(self, transport, *, max_hops=5, timeout=60, allow_localhost=False):
        self.transport = transport
        self.max_hops = max_hops
        self.timeout = timeout
        self.allow_localhost = allow_localhost

    async def resolve(self, url: str) -> dict:
        result = {"state": "error", "source_url": url, "final_url": url, "application_url": None,
                  "identity": None, "ats": "unknown", "board_type": "unknown", "evidence": [], "reason": ""}
        try:
            return await asyncio.wait_for(self._resolve(url, result), self.timeout)
        except asyncio.TimeoutError:
            result.update(state="error", reason="Source resolution exceeded its bounded time budget")
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
            result.update(state="error", reason="Playwright MCP source inspection failed; retry or review the source")
        return result

    async def _resolve(self, url, result):
        visited = set()
        current = url
        for hop in range(self.max_hops):
            if not is_public_url(current, allow_localhost=self.allow_localhost):
                result.update(state="blocked", reason="Unsafe or local navigation target rejected", handoff="waiting_input")
                return result
            if current in visited:
                result.update(state="blocked", reason="Application-link redirect cycle detected", handoff="waiting_input")
                return result
            visited.add(current)
            await self.transport.call_tool("browser_navigate", {"url": current})
            await self.transport.call_tool("browser_snapshot", {})
            observation = _json_result(await self.transport.call_tool("browser_evaluate", {"function": _OBSERVE}))
            # Workable initially renders its exact job title before the public
            # description/requirements sections arrive. Wait once, bounded,
            # then verify the same observed job before using late content.
            if (classify_ats(observation.get("url", "")) == "workable"
                    and not _observed_description(observation)):
                from .boards import job_identity
                before_identity = job_identity(observation.get("url", ""))
                if before_identity:
                    await asyncio.sleep(2)
                    late = _json_result(await self.transport.call_tool("browser_evaluate", {"function": _OBSERVE}))
                    if job_identity(late.get("url", "")) != before_identity:
                        result.update(state="ambiguous", final_url=late.get("url"),
                                      reason="Workable job changed while its description loaded")
                        return result
                    observation = late
            final = observation.get("url", current)
            if not is_public_url(final, allow_localhost=self.allow_localhost):
                result.update(state="blocked", reason="Redirect reached an unsafe or local target", handoff="waiting_input")
                return result
            result["final_url"] = final
            result["ats"] = result["board_type"] = classify_ats(final)
            result["evidence"].append({"kind": "mcp_navigation", "url": final,
                                       "from_url": current, "title": observation.get("title", "")[:200]})
            expected_identity = greenhouse_identity(current)
            final_parts = urlsplit(final)
            if (expected_identity and classify_ats(final) == "greenhouse" and
                    final_parts.path.rstrip("/").lower() == "/" + expected_identity[1] and
                    (parse_qs(final_parts.query).get("error") or [""])[0].lower() == "true"):
                result.update(state="not_greenhouse", closed=True, reason="The individual Greenhouse job redirected to its board's error page")
                result["evidence"].append({"kind": "closed_job_redirect", "url": final, "from_url": current})
                return result
            text = (observation.get("title", "") + "\n" + observation.get("text", "")).lower()
            if any(s in text for s in ("this job is no longer available", "job is no longer available", "job not found",
                                       "job you are looking for is no longer open", "page not found", "404 not found")):
                result.update(state="not_greenhouse", reason="The observed job page is closed or unavailable", closed=True)
                return result
            challenge = _challenge(observation)
            if challenge:
                result.update(state="blocked", handoff=challenge[0], reason=challenge[1])
                return result
            description = _observed_description(observation)
            if description:
                result["verified_job_description"] = description
            canonical = canonical_greenhouse(final)
            if canonical:
                expected = canonical_greenhouse(current)
                if expected and greenhouse_identity(expected) != greenhouse_identity(canonical):
                    result.update(state="ambiguous", reason="Redirect changed the explicitly identified Greenhouse job")
                    return result
                # A Greenhouse-looking URL without a rendered job/form is not confirmation.
                has_content = observation.get("form") or (observation.get("headings") and
                              any(re.search(r"\bapply\b", b, re.I) for b in observation.get("buttons", [])))
                if has_content:
                    embed = urlsplit(final).path.rstrip("/") == "/embed/job_app"
                    application_target = final if embed else canonical
                    result.update(state="greenhouse", application_url=application_target, embedded=embed,
                                  identity=list(greenhouse_identity(canonical)), reason="Rendered individual Greenhouse job confirmed")
                    result["evidence"].append({"kind": "confirmed_job", "url": application_target, "identity": result["identity"]})
                else:
                    result.update(state="blocked", handoff="waiting_input", reason="Greenhouse URL lacks a confirmed rendered job/application")
                return result

            candidates = []
            greenhouse_candidates = set()
            observed_embeds = {}
            for frame in observation.get("frames", []):
                target = urljoin(final, frame.get("url", ""))
                gh = canonical_greenhouse(target, context_url=final)
                if gh:
                    greenhouse_candidates.add(gh)
                    if urlsplit(target).path.rstrip("/") == "/embed/job_app":
                        observed_embeds[gh] = target
                    result["evidence"].append({"kind": "embedded_greenhouse", "url": target, "application_url": gh})
                elif classify_ats(target) not in {"unknown", "linkedin", "indeed", "greenhouse"}:
                    candidates.append(target)
            for script in observation.get("scripts", []):
                gh = canonical_greenhouse(urljoin(final, script), context_url=final)
                if gh:
                    greenhouse_candidates.add(gh)
                    result["evidence"].append({"kind": "greenhouse_embed_script_with_job_id", "url": script, "application_url": gh})
            for resource in observation.get("resources", []):
                gh = _greenhouse_api_job(resource)
                if gh:
                    greenhouse_candidates.add(gh)
                    result["evidence"].append({"kind": "observed_greenhouse_job_api_request", "url": resource, "application_url": gh})
            # Employer markup can explicitly declare both values for an inline integration.
            for data in observation.get("data", []):
                board, job_id = data.get("board"), data.get("job")
                if isinstance(board, str) and re.fullmatch(r"[A-Za-z0-9_-]+", board) and str(job_id).isdigit():
                    gh = f"https://job-boards.greenhouse.io/{board.lower()}/jobs/{job_id}"
                    greenhouse_candidates.add(gh)
                    result["evidence"].append({"kind": "explicit_greenhouse_data_attributes", "url": final, "application_url": gh})
            for link in observation.get("links", []):
                raw = urljoin(final, link.get("url", ""))
                target = _decode_outbound(raw)
                label = link.get("text", "") + " " + str(link.get("className", "")) + " " + link.get("tracking", "")
                apply_link = bool(re.search(r"\b(apply|application|external|company website)\b|apply[-_]", label, re.I))
                gh = canonical_greenhouse(target, context_url=final)
                if gh and apply_link:
                    greenhouse_candidates.add(gh)
                    result["evidence"].append({"kind": "observed_application_link", "url": raw, "application_url": gh})
                elif apply_link and target != final and is_public_url(target, allow_localhost=self.allow_localhost):
                    # In-page #apply does not advance resolution to a new source.
                    if urlsplit(target)._replace(fragment="").geturl() != urlsplit(final)._replace(fragment="").geturl():
                        candidates.append(target)
                        result["evidence"].append({"kind": "observed_application_link", "url": raw, "target_url": target})
            if len(greenhouse_candidates) > 1:
                result.update(state="ambiguous", reason="Several distinct Greenhouse jobs were observed; no job was guessed")
                return result
            if greenhouse_candidates:
                candidate = next(iter(greenhouse_candidates))
                expected = canonical_greenhouse(current)
                if expected and expected != candidate:
                    result.update(state="ambiguous", reason="Employer redirect changed the explicitly identified Greenhouse job")
                    return result
                if expected == candidate and current in visited and classify_ats(final) != "greenhouse":
                    # Some GH boards intentionally redirect individual jobs to a
                    # company page. Its observed integration identifies the exact
                    # board/job; verify the documented job_app endpoint directly.
                    region, board, job_id = greenhouse_identity(candidate)
                    host = "boards.eu.greenhouse.io" if region == "eu" else "boards.greenhouse.io"
                    target = f"https://{host}/embed/job_app?for={board}&token={job_id}"
                    result["evidence"].append({"kind": "documented_embed_from_observed_identity",
                                               "url": target, "identity": [region, board, job_id], "from_url": final})
                    current = target
                else:
                    current = observed_embeds.get(candidate, candidate)
                continue
            unique = list(dict.fromkeys(candidates))
            # Known final ATSs are enough to classify; Greenhouse preparation is intentionally restricted.
            if result["ats"] not in {"unknown", "linkedin", "indeed", "greenhouse"}:
                from .boards import canonical_url, job_identity
                observed_identity = job_identity(final)
                original_identity = job_identity(url)
                if not observed_identity:
                    result.update(state="ambiguous", reason="Observed ATS page lacks an individual job identity")
                    result.pop("verified_job_description", None)
                    return result
                if original_identity and original_identity[0] not in {"linkedin", "greenhouse"} and original_identity != observed_identity:
                    result.update(state="ambiguous", reason="Redirect changed the explicitly identified ATS job")
                    result.pop("verified_job_description", None)
                    return result
                posting_url = canonical_url(final)
                if not result.get("verified_job_description") and posting_url and posting_url != final and posting_url not in visited and hop+1 < self.max_hops:
                    result["evidence"].append({"kind": "exact_job_posting_description_check", "url": posting_url,
                                               "from_url": final, "identity": list(observed_identity)})
                    current = posting_url
                    continue
                result.update(state="not_greenhouse", application_url=final,
                              identity=list(observed_identity) if observed_identity else None,
                              reason="Observed job page uses " + result["ats"])
                return result
            if len(unique) > 1:
                result.update(state="ambiguous", reason="Several outbound application targets were observed; review is required")
                return result
            if unique:
                current = unique[0]
                continue
            if result["ats"] in {"linkedin", "indeed"}:
                easy_apply = any(re.search(r"\beasy apply\b", b, re.I) for b in observation.get("buttons", []))
                if easy_apply:
                    result.update(state="not_greenhouse", application_url=final, reason="Observed platform-hosted Easy Apply button")
                else:
                    result.update(state="ambiguous", reason="Job-board page has no readable outbound application target; ATS remains unconfirmed")
            elif result["ats"] == "greenhouse" or any(classify_ats(f.get("url", "")) == "greenhouse" for f in observation.get("frames", [])) or any(classify_ats(s) == "greenhouse" for s in observation.get("scripts", [])):
                result["ats"] = result["board_type"] = "greenhouse"
                result.update(state="ambiguous", reason="Greenhouse branding/embed was observed without an individual job identity")
            else:
                result.update(state="not_greenhouse", reason="ATS is unknown; no confirmed application source was observed")
            return result
        result.update(state="blocked", reason="Application-link hop budget exhausted", handoff="waiting_input")
        return result


GreenhouseSourceResolver = ApplicationSourceResolver


async def resolve_job(job, *, transport=None, allow_localhost=False, timeout=60, max_hops=5):
    url = job if isinstance(job, str) else job.get("url", "")
    if transport:
        return await ApplicationSourceResolver(transport, max_hops=max_hops, timeout=timeout,
                                                allow_localhost=allow_localhost).resolve(url)
    try:
        async def inspect():
            async with PlaywrightMCPClient(allow_localhost=allow_localhost) as client:
                return await ApplicationSourceResolver(client, max_hops=max_hops, timeout=timeout,
                                                        allow_localhost=allow_localhost).resolve(url)
        return await asyncio.wait_for(inspect(), timeout)
    except (RuntimeError, OSError, asyncio.TimeoutError):
        return {"state": "error", "source_url": url, "final_url": url, "application_url": None,
                "identity": None, "ats": "unknown", "board_type": "unknown", "evidence": [],
                "reason": "Isolated Playwright MCP setup or bounded inspection failed; verify the local MCP/Chromium installation or review the source"}
