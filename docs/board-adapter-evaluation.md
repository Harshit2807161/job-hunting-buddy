# Job-board adapter evaluation

The discovery pipeline classifies multiple ATS providers. The current registry
enables preparation for Greenhouse, Ashby, Workable and Lever. Workday, LinkedIn
Easy Apply, SmartRecruiters and iCIMS retain explicit limitations and are not
enabled for generic preparation. Submission capability is separate and currently
limited to Greenhouse/Ashby, with mandatory candidate approval through the local
portal. Phase 1's `main` and `v0.1.0` remain unchanged on the Phase 2 branch.

The historical manual evaluations below establish observed mechanics, not current
submission authority. Live validation exposed a required-only completeness defect:
substantive optional questions could remain blank. Every new review packet now
includes the complete question inventory; the portal requires individual blank
acknowledgments and an explicit per-draft Approve click. See
[portal-review.md](portal-review.md). Exact incident evidence remains private.

Ashby benefits from a shared adapter: four employers share the same field
and selection patterns. Workable needs a smaller layer for its uploads and
repeaters. Workday needs tenant authentication and a separate wizard/repeater
adapter. The existing CLI transport, browser lane and tab scope
are useful across all three; each board does not need its own browser controller.

## Live evidence

Public HTTP reads verified each Ashby posting's exact UUID, title and complete
description from the page's embedded `window.__appData.posting` object. The four
descriptions contained no excluded citizenship, clearance or polygraph
requirement. No authenticated API endpoint was guessed. Official company pages
supplied company-specific prose material.

Live application interaction used the registered Browser Use CLI, its default
daemon and the existing local Chrome session. Each operation held the shared
browser lane and selected the owned job target. Source-check evidence from
isolated Playwright MCP browsers is distinct from candidate-browser filling.
Screenshots, DOM/AX snapshots, source hashes and field audits remain ignored
private artifacts; candidate answers are not reproduced here.

