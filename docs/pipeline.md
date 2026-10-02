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

# Explicitly run a bounded batch with the saved local Codex sign-in.
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
are all answered. It cannot resume review-ready or submitted applications.
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
`private/applications`, and `private/notifications`. Application emails and
source handoff emails use the existing configured recipient when
`JHB_APPLICATION_EMAIL=1`. Failed email delivery retries independently of browser
preparation. The pending question outbox reflects the current private ledger,
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
use bounded exponential backoff. Review and verification handoffs do not retry
without explicit intervention. Closing or submitting a draft manually does not
automatically change its ledger state; update the recorded state before reusing
capacity. The pipeline does not delete tabs or drafts to reclaim space.

If a user answers a question while a worker still holds an older booklet
snapshot, the newer explicit answer is preserved. Once all current required
questions have answers, that job queues a fresh pass in the next bounded cycle.
An incompatible answer stays at a question handoff rather than retrying forever.

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
