"""Bounded browser actions; no final-submit method exists."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .booklet import normalize
from .planner import safe_next
from .queue import is_greenhouse

GUARD_SCRIPT = r"""
(() => {
  window.__jhbGuard = true;
  const terminal = el => /\b(submit|send application|finish application|apply now)\b/i.test(
    (el?.innerText || el?.value || el?.getAttribute('aria-label') || '').trim());
  document.addEventListener('click', event => {
    if (window.__jhbGuard && terminal(event.target.closest('button,input[type=submit],a,[role=button]'))) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('submit', event => {
    if (!window.__jhbGuard) return;
    const label = (event.submitter?.innerText || event.submitter?.value || '').trim();
    if (!/^(next|continue|review|review application|save and continue|create account|sign in|log in)$/i.test(label)) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  const originalSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function() {
    if (!window.__jhbGuard) return originalSubmit.call(this);
  };
})();
"""

OBSERVE_SCRIPT = r"""
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden';
  const label = el => (el.getAttribute('aria-label') ||
    [...(el.labels || [])].map(l => l.innerText).join(' ') ||
    el.getAttribute('placeholder') || el.name || '').trim();
  let index = 0;
  const ref = el => { const value = 'jhb-' + index++; el.setAttribute('data-jhb-ref', value); return value; };
  const elements = [...document.querySelectorAll('input,textarea,select')].filter(el =>
    visible(el) && !el.disabled && !['hidden','submit','button','reset','password'].includes(el.type));
  const groups = new Set();
  const fields = elements.flatMap(el => {
    if (el.type === 'radio') {
      const groupKey = (el.form?.id || '') + ':' + el.name;
      if (!el.name || groups.has(groupKey)) return [];
      groups.add(groupKey);
      const radios = elements.filter(r => r.type === 'radio' && r.name === el.name && r.form === el.form);
      return [{ref:ref(el), label:el.closest('fieldset')?.querySelector('legend')?.innerText.trim() || label(el),
        type:'radio', required:radios.some(r => r.required),
        options:radios.map(r => ({label:label(r), value:r.value, ref:ref(r)}))}];
    }
    return [{ref:ref(el),label:label(el),type:el.tagName.toLowerCase() === 'select' ? 'select' : el.type || 'text',
      required:el.required || el.getAttribute('aria-required') === 'true',
      options:el.tagName === 'SELECT' ? [...el.options].map(o=>({label:o.label,value:o.value})) : []}];
  });
  const buttons = [...document.querySelectorAll('button,input[type=submit],[role=button]')]
    .filter(visible).filter(el => !el.disabled)
    .map(el => ({ref:ref(el),label:(el.innerText || el.value || el.getAttribute('aria-label') || '').trim()}));
  return {fields,buttons,title:document.title};
}
"""


def origin_of(url):
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class BrowserActions:
    def __init__(self, page, *, demo_origin=None):
        self.page = page
        self.demo_origin = demo_origin
        self.blocked_requests = 0
        self.auth_path = None
        self.redirected_to = None

    def allowed_url(self, url):
        if self.demo_origin:
            return origin_of(url) == self.demo_origin
        return is_greenhouse(url)

    async def guard_route(self, route):
        request = route.request
        if request.resource_type == "document" and not self.allowed_url(request.url):
            self.redirected_to = request.url
            await route.abort()
            return
        # Read-only application preparation. Authentication is a separate scoped exception.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            parsed = urlsplit(request.url)
            allowed_auth = self.auth_path and (origin_of(request.url), parsed.path) == self.auth_path
            if not allowed_auth:
                self.blocked_requests += 1
                await route.abort()
                return
        await route.continue_()

    async def install(self):
        await self.page.context.add_init_script(GUARD_SCRIPT)
        await self.page.context.route("**/*", self.guard_route)

    async def observe(self):
        if self.redirected_to:
            return {"handoff": "unsupported", "reason": "Employer ATS redirect is outside v1 scope"}
        if not self.allowed_url(self.page.url):
            return {"handoff": "unsupported", "reason": "Employer ATS redirect is outside v1 scope"}
        # Detect visible challenges, not dormant strings in JS bundles.
        for selector in ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "[data-jhb-captcha]", "[id*='challenge-stage']"]:
            for element in await self.page.locator(selector).all():
                if await element.is_visible():
                    return {"handoff": "waiting_captcha", "reason": "Complete the visible CAPTCHA, then resume"}
        body = (await self.page.locator("body").inner_text())[:12000]
        if re.search(r"verify (?:you are human|your email)|check your inbox|verification code|access denied", body, re.I):
            return {"handoff": "waiting_login", "reason": "Website verification or login is required"}
        if await self.page.locator("input[type=password]:visible").count():
            return {"handoff": "waiting_login", "reason": "Sign-in or account registration required"}
        snapshot = await self.page.evaluate(OBSERVE_SCRIPT)
        snapshot["url"] = self.page.url
        return snapshot

    async def fill(self, field, value):
        locator = self.page.locator(f'[data-jhb-ref="{field["ref"]}"]')
        kind = field["type"]
        if kind in {"select", "radio"}:
            def matches(option):
                accepted = {normalize(str(value))}
                if isinstance(value, bool): accepted |= {"yes" if value else "no", "true" if value else "false"}
                return normalize(option["label"]) in accepted or normalize(option["value"]) in accepted
            option = next((o for o in field["options"] if matches(o)), None)
            if not option:
                raise ValueError("Stored answer does not match an available option")
            if kind == "select": await locator.select_option(option["value"])
            else: await self.page.locator(f'[data-jhb-ref="{option["ref"]}"]').check()
        elif kind == "checkbox":
            if not isinstance(value, bool): raise ValueError("Checkbox needs an explicit boolean answer")
            await locator.set_checked(value)
        elif kind == "file":
            from pathlib import Path
            p = Path(str(value))
            if not p.is_file() or p.suffix.lower() != ".pdf": raise ValueError("Approved PDF is unavailable")
            await locator.set_input_files(p)
        elif kind in {"text", "email", "tel", "textarea", "url", "number", "date", "month"}:
            await locator.fill(str(value))
        else:
            raise ValueError("Unsupported form control")
        # Verify native browser validation; a nonmatching date/number must not silently vanish.
        if kind != "radio" and not await locator.evaluate("el => el.checkValidity()"):
            raise ValueError("Stored answer did not satisfy field validation")

    async def click_next(self, button):
        if not safe_next(button): raise ValueError("Final submission is prohibited")
        await self.page.locator(f'[data-jhb-ref="{button["ref"]}"]').click()
        await self.page.wait_for_timeout(250)

    async def authenticate(self, answers, vault):
        """Synthetic fixture signup only; live sign-in must use Google SSO."""
        if not self.demo_origin:
            return False
        if not self.allowed_url(self.page.url): return False
        password = self.page.locator("input[type=password]:visible")
        email = self.page.locator("input[type=email]:visible")
        if await password.count() != 1 or await email.count() != 1: return False
        buttons = self.page.get_by_role("button", name=re.compile(r"^(Create account|Sign in|Log in)$", re.I))
        if await buttons.count() != 1: return False
        label = await buttons.inner_text()
        form = password.locator("xpath=ancestor::form")
        if await form.count() != 1: return False
        # Unapproved checkbox consent or extra fields require the user.
        if await form.locator("input[type=checkbox]").count(): return False
        endpoint = await form.evaluate("el => el.action")
        path = urlsplit(endpoint).path
        if origin_of(endpoint) != origin_of(self.page.url) or not re.fullmatch(r"/(?:signup|register|login|signin)/?", path):
            return False
        record = answers.get("identity.email", {})
        if record.get("status") != "verified": return False
        username = str(record["value"])
        origin = origin_of(self.page.url)
        secret = vault.get(origin, username)
        if secret is None:
            if normalize(label) != "create account": return False
            secret = vault.create(origin, username)
        self.auth_path = (origin, path)
        try:
            await email.fill(username)
            await password.fill(secret)
            await buttons.click()
            await self.page.wait_for_timeout(500)
        finally:
            self.auth_path = None
        return not await self.page.locator("input[type=password]:visible").count()

    async def human_takeover(self):
        # Only called after an interactive human acknowledgement; no further agent actions.
        await self.page.context.unroute("**/*", self.guard_route)
        await self.page.evaluate("window.__jhbGuard = false")
