# job-hunting-buddy

Discover jobs, prepare applications in the candidate’s local browser, and keep
submission under the candidate’s control through a private review dashboard.
Phase 2 is developed on a feature branch; `main` and `v0.1.0` retain Phase 1.

## Phase 2: prepare, review, approve

The local Next.js/FastAPI dashboard shows application status, every observed
question, selected documents, screenshots, and receipt-confirmed daily counts.
Only an explicit per-draft **Approve and submit** click authorizes the final
browser action. Changed answers or documents require another review; uncertain
attempts never replay automatically. Candidate data stays in ignored local files.

Saving an answer queues eligible applications for another filling pass; it does
not approve submission. The portal reports queued, paused, and still-blocked
states as the workers progress. **Open saved draft** focuses the captured existing
Chrome tab without opening a new form. **Original posting** is a separate link.
Confirmed totals include imported historical receipts; daily counts use the
actual confirmation date in America/Los_Angeles, not the time of tracker import.

Phase 1's scheduled discovery, preparation, and approved-submission dispatch are
separate lanes. Finite worker watches stop at their configured expiry; discovery
keeps its own cadence. The persistent approved worker consumes only unexpired
per-draft portal approvals. See the local worker guide for these distinct modes.

- [Run the dashboard](docs/dashboard.md) and [local workers](docs/local-worker-service.md).
- [Review and approval contract](docs/portal-review.md).
- [Phase 1 → Phase 2 pipeline](docs/pipeline.md) and [board coverage](docs/board-adapter-evaluation.md).
- [Submission receipt and spreadsheet tracking](docs/submission-tracking.md).

Greenhouse and Ashby support guarded submission after approval. Workable and
Lever currently support preparation; other recognized boards retain explicit
adapter handoffs. Fixture tests and live compatibility are documented separately.

## Phase 1 discovery

Polls for new-grad SWE/SDE and ML/AI/Data Science openings in the USA and emails
you when one appears.

Phase 1 of the plan in [`RESEARCH.md`](RESEARCH.md): **discovery only**. No browser,
no form filling, no automation surface. Per the research, this is where most of the
available edge already is — interview odds are up to 8× higher when you apply within
four days of a posting going live.

## Sources

| | SimplifyJobs | JobSpy |
|---|---|---|
| Role | Primary spine | Secondary breadth |
| Matching US new-grad roles | ~1,180 live | ~143/run after filtering |
| Direct employer ATS link | ~100% | ~14% |
| Poll cost | 1 GET, 14 MB, <1s (304 when unchanged) | 16 queries, ~3 min |
| Cadence | every 15 min | every 1 h (rate-limited) |

Measured 2026-09-14: 59% of JobSpy's matched companies are absent from Simplify
(Lockheed Martin, Raytheon, MITRE, IBM, HPE, AWS, TikTok USDS), which is why both
run. JobSpy's Google and ZipRecruiter backends return nothing / HTTP 403 and are
disabled in `config.JOBSPY_SITES`.

## Setup

```bash
conda create -n jhb python=3.12 -y
conda run -n jhb pip install "git+https://github.com/speedyapply/JobSpy.git" requests pytest
```

Then fill in `.env` (gitignored):

```
JHB_SMTP_USER=youraddress@gmail.com
JHB_SMTP_PASS=<16-char Gmail App Password, no spaces>
JHB_EMAIL_TO=dhankharharshit@gmail.com
```

App Password: <https://myaccount.google.com/apppasswords> (needs 2-Step Verification on).

## Usage

```bash
python -m jhb.poll                      # one cycle, then exit
python -m jhb.poll --no-jobspy          # Simplify only (fast)
python -m jhb.poll --dry-run            # render email to data/*.html, send nothing
python -m jhb.poll --watch 15           # loop every 15 min in the foreground
python -m jhb.poll --stats              # ledger + recent runs
python -m jhb.poll --test-email         # send one sample email
python -m jhb.poll --seed               # re-seed: record everything, email nothing
```

Scheduled runs: `.\register_task.ps1` registers **JobHuntingBuddy** to run
`run_poll.cmd` every 15 minutes, logging to `data/poll.log`.

```powershell
Get-ScheduledTask JobHuntingBuddy         # status
Start-ScheduledTask JobHuntingBuddy       # run now
Unregister-ScheduledTask JobHuntingBuddy -Confirm:$false
```