| Posting | Board | Observed mechanics and outcome |
| --- | --- | --- |
| [EvenUp — Software Engineer (New Grad), AI Entities](https://jobs.ashbyhq.com/evenup/19eb22cd-9540-49ed-840b-6422714413b5) | Ashby | Required text, country combobox, sponsorship/office radios, Yes/No employment buttons and PDF resume. All 13 required groups retained; guarded draft ready for review. |
| [Clay — Early Career Software Engineer](https://jobs.ashbyhq.com/claylabs/16778e12-31cb-4ca1-a321-7f629a7cf273/application) | Ashby | Adapter found 21 fields including a classless graduation radio group and optional surveys. All eight required fields retained in guarded live validation. |
| [Harvey — Software Engineer, New Grad (2027)](https://jobs.ashbyhq.com/harvey/b0996df6-6b6e-42be-a4f9-0084536068f5/application) | Ashby | Adapter found 23 fields and verified all 22 required fields, including a selected location autocomplete, checkbox groups and team matching. The calendar requires an explicitly approved exact date; it was handled through observed calendar controls. |
| [Parasail — Software Engineer, Forward Deploy — New Graduate '27](https://jobs.ashbyhq.com/parasail/da595923-4e35-4ba1-875d-383276069cf7) | Ashby | Actual form requires only name, email and PDF resume. All three retained; guarded draft ready for review. No graduation-screening question appeared, so posting eligibility remains a review consideration. |
| [Texas Sports Academy — AI Operations Associate](https://apply.workable.com/texas-sports-academy-main/j/FC4151F98F/) | Workable | Public application, native contact fields, separate resume/avatar uploads, text cover letter and education/experience repeaters. Manual preparation reached review with no submission; reusable Workable execution remains incomplete. |
| [Broadridge — Junior Full Stack Software Engineer (Hybrid)](https://broadridge.wd5.myworkdayjobs.com/careers/job/Newark-NJ/Full-Stack-Software-Engineer--Hybrid-_JR1086388) | Workday | No Google option appeared. An explicitly approved tenant account unlocked the wizard. Live CLI validation reached guarded Review, step six after authentication, with contact information, resume, education/employment repeaters, all nine application questions, voluntary disclosures and self-identification retained. No final submission occurred. |

These results cover the observed postings and controls. A ready draft means
reviewable field completion, not a submitted application or a guarantee of
employer eligibility. No final-submit action is part of the evaluation.

## What should be shared, and what needs an adapter

| Layer | Recommendation and evidence |
| --- | --- |
| Browser access, tab selection, locking, guarded terminal actions | Share across boards. An inactive local tab accepted inserted text while silently ignoring native mouse/keyboard actions on Ashby and Workable. Activating only the owned target under the lane resolved the demonstrated failures. This is not an ATS-specific defect. |
| Click targets and fixed overlays | Share hit-testing and bounded geometry checks. Workday's sticky footer covered a settled target center; viewport-valid coordinates alone did not establish that the intended control received the click. Scroll and reacquire the exact visible option before clicking. |
| Ashby field observation and execution | Keep a small shared Ashby adapter. Question-level requiredness, plain-wrapper radio fieldsets, grouped Yes/No buttons, ID-less controls and separate resume/autofill inputs recur across employers. Exact-job scope prevents one form's observation from being used on another. |
| Employer questions and conditional fields | Map observed wording to approved booklet answers; do not create a separate company adapter for each question. Re-observe after selections reveal new controls. Unknown factual or preference decisions are handoffs. |
| Workable native inputs | Reuse native text, file and button actions with observed Workable labels and attributes. Resume and avatar file inputs must be distinguished; dynamic input IDs alone are insufficient. |
| Workable education/experience repeaters | Add reusable repeater support before automatic dispatch. Live manual work identified Add/Update/Edit controls, month/year dates, current-employment controls and duplicate summary textarea IDs. A future graduation date rejected by the picker was retained truthfully in degree text and the original resume, with the end-date field left blank. |
| Workday tenant access and multi-page state | A dedicated adapter is justified by the seven-step wizard, saved repeaters, degree/major catalogs, date spinbuttons and sticky-footer occlusion. The observed tenant has no Google route; explicit site-specific instructions govern account creation. Catalog values must be checked after the popup closes; month/year controls cannot be treated as ordinary textboxes. |

The observed Workday wizard returned a transient server error while advancing.
One site-directed refresh recovered the saved contact and experience sections,
but questionnaire selections reset. Recovery must re-audit each restored section
and re-enter only previously verified answers before continuing. The final Review
page confirmed retained values across all sections. No Skills field, education
date controls, sexual-orientation question or separate cover-letter upload was
offered on this tenant's observed form; other tenants may differ.

The current `ManualATSCLI` scopes Ashby, Workable and Lever around the shared CLI
transport and is wired into the scheduled dispatcher. Location autocomplete
uses an approved query and exact choice scoped to that field's visible listbox;
the selected value and closed popup are verified. Calendars and new repeater
shapes still need observed, bounded handling. Existing Greenhouse
behavior remains restricted to Greenhouse application identities.

## Answer and document preparation

Use the board-specific preparation skill selected by `boards.py` for each
scheduled or manually queued job. Match
the selected resume to the actual role responsibilities, preserving its source
path and hash. An AI-related engineering team does not automatically require the
ML resume. Match skills to both the job description and selected resume; include
other relevant skills or courses only from verified candidate records.

Reuse approved standing preferences and known factual answers. Keep university,
GPA and graduation status consistent within the same education record; never
change an expected degree into a completed one to fit an employer's wording.
Retain any mismatch between a posting's eligibility criteria and the original
resume as a review note.

Subjective answers follow the user's shared writing guidance: brief first-person
language, concrete company/product reasons, and a short verified experience
connection when useful. Experience questions use relevant source facts, with
AWS before AnyFeast when both fit. Company claims come from official sources;
assistant drafts are suggestions, not candidate profile facts. New required
authorization, sponsorship, demographic, consent or preference decisions need
the candidate's answer.

## Synthetic validation versus live validation

`tests/applications/test_manual_ats.py` uses synthetic HTML and a synthetic PDF.
It checks exact-job scope, grouped Yes/No state, hidden radio/checkbox labels,
ID-less inputs, resume/autofill separation, changed-field rejection, bounded
foreground behavior and submission guards. All browser requests are fulfilled
from fixture HTML. Async portaled autocomplete tests reject missing, ambiguous,
uncommitted and unrelated-listbox choices. This covers the observed pattern,
not every custom combobox implementation.

```sh
.venv/bin/python -m pytest -q tests/applications/test_manual_ats.py
.venv/bin/python -m pytest -q
```

These fixture checks do not exercise the Browser Use daemon, candidate session,
tenant authentication or real upload service. Live evidence in the table above
comes from actual CLI calls and retained controls on the stated sites. No claim
of autonomous CAPTCHA handling or general Workday/Workable support follows from
either type of validation.

SuccessFactors classification recognizes the observed NS2 host
`career-hcm03.ns2cloud.com` and exact `/sfcareer/jobreqcareer` route with one
positive decimal `jobId` and one explicit `company` tenant. The canonical URL
retains both identity parameters; duplicate/case-variant parameters, unsafe URLs,
listing pages and redirects to another tenant or requisition do not yield an
application route. Tenant case is preserved because case equivalence is not
established. Other SuccessFactors datacenters remain outside this exact identity
registry until their URL rules are reviewed. Preparation and submission are
disabled, and no preparation skill is implied by this classification. The local
isolated-source evidence establishes the destination URL; synthetic tests cover
identity parsing and routing, not a live SuccessFactors form fill. SAP's own
[NS2 SuccessFactors documentation](https://userapps.support.sap.com/sap/support/knowledge/en/3763492)
identifies the hosting family, and its
[job-posting URL documentation](https://userapps.support.sap.com/sap/support/knowledge/en/2852775)
describes the tenant/requisition URL shape.

UKG currently has identity-only support for the observed
`wbdus.rec.pro.ukg.net` host. Exact tenant, JobBoard UUID, and opportunity UUID
are part of the durable identity. The observed `OpportunityDetail` and
`OpportunityApply` routes share that identity; canonical links point to the
detail route. Only one exact `opportunityId` query parameter is accepted;
malformed, duplicate, case-variant and additional parameters are rejected.
Tenant case is preserved, while UUIDs normalize to lowercase. Other UKG hosts,
login pages and board listings are not individual application identities.

The evaluated UKG form exposes resume upload, contact fields, optional
experience/education/skills sections, employer screening questions and voluntary
disclosures. Public HTTP provides an exact opportunity object and complete JD
inside the detail page, even when its visible HTML is a JavaScript shell.
Interactive preparation uses the existing guarded Browser Use CLI tab.
Identity support allows a private manual packet to appear in the portal; it
neither validates a reusable UKG preparation adapter nor enables final
submission. Both registry capabilities remain disabled. Synthetic tests verify
identity parsing, draft visibility and rejection by the submission capability
gate; they are not live UKG filling or submission validation.

The separately evaluated interactive flow is documented in
[UKG preparation observations](ukg-interactive-preparation.md), including import
corrections, saved-record audits and the remaining terminal-action limitations.

An observed Ashby residence control returned no results when reopened with its
already-selected full display label. Preparation may query the verified state
and recommit only the exact original state/country option from the control's
owned native results. It must verify the actual selection, not merely restore
typed display text. A bounded native-catalog receipt is tied to the same input,
job, question and retained value; edits or changed question context invalidate
it. Later read-only inspection can use that evidence without clearing a saved
selection. Synthetic fixtures cover successful native recommit, absent choices,
foreign listboxes, changed descriptions and edit invalidation. A separate live
CLI retry verified retained selection and the completed question inventory.

The standard Ashby contact Location field also needs a native catalog selection;
a stored city string alone cannot fill it. Its adapter now derives a query and
one exact city/state/country option from verified application-location preferences,
independently of the mailing city. This is restricted to the observed system field
with no help or its exact “City, State, and Country” hint. Synthetic Chromium fixtures cover restored blank
queries, already-filled values, wrong-country and ambiguous choices, native
commit, final retained-answer audit, and changed-context rejection. Live validation
of this contact-field extension remains pending; the residence retry above does
not establish it.

The observed current-or-in-progress education group has its own native school
catalog. A scoped projection uses one verified current record for institution,
degree and major, and the candidate's explicitly sourced exact expected graduation
day. It requires the observed instruction accepting study in progress and does
not infer a completed qualification. Synthetic fixtures cover owned catalog
commit, absent/ambiguous schools, changed instructions, original record validation
and date consistency. Its live validation also remains pending.

The Browser Use transport currently counts waiting for the shared browser lane
inside its operation timeout. An explicit interactive batch can allow more
bounded queue time while retaining the same lock, exact-tab scope and process
cleanup. A timeout before inspection does not establish an authentication or
candidate-answer problem. Do not respond by opening duplicate tabs, bypassing
the browser lock or changing final-submission deadlines.

### Read-only source tabs and application capacity

A local classification run exposed six retained LinkedIn source listings using
all worker-owned tab slots. Those pages had login or unsupported outcomes, so
the older cleanup rule requiring a verified external job description never ran.
The result was browser-capacity backpressure before application preparation.

Owned source creation is now limited to two tabs (and below the total cap),
reserving capacity for application drafts. Exact existing-source reuse remains
available. A read-only classification records a private no-click witness in the
ownership ledger. Terminal classification can close that exact source only
with a fresh SHA-bound private proof and matching target, URL and job identity.
A fresh DOM inspection preserves changed inputs, passwords, uploads, dialogs,
application forms and verification challenges. User tabs and unclaimed popups
remain untouched. Native Apply routing invalidates earlier read-only evidence.
Capacity deferral remains separate from a failed application attempt.

Synthetic Chromium fixtures cover terminal/login/unsupported outcomes and
preservation guards. This establishes no live candidate-tab closure or live
submission evidence; existing stale source tabs require fresh classification.
