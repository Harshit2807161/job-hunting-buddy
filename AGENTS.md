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

For explicitly requested manual Ashby applications, use
`skills/prepare-ashby/SKILL.md` and the exact-job-scoped manual adapter. Manual
preparation on another ATS requires explicit authorization for that job and
observed controls; it does not enable scheduled preparation for that board.
The automated Phase 1-to-Phase 2 dispatcher remains Greenhouse only. Track
adapter evidence and limitations in `docs/board-adapter-evaluation.md`.

Use `skills/browser-use/SKILL.md` and the official Browser Use CLI for live browser
application filling. Always invoke `browser-use` through its CLI access mode, using the
default daemon and the existing local CDP endpoint. Do not use the Python browser
library or direct Playwright for live interaction. Serialize browser operations
and preserve tabs containing drafts. Fixture tests and direct Playwright checks
are not live Browser Use validation.

For Phase 1 application-source classification, use
`skills/check-application-source/SKILL.md` and the official Playwright MCP server
in isolated browsers, as requested by the user. Resolve redirects and embedded
ATS forms before routing; only confirmed Greenhouse jobs enter Phase 2. This
read-only checker must not attach to or reset the candidate's Chrome profile.
Independent jobs may plan in parallel, but Browser Use CLI operations in the
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
Unknown factual answers and unresolved verification challenges are explicit handoffs.
Explicitly authorized application email verification may use the candidate's
existing mailbox session. Report fixture and live-site
validation separately.

Run `.venv/bin/python -m pytest -q`; browser tests require the local Chromium
installation. Do not use API credentials or subscription authentication in CI.
