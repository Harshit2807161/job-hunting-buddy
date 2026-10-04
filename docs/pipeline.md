# Phase 1 → application preparation

Phase 1 records each new posting in `application_sources` before collapsing
duplicate email cards or marking postings notified. This includes direct ATS
links, LinkedIn/Indeed wrappers, and employer career pages. Seed and dry-run
polls do not schedule source checks. Recording a source check does not start a
browser; scheduled execution requires `JHB_APPLICATIONS_ENABLED=1`.

The source checker calls the official Playwright MCP server over stdio, in an
isolated headless browser. It records redirects, job-specific application links,
and embedded forms, and classifies supported ATS host patterns. It neither
attaches to the candidate's Chrome nor inherits candidate sessions. A page
requiring login, a verification challenge, or a choice among several different
application identities produces a handoff. Unsupported boards are classified
and recorded, but never dispatched to Phase 2. A closed posting is not prepared.

Only a confirmed canonical Greenhouse application URL enters the application
queue. The canonical region/board/job identity deduplicates different wrappers
and tracking links. An existing submitted application stays submitted.

The standing eligibility filter excludes jobs requiring a particular citizenship,
security clearance, TS/SCI, or a polygraph, including the ability to obtain or
maintain those requirements. Phase 1 checks available titles and descriptions;
the preparation worker verifies the exact job's official Greenhouse description
before accessing the candidate browser. Matched requirements and their source
are saved privately, and the application becomes `skipped`. Answering an older
question or resuming the queue cannot reactivate that state. This filter does not
infer the candidate's citizenship and does not exclude optional citizenship
questions, disclosure requirements, generic work authorization, background
checks, explicitly unnecessary clearances, or citizenship with a permanent-
resident alternative.

An unavailable or empty official description prevents preparation. Stale,
mismatched, or altered cached descriptions trigger a fresh official fetch.
Failed verification produces a `waiting_input` operational handoff
with no candidate question; no application tab is opened or filled. Retry only
after the official description can be verified. Fresh cached descriptions must
match the exact official job URL and their recorded content hash.

Greenhouse preparation uses the registered Browser Use skill and its official
CLI, default daemon, and existing local Chrome CDP endpoint. Independent job
planners can run concurrently. Every browser operation holds the shared browser
lane and selects that job's tab before acting. Tabs containing drafts remain
open. Final submission is guarded; review, unknown answers, login, and CAPTCHA
are explicit stopping points. Google SSO is the only live authentication mode.

The two browser tools have separate access paths. Source discovery uses the
official **Playwright MCP** protocol in a temporary isolated browser. Application
filling uses `skills/browser-use/SKILL.md` and the official **Browser Use CLI**
against the existing local session. The application planner uses
`skills/prepare-greenhouse/SKILL.md`; cover-letter preparation follows
`skills/tailor-cover-letter/SKILL.md` and the candidate's local source skill.
Direct Playwright application filling is confined to synthetic fixtures.

## Run and inspect

Install the pinned official MCP server and its local Chromium dependency before
running source checks. Node.js with `npm` is required; candidate credentials are
not passed to the server.

```sh
npm ci
PLAYWRIGHT_BROWSERS_PATH="$PWD/.local-browsers" npx playwright install chromium
# Separate Chromium revision for Python fixture tests:
PLAYWRIGHT_BROWSERS_PATH="$PWD/.local-browsers" .venv/bin/python -m playwright install chromium
```

```sh
# Read-only isolated source classification; no application filling.
.venv/bin/python -m jhb.applications.cli classify 'https://example.com/careers/job'

# Explicitly run a bounded batch using approved field/key bindings.
.venv/bin/python -m jhb.applications.cli pipeline

# Same opt-in gate used by the launchd wrapper.
.venv/bin/python -m jhb.applications.cli pipeline --if-enabled

.venv/bin/python -m jhb.applications.cli source-status
.venv/bin/python -m jhb.applications.cli status
.venv/bin/python -m jhb.applications.cli questions

# Interactive answers are saved privately rather than passed on the command line.
.venv/bin/python -m jhb.applications.cli answer-question q_QUESTION_ID

# Retry a source after its handoff has been resolved.
.venv/bin/python -m jhb.applications.cli source-resume SOURCE_JOB_HASH
```

New application questions are employer scoped, carry their exact wording and
form context, and are deduplicated across applications. Answering a question
automatically resumes only `waiting_input` jobs whose required pending questions
are all answered. It cannot resume review-ready, submitted, or skipped applications.
`--decline` is available for optional questions; required questions require an
explicit answer. Verification codes and credentials do not enter this ledger.

## Reusing approved answers

