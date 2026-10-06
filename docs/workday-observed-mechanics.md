# Workday wizard observations

These notes come from live preparation through the registered Browser Use CLI,
using the existing local daemon, exact owned targets, the shared browser lane,
and an active final-submission guard. They describe observed mechanics, not
universal Workday behavior. Workday preparation and submission registry flags
remain separate; these observations do not enable either flag.

## Stable question ownership

Some application-question buttons expose only `Select One Required` through
`aria-label`. Their question is a visible `legend` inside the button's nearest
owned `fieldset`, within the matching `data-fkit-id` container. Capture that
legend rather than presenting several indistinguishable Select One handoffs.
Strip only the trailing required marker; preserve the substantive question.
Keep the actual control ID and verify ownership again before selecting an answer.
A label repair does not supply an unknown answer or broaden a standing fact:
“ever employed by government” is different from a five-year employment question.

Modern catalogs can use `data-uxi-widget-type="selectinput"` on ordinary text
inputs. `type="text"` alone does not prove that free text is an accepted answer.
A selected chip or the owned `promptAriaInstruction` must prove commitment.
Neighboring calling-code chips do not prove another field's selected country,
state, phone device type, or source. Scope each readback to its actual owner.

## Native dates

Education can expose only From and To year spinbuttons, with To explicitly
labeled `Actual or Expected`. A source year supplies that year, not an invented
month or day. Preserve an expected degree as expected in the uploaded resume
and review notes. Rendered DOM row IDs need not be consecutive.

A date segment input can be visually collapsed while an associated display
covers its click position. Use the observed segment's native focus/key mechanics,
or its observed display control; do not bypass hit testing on an arbitrary
covering element. Native digit keys and native ArrowUp/ArrowDown can operate
these spinbuttons. Confirm exact focus before sending keys.

A later year edit can alter a previously entered month. An immediate successful
month check therefore does not verify the complete date. Re-read every required
segment after all date edits and blur, after saving the step, and when revisiting
or inspecting the final native review. Compare the entire retained date with the
verified source. A mismatch is an agent repair task, not a new candidate fact.

## Source and skills catalogs

Discovery-source catalogs may be hierarchical: a visible parent category can
open another menu instead of committing a value. For example, an observed
CareSource Careers Page category led to a Corporate Website leaf. Read the
actual next menu and the final retained leaf. An unavailable source option does
not authorize claiming recruiter contact; mark a proposed closest observed
source in the per-application review rather than rewriting profile history.

Skills catalogs can offer recommendations and custom entries. Pressing Enter
while focus has moved from the search input to the list can toggle a recommended
option rather than execute the intended search. Select-all commands can likewise
act on the list. Verify the exact active search input before keyboard input;
prefer the owned clear-search control when a search query needs clearing.
Search text is not a selected skill, and selected recommendations are not
candidate evidence. Inspect the complete selected-chip set against the chosen
resume and verified coursework. Correct only unsupported chips inside that
owned skill field and re-read the remaining set. Do not claim unoffered skills
as committed; the approved resume retains the original full skill list.

## Step transitions and partial review

Save and Continue can return before the new page has finished rendering. A
progress marker can advance while the old inputs remain in the DOM during
Loading. Wait for the actual owned new-step controls, then observe again before
planning. A changed progress label alone is insufficient.

Footer Back can open a Discard Application dialog instead of returning to a
previous editable step. Preserve the draft: dismiss that specific dialog through
its actual continue-editing action. Do not choose Discard as a repair technique.
Completed progress labels are not navigation controls merely because their text
names an earlier step. Use an exposed native edit/step control; if unavailable,
retain the exact known-value mismatch as a technical task until an actual Review
Edit control becomes available.

Keep cumulative question inventory, verified fills, document evidence, and
technical tasks across all discovered steps. A partial snapshot of the current
questionnaire must not replace earlier identity, employment, education, or upload
evidence. Unknown required facts can park a partial draft while other jobs
continue. Later disclosure pages and the final native review remain unverified
until actually inspected. Never mark such a draft complete or approve it.

## Evidence limits

Live observations cover authenticated manual preparation, question ownership,
record entry, source hierarchy, source-grounded skill chips, native upload
success, and guarded step transitions. They do not establish general tenant
compatibility, a completed final Workday audit, or successful terminal submission.
Synthetic date/ownership regressions are separate evidence and must be reported
as fixtures. Candidate values, credentials, control IDs, screenshots, and runtime
proofs remain in ignored private storage.

Modern date widgets may expose a tiny hidden spinbutton behind a visible native
`dateSectionMonth/Year/Day-display`. Focusing and typing into that hidden input
can move focus to a sibling and corrupt the date. For this exact renderer, verify
that the input and unique visible display share the same `dateInputWrapper`,
click the display natively, and adjust the focused segment with bounded native
ArrowUp/ArrowDown events. Stop if focus moves, the value jumps unexpectedly, or
more than 100 increments would be needed. Finish composite blur and recheck all
segments, including previously edited ones. This was validated through the
registered CLI on six employment dates and four education years; the fixture
regressions separately cover a foreign display, moved focus and unstable values.
