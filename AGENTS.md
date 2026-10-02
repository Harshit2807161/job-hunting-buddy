# Repository guidance

Keep Phase 1's `main` and `v0.1.0` baseline reviewable. Phase 2 work belongs on a
feature branch until its tests and live limitations have been reviewed.

Use `skills/prepare-greenhouse/SKILL.md` for application planning and
`skills/tailor-cover-letter/SKILL.md` for cover-letter work. Read the candidate's
local source skill before editing a letter; references stay unchanged.

Use `skills/browser-use/SKILL.md` and the official Browser Use CLI for live browser
interaction. Always invoke `browser-use` through its CLI access mode, using the
default daemon and the existing local CDP endpoint. Do not use the Python browser
library or direct Playwright for live interaction. Serialize browser operations
and preserve tabs containing drafts. Fixture tests and direct Playwright checks
are not live Browser Use validation.

Use Google SSO for job-site authentication, as explicitly requested by the user.
Do not fall back to email/password registration. Reuse the existing Google
session when available; an unknown account choice or Google verification needs
an explicit handoff.

Private candidate data, browser sessions, application screenshots, and credentials
must remain in ignored local directories. Fixtures use synthetic candidates. Do
not submit real applications in development, test, or automation. Unknown answers
and verification challenges are explicit handoffs. Report fixture and live-site
validation separately.

Run `.venv/bin/python -m pytest -q`; browser tests require the local Chromium
installation. Do not use API credentials or subscription authentication in CI.
