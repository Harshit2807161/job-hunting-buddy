# Phase 2 v1: Glassdoor application preparation

Status: feature branch; fixture prototype plus live Browser Use search and Google
SSO navigation verified. Live worker integration and signed-in application
validation remain pending. Phase 1 remains tagged `v0.1.0`. No automated
application submission is in scope.

## Acceptance criteria

- Populate a private, provenance-bearing answer booklet from separate SDE and
  AI/ML resumes, preserve both document variants, and list missing decisions.
- Discover Glassdoor jobs through Phase 1; enqueue once by job identity. Seed and
  dry-run cycles must not start applications or create account records.
- Run a bounded observe → plan → validate → act loop. Codex CLI uses its existing
  local sign-in; no Platform API key or copied Codex token is needed.
- Fill only known answers. Unknown mandatory questions stop for input. Sensitive
  self-identification and eligibility fields never come from model inference.
- Support text, native select, radio, checkbox, file upload, and multiple pages.
  Unsupported widgets and off-site ATS redirects produce explicit handoffs.
- Preserve a private application packet, browser screenshot, event log, and
  resumable browser session. Notify the candidate and stop before final submission.
- Use Google SSO for live authentication. Reuse its local session after first
  sign-in; never fall back to email/password registration. Handle verification
  challenges explicitly and report local CAPTCHA solving only after a live test.
- Demonstrate discovery → queue → registration → saved login → fill → review
  locally, with zero calls to the fixture's final submission endpoint.

## Architecture

SQLite owns the queue and transitions. A single local worker claims one job at a
time; leases prevent duplicate claims of the same job and permit crash recovery.
Shared browser access also needs a global worker lock before cron is enabled.
The poller enqueues independently of email delivery. A wrapper drains one queue
item per scheduled cycle, keeping discovery independent of browser failures.

Live interactions use the official Browser Use CLI and local Chrome via CDP.
The fixture prototype still uses Playwright and requires migration before it can
serve as the live cron worker. Codex returns a JSON plan that
maps observed fields to booklet keys. It has no direct browser or credential tools
in this loop. Code rejects arbitrary answers, selectors, URLs, and terminal clicks.
All non-GET/HEAD application requests are blocked in the preparation browser;
synthetic fixture registration has a separate action and allowlist. This is conservative:
sites requiring server-side draft saves or GraphQL POSTs may need a later adapter.

Browser sessions and credentials stay on the candidate's machine. Passwords use
the OS keyring; the local demo can use an isolated private file vault. The model
never receives passwords. Live job-site authentication uses Google SSO, and
ambiguous account choice or consent is a handoff. Password registration belongs
only to the synthetic fixture and does not prove live Google sign-in.

The last-step review packet is a local HTML file. Live workers keep the browser
open during a bounded review/handoff window; a rerun refills from the booklet
after that window. Session cookies persist but arbitrary form drafts may not.
The user submits manually in the browser after reviewing; the worker has no submit
command. A review packet alone is never described as a submitted application.

## Validation and release

Required unit tests cover queue dedupe/leases, provenance, role routing, planner
validation, and input boundaries. Required browser fixtures exercise SDE and ML applications,
signup, CAPTCHA handoff/resume, missing answers, external redirects, and final
submission blocking. A live Glassdoor read-only probe records the real access
result separately; fixture success does not imply live-site compatibility.

Enable cron processing only with `JHB_APPLICATIONS_ENABLED=1`; existing discovery
continues otherwise. Roll back by unsetting it or checking out `v0.1.0` after
backing up the local ledger. Tests/CI never use personal resumes, real accounts,
or subscription credentials.
