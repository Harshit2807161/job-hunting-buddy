# Phase 2 v1: Greenhouse application preparation

Phase 1 remains on `main`, tagged `v0.1.0`. Phase 2 stays on
`feat/phase2-greenhouse-agent` for review. This release prepares applications and
hands them to the candidate; it never submits them.

## Implemented scope

- A private answer booklet retains provenance, conflicting source history, and
  separate SDE/ML resumes and skills. Authenticated Simplify Profile and Personal
  Info observations supplement resume facts; explicit user answers take priority.
- Phase 1 durably records every new job URL for source classification before
  notification deduplication. Seed and dry-run cycles never enqueue. The official
  Playwright MCP server checks redirects, employer pages and embedded forms in
  an isolated browser. It classifies recognized ATS providers and dispatches
  only confirmed Greenhouse jobs. Tracking parameters, wrappers and old/new
  board domains share a canonical application identity. Closed or ambiguous
  postings never become guessed application targets.
- The live worker uses the **Browser Use CLI**, its default local daemon, and the
  existing Chrome CDP endpoint. It reuses matching tabs and preserves drafts.
  A manager lock prevents overlapping batches; independent planners and jobs
  run with bounded concurrency. Each CLI operation serializes the browser lane
  and attaches to its job's own tab.
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
- Explicit standing preferences reuse approved relocation, office/HQ,
  career-fair and preferred-name decisions through supported exact question
  templates. Original verified institutions drive institution-specific school
  checks; employer dropdown display mappings never change education facts.
- Salary expectation uses the arithmetic midpoint of a single recognized
  advertised annual USD base range, otherwise the user's approved annual
  fallback. Multiple ranges require a location/range decision. Hourly,
  foreign-currency and total-compensation ranges are excluded from extraction.
- Required unknown answers produce `waiting_input`; a visible challenge produces
  a verification handoff. Unsupported widgets and external redirects stop the
  workflow. Meaningful unknown optional fields enter the private question ledger;
  technical widget failures remain distinguishable from missing user answers.
  Questions retain wording, choices, employer scope and field context. Explicit
  replies unblock only eligible input handoffs, with a fresh pass if an answer
  arrives while an older worker snapshot is still running.
- A local HTML/JSON review packet, screenshot and event log accompany each
  outcome. Notifications go to a private local outbox; email is separately opt-in.
  The persistent Chrome tab stays open. No automation operation submits a form.
- After a separately authorized, confirmed submission, a durable private receipt
  ledger automatically updates the configured existing application spreadsheet.
  Canonical ATS identities and legacy employer/role/date entries prevent duplicate
  rows. Pending deliveries reconcile in the pipeline's final stage; an uncertain
  append is checked by reading before any retry. See
  [submission-tracking.md](submission-tracking.md).

## Authentication and submission boundaries

Before opening or filling an application, the worker enforces the standing
citizenship, security-clearance, TS/SCI and polygraph exclusion policy against
the exact official Greenhouse job description. Ability-to-obtain or maintain
requirements count. Ordinary work authorization, background checks, optional
citizenship disclosures, and citizenship-or-permanent-resident alternatives do
not count as strict citizenship requirements. Excluded jobs become durable
`skipped` records and cannot be resumed through an older question answer.
If the description cannot be verified, the worker stops at an operational
`waiting_input` handoff without opening the browser or asking the candidate to
guess a job requirement. Evidence stays in the private application packet.

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

Submission explicitly directed by the user in a separate instruction is
outside the preparation worker. After receipt is confirmed, its private ledger
records `submitted` through `confirm-submission` and synchronizes its tracker;
that state cannot be resumed, claimed or prepared again.
The review command opens no browser for completed records, and preparation
notifications exclude them. This does not authorize unattended submission.

CAPTCHA solving in the candidate's local browser has not been demonstrated.
Candidate-browser preparation encountered no visible CAPTCHA; isolated source
checks did encounter Cloudflare verification and stopped. Invisible reCAPTCHA
response inputs do not count as a challenge. No cloud browser or
subscription/API credentials run in CI.

## Validation evidence

Report these separately:

1. **Synthetic fixtures:** SDE/ML role selection, synthetic registration, native
   fields, missing-answer and visible-challenge handoffs, custom combobox input,
   PDF upload, and blocked terminal submissions. Requests stay local or are
   fulfilled from synthetic HTML before reaching a real site.
2. **Live Playwright MCP source checks:** OneImaging and Parallel Systems hosted
   Greenhouse jobs confirmed from rendered pages; Block's employer page resolved
   to a rendered embedded Greenhouse application. Pinterest and RTX/Recruitics
   reached Cloudflare verification handoffs; a LinkedIn Apply destination
   required login. Atlas Energy Solutions redirected to its board error page
   and was recorded closed. Navigation/form evidence remains in ignored local
   files. These checks used isolated browsers and did not access candidate
   sessions or fill applications.
3. **Live Browser Use CLI:** authenticated Simplify Google sign-in and both profile
   subpages; the OneStream Greenhouse form; contact/address fields, custom
   dropdowns and the ML resume upload, with retained-value verification.
   The final audit verified 30 filled answers, both education records, the race
   field revealed by the Hispanic/Latino answer, and no blank required fields or
   invalid controls. The graduate institution resolved to its exact catalog entry.
   The undergraduate institution was absent from the catalog, so the available
   `Other` category was used with an
   employer-scoped mapping; its full name remains in the resume and booklet.
   Preferred first name and two inapplicable conditional explanations remain blank.
   Later OneImaging and Parallel Systems preparations verified known fields and
   role-specific resume uploads and handed off genuinely new employer questions.
   Fresh passes using explicit answers and tailored documents remain subject to
   their own final audits; source confirmation alone is not draft completion.
4. **Signed-in Codex:** schema output from the actual CLI validated against the
   observed OneStream controls, with no final-submit action.

These results do not establish compatibility with every Greenhouse employer or
unattended CAPTCHA handling.

## Local commands

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/start-local-browser.py
.venv/bin/python -m jhb.applications.cli missing
.venv/bin/python -m jhb.applications.cli classify https://example.com/careers/job
.venv/bin/python -m jhb.applications.cli probe https://job-boards.greenhouse.io/BOARD/jobs/ID
.venv/bin/python -m jhb.applications.cli pipeline --planner codex
.venv/bin/python -m jhb.applications.cli source-status
.venv/bin/python -m jhb.applications.cli status
.venv/bin/python -m jhb.applications.cli questions
.venv/bin/python -m jhb.applications.cli answer-question q_QUESTION_ID
.venv/bin/python -m jhb.applications.cli demo
```

Install `.[dev,browser-tests]`, run `npm ci` for the pinned official MCP server,
and install local Chromium for source checks and fixtures. Install the
stable Browser Use CLI using the setup document before live commands. Fill the
ignored local booklet and configure the existing endpoint in ignored `.env`.
Set `JHB_APPLICATIONS_ENABLED=1` to opt into the discovery-to-worker path;
`JHB_APPLICATION_EMAIL=1` opts into email notifications. Keep these disabled in CI.
The wrapper runs bounded batches, defaulting to three source checks and three
applications with two concurrent job planners and ten active drafts. Browser
operations remain serialized. See [pipeline.md](pipeline.md) for limits,
scheduler recovery and separate fixture/live results. Reset the opt-in
to zero to return to Phase 1 discovery. Never merge Phase 2 before its limitations
and actual review evidence have been assessed.
