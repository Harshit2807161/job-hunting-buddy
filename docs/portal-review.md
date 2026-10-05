# Candidate approval before submission

The current workflow prepares applications autonomously and stops for a portal
review. Only the candidate's explicit **Approve and submit** click creates a
per-application approval. Saving an answer only resumes filling.

The local portal shows the live application link, chosen resume, retained final
review screenshot, and an inventory of every discovered question. Optional
questions remain visible. Each blank optional answer needs its own unchecked
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
