# Multi-board application dispatch

Phase 1 records discovered URLs. Phase 2 identifies the actual job destination,
selects a reviewed adapter and its skill, checks the official description, and
fills the candidate's existing Chrome through the registered Browser Use CLI.

`boards.py` separates exact identity, preparation capability, submission
capability and planning skill. Greenhouse's existing durable hashes remain
unchanged. Canonical identities deduplicate tracking parameters, posting versus
application paths and multiple discovery wrappers. Unsupported boards retain
their source evidence and an actionable adapter handoff rather than receiving a
generic filler that assumes their controls.

Verified resolved sources can be replayed into newly supported adapters without
resetting prepared, submitted or uncertain applications. Existing positive
receipts also prevent preparation of previously submitted manual applications.
Source access handoffs remain independent from candidate questions.

## Authenticated LinkedIn

`JHB_LINKEDIN_LOCAL_RESOLUTION=1` enables the explicitly authorized local source
route. Its fixed CLI dispatcher opens an exact numeric LinkedIn job, observes
Apply, follows the native action and waits for the actual destination. The
external page is then classified through isolated Playwright MCP. LinkedIn
safety redirects and inactive-tab navigation have been validated live. Easy
Apply is recorded as a distinct observed capability, never inferred from the
URL; only its reviewed modal adapter may fill that form.

## Candidate approval and separate reviewer

Preparation captures all observed questions, including optional blanks, and a
full-page review screenshot. The candidate reviews these at the local portal.
Each blank needs an explicit acknowledgment, and only a per-application Approve
click authorizes the final action. See [portal-review.md](portal-review.md).

The filler retains field values and document provenance. After a fresh browser
audit, `application_review.py` starts a separate read-only `codex exec` session
using the existing local login. It reviews the exact retained snapshot against
the selected role's verified profile, original education records, approved
preferences and official job description. It cannot supply replacement answers.

A successful verdict binds the job, authorization, packet, document hashes and
upload receipts, retained snapshot, and approved booklet digest. The final
runtime rechecks this binding and performs two fresh browser audits before the
write-ahead terminal marker. Changed answers or documents invalidate approval.
A reviewer outage or rejected application stays held; no approval fallback
exists. Synthetic tests use an injected reviewer and no subscription credentials.

## Hourly reporting

`JHB_HOURLY_PROGRESS_EMAIL=1` enables the user's requested receipt-based progress
emails during the separate finite reporting window. Reporting grants no
submission permission. The first report is due after one
hour, including when there are zero submissions. Durable period keys and mail
channel leases prevent every cron cycle from sending the same report. Failed
delivery backs off. Counts come from positive receipts, with verified sheet
delivery reported separately; drafts and uncertain attempts are excluded.

The pipeline reconciles spreadsheet delivery as its final stage. A separately
scheduled `python -m jhb.applications.hourly_reports --watch` process keeps
reporting independent of a long browser preparation cycle and exits at expiry.
Technical repair supervision also remains a separate, serialized finite process.

## Validation

Run `.venv/bin/python -m pytest -q`. Job identity, saved-record editors, document
mutation detection, independent reviewer bindings, no-replay attempts and mail
deduplication are fixture-tested with synthetic data. The board evaluation
document records live coverage separately. A recognized board or passing fixture
does not establish live compatibility with every employer's custom form.