Explicit user answers and standing preferences take priority over imported
profile observations. The worker reuses approved identity, work authorization,
sponsorship, disclosure, and role-specific resume facts through exact field
aliases. New wording without an approved binding remains a question handoff.
Employer-specific certifications stay employer scoped; an answer for one
employer does not authorize another employer's consent.

The private booklet can also carry explicit standing rules for relocation,
office/HQ willingness, career-fair contact, and preferred name. Their application
is restricted to supported question templates. Preferred first name can remain
blank when optional and use the approved first name when required. A career-fair
contact rule can select `N/A` when available; it cannot invent a person's name.

Salary expectations use the arithmetic midpoint of one recognized advertised
annual USD base salary range. When no range is recognized, the worker uses the
user's approved annual fallback. Multiple distinct ranges require a location or
range decision; hourly, foreign-currency and total-compensation text is excluded
from range extraction. Salary values and their evidence remain private.

School answers use original verified education records, with an explicit rule
to reuse those facts for supported institution-specific screening questions.
Institution matching normalizes punctuation and uses exact names or an approved
alias; it does not substitute a different campus. Employer catalog mappings such
as `Other` never replace the actual institution in the booklet or resume.

Private evidence and notifications live in `private/source-checks`,
`private/applications`, and `private/notifications`. Review-ready drafts and new
required questions use the configured recipient when `JHB_APPLICATION_EMAIL=1`.
Technical failures and optional questions remain local. Sustained source access
blocks produce one grouped digest after an hour. Delivery keys persist across
worker restarts; category-wide email backoff starts at five minutes and grows to
six hours, so newly discovered jobs cannot bypass a failing mail channel.
Failed email delivery retries independently of browser preparation. The pending question outbox reflects the current private ledger,
including earlier batches and questions that have since been answered.

## Scheduled limits and recovery

`run_poll.sh` executes Phase 1 in its existing conda environment, then invokes
the pipeline in `.venv`. The existing launchd agent runs this wrapper every
15 minutes. Its logs are ignored local files under `data`.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `JHB_SOURCE_BATCH_SIZE` | 3 | Source checks per cycle |
| `JHB_APPLICATION_BATCH_SIZE` | 3 | Application preparations per cycle |
| `JHB_PIPELINE_CONCURRENCY` | 2 | Independent source checks / job planners |
| `JHB_MAX_ACTIVE_DRAFTS` | 10 | Capacity for running and handoff drafts |
| `JHB_SOURCE_TIMEOUT_SECONDS` | 90 | Source check budget |
| `JHB_APPLICATION_TIMEOUT_SECONDS` | 600 | Application preparation budget |

Work exceeding a batch or draft limit stays queued. One manager lock prevents
overlapping cron and manual workers. Claims have leases; expired claims recover,
and repeated crashed claims stop after three attempts. Transient source errors
use bounded exponential backoff. Classified application transport/mechanics
failures retry after five minutes, then ten minutes, with at most three preparation
attempts. Generic validation errors do not qualify for automatic recovery.
Review and verification handoffs do not retry
without explicit intervention. Closing or submitting a draft manually does not
automatically change its ledger state; update the recorded state before reusing
capacity. Record actual submission evidence with `confirm-submission` rather than
changing a draft state based on a click. The pipeline does not delete tabs or
drafts to reclaim space.

If a user answers a question while a worker still holds an older booklet
snapshot, the newer explicit answer is preserved. Once all current required
questions have answers, that job queues a fresh pass in the next bounded cycle.
An incompatible answer stays at a question handoff rather than retrying forever.

## Final step: submission tracking

After a separately authorized submission succeeds, the agent records the exact
job's private success receipt with `confirm-submission`. This durably records the
submission and automatically synchronizes the configured existing spreadsheet.
The final stage of subsequent pipeline cycles reconciles pending sheet deliveries;
it never submits drafts or infers success from preparation results.

Sheet logging preserves the existing eight-column format, checks canonical ATS
job links and legacy employer/role/date entries, and verifies every appended row.
An uncertain write stays pending for read-only reconciliation instead of being
blindly repeated. Configuration, credentials and receipts remain private; CI
uses synthetic tool responses. See [submission-tracking.md](submission-tracking.md)
for configuration and receipt recording commands.