On macOS, `./register_launchd.sh` installs the launchd agent **com.jobhuntingbuddy.poll**
(`~/Library/LaunchAgents/`), which runs `run_poll.sh` every 15 minutes and logs to
`data/poll.log`.

```bash
launchctl print gui/$(id -u)/com.jobhuntingbuddy.poll    # status
launchctl kickstart gui/$(id -u)/com.jobhuntingbuddy.poll  # run now
launchctl bootout gui/$(id -u)/com.jobhuntingbuddy.poll    # remove
```

## How it stays cheap and safe

- **Conditional GET.** The Simplify poll sends `If-None-Match`; an unchanged feed
  returns `304` with no body, so a 15-minute cadence does not re-download 14 MB
  96 times a day.
- **Idempotent ledger.** `dedupe_hash = sha256(company|title|source_id-or-url)` is the
  PRIMARY KEY, so re-polling unchanged data is a no-op.
- **Seed mode.** The first run records all ~1,170 current listings as already-seen,
  so you get zero emails on day one and only genuinely new postings after.
- **Burst guard.** More than 25 pending in one cycle sends a single summary instead
  of 25 emails — a feed reshuffle or schema change can't flood your inbox.
- **SMTP retry.** Gmail intermittently drops the connection; sends retry three times
  with backoff — but only when the message was never handed over. A failure during
  QUIT means the mail was already accepted, so retrying would send a second copy.
- **Stable Message-ID.** Derived from the exact set of jobs, so a duplicate delivery
  is collapsed by Gmail instead of showing twice.
- **Single-instance lock.** `data/poll.lock` (O_EXCL, stale after 25 min). Task
  Scheduler's `IgnoreNew` only stops the scheduler overlapping itself — a manual
  `--once` could still race a scheduled fire, with both reading the same pending
  set and both emailing it.
- **Role collapsing.** Simplify lists one row per posting, so RTX "Software Engineer 1"
  arrived 17 times across 11 cities and Palo Alto Networks "Software Engineer" 21
  times. Notification groups by `(role_key, source)` and emits one card carrying
  every location — 12.8% of ledger rows collapse. Cross-source duplicates are kept
  deliberately: the Simplify row has the employer ATS link, the JobSpy row the
  LinkedIn one, and both are useful.

## No liveness checking, deliberately

Postings are never HTTP-checked before emailing, so an occasional stale link gets
through. This was built and then removed: against 80 live listings, closure-text
matching wrongly flagged **7 (8.75%)** as dead — including a JHU APL *Software
Engineering/ML/Data Scientist New Grad* role — because iCIMS and Apple ship their
404 error strings inside the JS bundle of every page. Against 120 closed listings
it caught only 2, and Workday (~75% of them) renders client-side and returns an
empty body either way.

Missing a real opening costs far more than clicking a stale link.

## Filters

Defined in `jhb/matching.py` and `jhb/config.py`, all regex, no model:

- **SWE/SDE** — software engineer/developer, SDE, SWE, Member of Technical Staff,
  backend/frontend/full-stack/platform/infra/embedded engineer, programmer.
- **ML/AI/DS** — ML/AI/deep learning/NLP/CV/LLM + engineer|scientist|researcher, plus
  data scientist, applied scientist, research scientist, decision scientist.
- **Excluded** — senior/staff/principal/lead/manager/director, level ≥ II or 2,
  interns and co-ops, and `data analyst`/`data engineer`/`business analyst`.
- **US only** — 710 distinct location strings handled: `San Jose, CA`, `NYC`, `SF`,
  bare state names, `Remote in USA`, with a non-US country blocklist.
- **Company blocklist** — staffing body shops and aggregators reposting others' roles.

## Tests

```bash
conda run -n jhb python -m pytest tests/ -q
```

The Phase 1 baseline has 105 tests covering title classification, seniority
exclusion, US location parsing, ledger idempotency, role collapsing, and
duplicate-email prevention. Run the current Phase 2 suite from the project venv:

```bash
.venv/bin/python -m pytest -q
```

Browser fixtures require the installed local Chromium. These tests are separate
from live Browser Use compatibility and never use candidate credentials in CI.
