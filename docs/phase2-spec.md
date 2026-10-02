# Phase 2 v1: Greenhouse application preparation

Phase 1 remains on `main`, tagged `v0.1.0`. Phase 2 stays on
`feat/phase2-greenhouse-agent` for review. This release prepares applications and
hands them to the candidate; it never submits them.

## Implemented scope

- A private answer booklet retains provenance, conflicting source history, and
  separate SDE/ML resumes and skills. Authenticated Simplify Profile and Personal
  Info observations supplement resume facts; explicit user answers take priority.
- Phase 1 optionally queues HTTPS Greenhouse-hosted job URLs from any discovery
  source. Tracking parameters and old/new board domains share one job identity.
  Seed and dry-run cycles never enqueue. Queue claims and retries are bounded.
- The live worker uses the **Browser Use CLI**, its default local daemon, and the
  existing Chrome CDP endpoint. It reuses matching tabs and preserves drafts.
  Worker and browser locks serialize automated preparation.
- A signed-in `codex exec` instance proposes schema-constrained field bindings.
  It receives field labels and answer keys, without answer values or credentials.
  Python validates the plan and performs the approved actions. A deterministic
  planner is also available for fixtures and troubleshooting.
- The live adapter handles verified text input, Greenhouse-style custom
  comboboxes and PDF uploads. A displayed filename alone does not distinguish
  resume variants; an existing upload is replaced from the selected source.
- Repeated education rows bind to their own indexed records. The worker adds
  supported rows and re-observes after filling to catch newly revealed questions.
  Employer-specific school catalog mappings preserve the actual institution in
  the booklet and resume.
- Required unknown answers produce `waiting_input`; a visible challenge produces
  a verification handoff. Unsupported widgets and external redirects stop the
  workflow. Optional fields that fail are reported in the private event log.
- A local HTML/JSON review packet, screenshot and event log accompany each
  outcome. Notifications go to a private local outbox; email is separately opt-in.
  The persistent Chrome tab stays open. No automation operation submits a form.

## Authentication and submission boundaries

Google SSO is the user's required authentication route. Hosted Greenhouse forms
often need no account: OneStream's tested form was public. Unknown login,
account choice, MFA and verification requirements are explicit handoffs. Live
password registration has no fallback. Password signup and its private vault
exist only in the synthetic fixture.

The executor rejects terminal clicks, and the page guard blocks submission
buttons and native form submission. Live uploads require network writes, so the
live adapter does **not** claim to block all POST requests. Fixture tests also
block non-allowlisted writes and count submission requests. The automation does
not expose a submit command or automatically release the live submission guard.
The interactive review command releases it only when the candidate types
`TAKE OVER`; browser automation ends immediately after that acknowledgement.

CAPTCHA solving in a local browser has not been demonstrated. No visible CAPTCHA
appeared in the tested live flows. Invisible reCAPTCHA response inputs do not
count as a challenge. No cloud browser or subscription/API credentials run in CI.

## Validation evidence

Report these separately:

1. **Synthetic fixtures:** SDE/ML role selection, synthetic registration, native
   fields, missing-answer and visible-challenge handoffs, custom combobox input,
   PDF upload, and blocked terminal submissions. Requests stay local or are
   fulfilled from synthetic HTML before reaching a real site.
2. **Live Browser Use CLI:** authenticated Simplify Google sign-in and both profile
   subpages; the OneStream Greenhouse form; contact/address fields, custom
   dropdowns and the ML resume upload, with retained-value verification.
   The final audit verified 30 filled answers, both education records, the race
   field revealed by the Hispanic/Latino answer, and no blank required fields or
   invalid controls. The graduate institution resolved to its exact catalog entry.
   The undergraduate institution was absent from the catalog, so the available
   `Other` category was used with an
   employer-scoped mapping; its full name remains in the resume and booklet.
   Preferred first name and two inapplicable conditional explanations remain blank.
3. **Signed-in Codex:** schema output from the actual CLI validated against the
   observed OneStream controls, with no final-submit action.

These results do not establish compatibility with every Greenhouse employer or
unattended CAPTCHA handling.

## Local commands

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/start-local-browser.py
.venv/bin/python -m jhb.applications.cli missing
.venv/bin/python -m jhb.applications.cli probe https://job-boards.greenhouse.io/BOARD/jobs/ID
.venv/bin/python -m jhb.applications.cli worker --planner codex
.venv/bin/python -m jhb.applications.cli status
.venv/bin/python -m jhb.applications.cli demo
```

Install `.[dev,browser-tests]` and local Chromium for fixture tests. Install the
stable Browser Use CLI using the setup document before live commands. Fill the
ignored local booklet and configure the existing endpoint in ignored `.env`.
Set `JHB_APPLICATIONS_ENABLED=1` to opt into the discovery-to-worker path;
`JHB_APPLICATION_EMAIL=1` opts into email notifications. Keep these disabled in CI.
The wrapper drains at most one application per discovery cycle. Reset the opt-in
to zero to return to Phase 1 discovery. Never merge Phase 2 before its limitations
and actual review evidence have been assessed.
