# Candidate approval before submission

Review mode prepares applications autonomously and stops for a portal review.
Only the candidate's explicit **Approve and submit** click creates a
per-application portal approval. Saving an answer only resumes filling. A separately
enabled, finite Full autonomy window can delegate final review to an independent
agent without manufacturing portal approvals; see [overnight-submissions.md](overnight-submissions.md).

The local portal shows the chosen resume, retained final review screenshot,
and an inventory of every discovered question. **Open saved draft** focuses the
exact captured existing Chrome target through the official Browser Use CLI,
keeping its submission guard enabled. It never opens a fresh form as a fallback;
a closed, changed or disconnected target produces a handoff. **Original posting**
is a separate link to the public job page. Optional questions remain visible.
Each blank optional answer needs its own unchecked
**Leave blank** acknowledgment; required missing answers cannot be approved.
Older packets without a complete field inventory need a fresh preparation pass.
Prompts asking for the candidate's own non-AI wording require candidate input or,
when optional, an explicit decision to leave them blank.

`approvals.py` stores a durable approval and private two-hour authorization. The
revision binds the exact packet, relevant candidate facts, PDF hashes and review
screenshot. The API rejects stale views and duplicate approvals. The worker
rechecks the revision and live fields, invokes a separate read-only reviewer,
and performs two fresh retained-value audits. New questions, changed answers,
changed documents, revocation or expiry stop the terminal action.

`JHB_REQUIRE_PORTAL_APPROVAL=1` rejects legacy broad overnight authority even if
its old environment flag is accidentally enabled. The separate
`JHB_PORTAL_SUBMISSIONS_ENABLED=1` enables draining candidate-approved records;
it grants no authority on its own. Private pause/quarantine files take precedence.
Agents must never call the real approval endpoint on the candidate's behalf.

Before a terminal click the browser persists a write-ahead marker. Interrupted
or uncertain attempts cannot replay. Only a positive exact-job receipt records
a submission and starts the deduplicated spreadsheet synchronization step.
Failed spreadsheet delivery retries without submitting another application.

The dashboard runs on loopback and serves an explicit private artifact allowlist.
Host/client/origin checks and a same-origin CSRF token protect state-changing API
routes. It never serves the answer booklet, credentials or arbitrary local files.
Tests use synthetic candidates and mock approvals; real approval must come from
the candidate's portal interaction.

New preparation packets bind a fresh screenshot to that capture attempt, job,
packet timestamp and image SHA-256. Capture writes to a unique private temporary
PNG and replaces `browser.png` only after file and PNG validation. A failed
capture preserves filled answers and older evidence, parks the draft as a
bounded technical retry, and blocks review readiness. The portal does not display
the older image as current. Changed image bytes or a mismatched capture manifest
also block approval. Legacy packets without a capture manifest retain their
existing review policy until a fresh preparation pass replaces them. Capture
regressions use synthetic files and an isolated Chromium fixture, not live
candidate-browser validation.

Repeated education year questions are bound to their original degree record.
Identical start/end year labels produce separate indexed questions and explicit
answers apply only to that row. Older answers without a degree reference remain
in the audit history but cannot override every degree's verified dates. A fresh
verified fill resolves old technical handoffs without deleting their history.

Observed-form mappings reuse verified standing facts without creating new
candidate answers. Explicit US authorization, combined present/future US visa
assistance, relocation and employment restrictions have narrow question
templates; an unspecified or multi-country job does not inherit US eligibility.
Contact residence is separate from the job country. Verified current graduate
Computer Science study can satisfy the exact graduate-study question without
claiming completion or a particular degree focus.

Choice derivations bind the observed question, field reference and option list.
Recorded discovery through Simplify can select a unique observed Job Board
category. Office choices use only observed US locations when the job is in the
US and both relocation and office willingness are verified. Calendar availability
may select “One month +” only when its earliest possible date exceeds one
calendar month from assessment; a saved month never becomes an invented exact
start date. Existing verified profile URLs can populate an optional links field.
The exact optional cool-work prompt can receive a proposed achievement from
the selected verified resume and verified job description, still requiring
portal review. Travel and residency answers require explicit verified preferences.
A saved full calendar date can answer the exact residency-start question; a
month-only value cannot. Residency duration must match a unique offered range.
Degree focus and differently scoped screening questions remain candidate handoffs.
These mappings are covered by synthetic tests; live form results are recorded
separately in the adapter evaluation.

The observed Ashby React datepicker uses a text input with a local calendar
display. Only its exact date class and owned wrapper establish the
`MM/DD/YYYY` widget format. Preparation preserves the approved ISO source date,
types the local display format through native input, commits with Tab and
checks the calendar day after blur. Inventory and fresh retained-value audits
bind the observed format; changed widget metadata or an off-by-one day blocks
submission. Plain text controls receive no date normalization. The timezone
regression reproduces ISO midnight becoming the previous US calendar day in an
isolated Chromium fixture; live validation of the repaired path is separate.

