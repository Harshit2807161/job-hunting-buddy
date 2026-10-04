# Job-board adapter evaluation

The discovery pipeline classifies multiple ATS providers, but scheduled
application preparation remains **Greenhouse only**. The six postings below
were explicitly authorized for manual evaluation and preparation. Their results
do not enable automatic Ashby, Workable or Workday dispatch. Phase 1's `main` and
`v0.1.0` baseline remain unchanged; this work stays on the Phase 2 branch.

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
| [Broadridge — Junior Full Stack Software Engineer (Hybrid)](https://broadridge.wd5.myworkdayjobs.com/careers/job/Newark-NJ/Full-Stack-Software-Engineer--Hybrid-_JR1086388) | Workday | No Google option appeared. An explicitly approved tenant account unlocked the seven-step wizard. Contact information, resume, education and employment repeaters were retained; two new factual checks paused Application Questions. Later disclosure/review steps remain unvalidated until those answers arrive. |

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

The current `ManualATSCLI` is an explicit Ashby scope around the existing CLI
transport; it is not wired into the scheduled dispatcher. Location autocomplete
uses an approved query and exact choice scoped to that field's visible listbox;
the selected value and closed popup are verified. Calendars and new repeater
shapes still need observed, bounded handling. Existing Greenhouse
behavior remains restricted to Greenhouse application identities.

## Answer and document preparation

Use [prepare-ashby](../skills/prepare-ashby/SKILL.md) for explicitly authorized
Ashby work and the existing Greenhouse planning skill for scheduled jobs. Match
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
