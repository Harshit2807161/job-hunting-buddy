# Local application workspace

The dashboard uses Next.js/TypeScript and FastAPI over the existing SQLite
pipeline. Queue records, receipt-confirmed submissions, question handoffs and
Sheets delivery remain their original durable sources. No parallel database,
Celery worker, or application-submission service is created by the dashboard.

Install and build:

```sh
.venv/bin/python -m pip install -e '.[dashboard]'
cd frontend
npm ci
npm run typecheck
npm run build
```

From the repository root, run:

```sh
.venv/bin/python -m jhb.dashboard --port 8030
```

Open <http://127.0.0.1:8030>. The built frontend is served by the API itself;
there is one origin and no separate frontend proxy. Both the CLI server and
its request boundary accept local loopback connections only. Do not expose
this service through a tunnel or reverse proxy: its privacy model trusts the
candidate's local machine, rather than a remote account login.

The UI polls every five seconds. Confirmed daily counts use receipt timestamps
in `America/Los_Angeles`; the date picker controls the fourteen-day chart and
selected-day counts. Prepared counts use the saved review packet's creation
date. Current worker/queue counts are current states, not daily totals. An
uncertain submit, clicked button, saved draft, or queue `submitted` label alone
does not contribute a confirmed count. Pipeline heartbeat staleness, paused
automation, unavailable storage and screenshots are shown explicitly.

Application review shows the saved `review_inventory`, including required and
optional fields, answered and blank values, document filenames/variant,
independent reviewer issues, and quality incidents. A saved screenshot is the
preparation handoff, not a live browser view. The live form link opens the exact
known ATS job. Legacy packets without a complete inventory remain inspectable
but cannot be approved. Receipt counts remain intact when a quality incident
flags unanswered questions in a previously submitted application.

The candidate clicks **Approve and submit this application** separately for
each complete draft. The portal calls the approval ledger; it never clicks
Submit itself. Every optional blank must be acknowledged individually, without
preselected checkboxes. Approval binds the exact current packet, candidate
facts, screenshot and PDF bytes. Required blanks, missing inventory, changed
facts/documents, unsupported terminal adapters and uncertain outcomes prevent
approval/submission. Revocation uses the same ledger. An approved draft still
passes the pipeline's independent reviewer and fresh native checks before an
authorized browser action.

Question answers persist through `questions.answer`, preserving employer scope
and provenance. A blocked input handoff queues filling after all required
questions are answered, including while automation is paused. An explicit answer editing a reviewed draft safely revokes its
pending approval and queues a fresh filling pass, including during a global
pause. Queued edits remain idle until automation is resumed; the dashboard does
not remove the pause. Active, unknown or clicked final attempts prevent the
edit, and running, submitted, skipped and uncertain applications are never
requeued. Any newly prepared draft requires a new review and approval.
Answering a question does **not** approve submission. Credential and verification
code questions are excluded from this booklet UI. Substantive prompts requiring
the candidate's own wording must be answered by the candidate or deliberately
acknowledged as blank in the review.

`/api/v1` is intentionally narrow: overview, local health/session, job review,
validated screenshot, question answer, and approval/revocation. Mutations require
an exact same-origin header and a process-local CSRF token. Cross-site requests,
nonloopback clients and foreign/DNS-rebinding hosts are rejected. There is no
raw private file, credential, cookie, log, resume-download or arbitrary path API.
Screenshots must be PNGs beside an identity-matching private review packet;
symlinks and oversized artifacts are refused. Candidate data must never be
copied into frontend source or fixtures.

Validation:

```sh
.venv/bin/python -m pytest -q tests/dashboard
cd frontend
npm run typecheck
npm run build
```

Synthetic API and headless UI tests are dashboard validation, not live Browser
Use application validation. Tests need no API credentials, subscription login,
mail delivery, real browser session or real application submissions.