Full-page review captures settle the exact application at the page top with
native Browser Use scrolling before capture, so sticky headers do not cover
fields in the middle of the saved image. A live Ashby validation confirmed four
retained answers unchanged before and after capture. This is screenshot and
retention evidence, not submission evidence.

Owned Ashby question instructions remain attached to their original question in
private observations and review evidence. The exact restriction instruction to
select N/A when based in California uses only verified US contact residence and
one unique offered N/A choice; it preserves the original general restriction
answer. An unfamiliar or incomplete note cannot silently reuse a general No.
A newly explicit candidate answer to an unfamiliar complete note must carry a
proof of the exact displayed instruction. Changed or newly present help text
invalidates a previous fresh submission audit and requires renewed review.
Synthetic tests cover owned versus nested/hidden notes, conditional choice
retention, context-bound candidate answers and zero-click stale-context handoffs.
These tests do not claim live application or submission validation.

Known booklet facts and document work are separate from candidate questions.
The portal routes a failed control with an exactly bound verified answer to
agent filling work. A missing cover-letter upload routes to document preparation;
an existing verified document needs upload verification. These tasks remain
visible in the application review and block approval until verified. A nonempty
catalog that cannot uniquely represent the saved fact remains a candidate question.
For example, a current disability answer does not establish lifetime medical
history. Published employer metadata can identify an already-known exact US
work-authorization question for routing, but the worker must still inspect its
owned native help and choices before using the answer.

Legacy pending cards can be reconciled explicitly with
`jhb.applications.question_routing.reconcile(bookpath)`. This bounded operation
updates per-context routing and history under the private booklet lock, preserves
candidate answers, and returns `agent_tasks`. It does not modify application
states, receipts, approvals or browser fields. Preparation scheduling remains a
separate guarded step. Candidate lists and emails also route contexts dynamically,
so stale cards cannot overwrite known facts while reconciliation is pending.

The review also warns when a different posting at the same company has the same
full title as a recorded confirmed application. It shows both locations and links
the earlier portal record. Different posting IDs do not establish different
internal requisitions, and matching titles do not prove duplication. This warning
is informational: it never changes receipt history, job state or approval rules.

`JHB_CURATED_COMPANY_INTEREST=1` enables a separate company-interest drafting route.
It runs before the legacy mission-sentence template and never silently falls back
to that template. A tool-less local Codex call drafts a brief paraphrased paragraph
from complete official JD units and the selected verified resume; a fresh read-only
Codex call checks every factual claim, the exact answer hash and its style.
Draft transport cites evidence IDs only; local validation attaches the exact full
source units before review and storage, so long JDs need not be copied in output.
Accepted text is still marked proposed and still needs per-application portal approval.
A reviewer approval is a writing check, never submission authority.

The private `workflow_preferences.narrative_style.company_interest_reference`
is a bounded style reference, not a source of facts about other companies or the
candidate. Word/character limits and this reference participate in the cache key,
as do question, official JD, selected facts and resume bytes. Existing explicit
candidate answers take precedence. Employer own-wording/no-AI guidance and unknown
factual history remain handoffs. Generation/reviewer outages and rejected prose
are retryable agent tasks (`narrative_generation`), rather than new candidate
questions. Both model calls must be injected in CI; no subscription authentication
or API credentials are used in tests. The flag stays disabled until integration
and live output review.

The candidate can discard one application through
`POST /api/v1/applications/{job_hash}/discard`. This same-origin, CSRF-protected
click records a durable exact-job exclusion, revokes pending approval, retires
only that job's question contexts and sets its queue state to `discarded`.
Packets, screenshots, answers and submission history remain available; current
review evidence is also archived privately at the discard boundary. Older worker
results, retries and rediscovery cannot resume a discarded application.

Cancellation is cooperative. The current serialized browser operation finishes,
then that application's next checkpoint exits without touching other workers or
the shared Browser Use daemon. The official CLI closes only the exact captured
target while holding the browser lane and only if it still displays that job.
A changed page is preserved. Busy or disconnected closes remain explicit pending
work, retried boundedly after the request, worker exit or a subsequent pipeline
cycle. The portal reports worker and tab outcomes separately; a discard record
alone does not claim a tab was closed. Unconfirmed closes are never blindly
replayed. A per-job lock orders discard against the final native Submit press;
already-submitted, uncertain or potentially clicked applications cannot be
misrepresented as discarded. These controls have synthetic DB, CLI-helper and
native browser fixture coverage; no candidate application was discarded during
development validation.
