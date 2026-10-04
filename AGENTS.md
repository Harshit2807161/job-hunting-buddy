# Repository guidance

Keep Phase 1's `main` and `v0.1.0` baseline reviewable. Phase 2 work belongs on a
feature branch until its tests and live limitations have been reviewed.

Use `skills/prepare-greenhouse/SKILL.md` for application planning and
`skills/tailor-cover-letter/SKILL.md` for cover-letter work. Read the candidate's
local source skill before editing a letter; references stay unchanged.
Deliver the validated company-named cover-letter PDF into the matching candidate
role directory beside its resume, as the source skill requires. Private build
artifacts do not replace that final delivery. Verify the resume variant before
upload and retain document provenance privately.

Select application guidance through `jhb/applications/boards.py` after resolving
the exact job identity. The user explicitly requested multi-board preparation
and submission, authenticated LinkedIn routing, independent review, and hourly
progress emails. Keep preparation and submission capability flags separate:
recognizing a host does not validate its adapter. Preserve observed mechanics
in the matching `skills/prepare-*/SKILL.md` and track fixture/live evidence and
remaining controls in `docs/board-adapter-evaluation.md`.

Use `skills/browser-use/SKILL.md` and the official Browser Use CLI for live browser
application filling. Always invoke `browser-use` through its CLI access mode, using the
default daemon and the existing local CDP endpoint. Do not use the Python browser
library or direct Playwright for live interaction. Serialize browser operations
and preserve tabs containing drafts. Fixture tests and direct Playwright checks
are not live Browser Use validation.

For Phase 1 application-source classification, use
`skills/check-application-source/SKILL.md` and the official Playwright MCP server
in isolated browsers, as requested by the user. Resolve redirects and embedded
ATS forms before routing; only exact jobs with reviewed adapters enter Phase 2. This
read-only checker must not attach to or reset the candidate's Chrome profile.
An explicitly enabled authenticated LinkedIn route may use the user's local
session to inspect the exact job's Apply action and observe its actual external
destination. Easy Apply requires its own scoped modal adapter. Independent jobs may plan in parallel, but Browser Use CLI operations in the
shared local browser must hold the browser-lane lock and reattach their own tab.

Use Google SSO for job-site authentication, as explicitly requested by the user.
An explicit site-specific exception recorded in the private answer booklet may
authorize password registration; do not infer a general fallback. Save approved
credentials in the local credential store and never emit their values. Reuse the existing Google
session when available; an unknown account choice or Google verification needs
an explicit handoff.

Private candidate data, browser sessions, application screenshots, and credentials
must remain in ignored local directories. Fixtures use synthetic candidates. Do
not submit real applications in development, test, or scheduled automation.
A later explicit request to submit one named application authorizes only that
interactive action after its retained answers and documents have been checked;
record the authorization and live receipt privately. Other drafts keep their guards.
The current user policy requires a separate explicit Approve click in the local
portal for every application. Keep `JHB_REQUIRE_PORTAL_APPROVAL=1`; old overnight
authorization cannot replace this approval. The portal shows every observed
question, including optional blanks, and requires individual acknowledgment of
each blank before approval. Approval binds one exact packet, candidate facts,
document bytes, and review screenshot, expires after two hours, and cannot be
reused after changes or a terminal attempt. Never click Approve on the user's
behalf. A separate read-only reviewer, two retained-answer audits and a positive
receipt remain required. See `docs/portal-review.md`. Legacy finite authorization
code remains for compatibility tests; it is disabled in the current local setup.
Technical repairs must pass checks before preparation or submission resumes.
Unknown factual answers and unresolved verification challenges are explicit handoffs.
Check actual role fit against the chosen resume before filling. A generic
software-engineer title does not qualify a robotics, embedded, or unrelated
specialist role. Respect exact-job exclusions. Employer prompts requesting the
candidate's own wording must be surfaced for user input or explicit blank review.
Explicitly authorized application email verification may use the candidate's
existing mailbox session. Report fixture and live-site
validation separately.

After an explicitly authorized application is confirmed submitted, record its
private receipt and sync the configured existing application spreadsheet as the
final pipeline step. Follow `docs/submission-tracking.md`; deduplicate existing
entries, preserve their column/date format, and verify the appended row. Review
drafts, submit attempts, and expired sessions are not confirmed submissions.
Keep tracker configuration, evidence and sync history private. A failed sheet
update must retain the confirmed submission for later reconciliation.

Run `.venv/bin/python -m pytest -q`; browser tests require the local Chromium
installation. Do not use API credentials or subscription authentication in CI.