## Validation boundaries

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m jhb.applications.cli demo
```

The demo uses synthetic Phase 1 postings, an injected source classifier, and
actual guarded browser filling against local fixture forms. It includes a
direct Greenhouse link, a simulated LinkedIn-to-Greenhouse result, and a Lever
posting; two Greenhouse jobs reveal one new employer question, an explicit
synthetic answer resumes both, and both stop at review with zero submissions.
This proves the orchestration and handoff flow. It is not evidence of live MCP
resolution or live Browser Use behavior. Dedicated MCP fixture tests exercise
the actual MCP protocol; live site results must be reported separately.

Synthetic eligibility regressions cover required citizenship and clearance,
negated requirements, disclosure-only questions, permanent-resident alternatives,
unavailable descriptions, and proof that excluded jobs never construct a browser
client. A separate live-description audit excluded a previously prepared draft
whose official posting required citizenship and clearance; its tab remained
untouched. Another audited draft passed the filter and reached review using the
Browser Use CLI. No submission is part of these checks. Phase 1's `main` and
`v0.1.0` baseline remain unchanged; these changes belong to the Phase 2 branch.

### Live source validation, 2026-10-02

These results come from actual isolated Playwright MCP source checks. Ignored
`private/source-checks/live-validation-*.json` files retain navigation and form
evidence. No candidate profile or browser session was supplied to these checks.

| Source | Observed result |
| --- | --- |
| OneImaging | Rendered individual hosted Greenhouse job confirmed |
| Parallel Systems | Old board URL redirected to the confirmed hosted Greenhouse job |
| Block employer career page | Rendered embedded Greenhouse application confirmed after navigation |
| Pinterest | Cloudflare verification handoff; no target guessed |
| RTX via Recruitics | Actual employer redirect, then Cloudflare verification handoff |
| Cedars-Sinai via LinkedIn | Observed Apply destination required login |
| Atlas Energy Solutions | Individual Greenhouse job redirected to its board error page; recorded closed |

Separately, live Browser Use CLI preparation on OneImaging and Parallel Systems
verified known contact fields, disclosures, and role-specific resume uploads,
then stopped for new employer questions. Subsequent answers and tailored
documents are being validated in fresh passes. These source checks do not
establish that every new draft is review-ready or that blocked sites can be
completed autonomously.

The final connected live check prepared Block's embedded form through the same
scheduled source queue and Browser Use worker. After explicit new answers, its
required and optional controls were accounted for and the draft stopped at
review. Two hosted Greenhouse drafts also reached review. A scheduled Phase 1
poll independently found two fresh postings: Workday was recorded without
preparation, and an unresolved LinkedIn destination became a source handoff.
Private candidate artifacts contain the field audits and notifications.

Public Greenhouse job metadata supplies country context only when the location
explicitly names one country. City-only or ambiguous locations remain unknown;
a US authorization answer cannot substitute for a Canadian one. Standing
compliance, signature, prior-employer and education-catalog preferences require
explicit candidate authorization. They are not enabled by the example profile.

GitHub's Ubuntu runner restricts Chromium user namespaces. CI explicitly opts
into a sandbox override for the trusted localhost-only MCP fixture. The same
flag has no effect on live source checks; those retain MCP's browser sandbox.


### Reliability validation, 2026-10-04

The live repair reproduced a slow planning call, inactive-tab input timeouts,
interrupted dropdown overlays, and a screenshot failure after a job redirect.
Known answer bindings now avoid model calls. The CLI can wake its exact owned
job after a frozen-page/native-scroll timeout, close an interrupted dropdown
without changing its selection, and recheck retained values. A redirected job
is handed off before further filling. Screenshots are optional evidence and
cannot turn a valid preparation result into a file-not-found failure.

One existing Phase 1 Greenhouse job completed through the real connected
pipeline using the local Browser Use CLI: 22 retained fields/documents, zero
missing required answers, a working final submit control, and its submission
guard still enabled. SMTP accepted its review notification; the delivery key
was persisted. Two recovered jobs redirected outside supported individual forms
and stopped as unsupported. A fresh scheduled wrapper cycle exited successfully
without another review or failure email. Exact job and browser evidence stay in
ignored private artifacts. These live checks are separate from synthetic tests.

Additional fixture regressions cover minimal launchd PATH, persistent mail
backoff and concurrent delivery claims, retry leases and attempt limits,
required clearance headings, export-control alternatives, menu/geometry recovery,
and salary-category retention. They do not prove universal ATS compatibility or
CAPTCHA solving. Unsupported controls and genuinely unknown factual answers
remain explicit handoffs; the pipeline never changes an answer to improve
screening results and never submits scheduled applications.


A subsequent live education audit verified four indexed year fields across two
education records. Chrome exposes these numeric inputs as AX `spinbutton`
controls. The adapter now recognizes that role and extracts calendar years only
from verified valid original dates, retaining expected-graduation provenance.
Malformed dates and different record indexes cannot receive a guessed year.
