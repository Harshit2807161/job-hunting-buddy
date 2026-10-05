---
name: prepare-workday
description: Plan and prepare an exact Workday job application using the registered Browser Use CLI wizard adapter, verified candidate records, and observed employer questions.
---

Use the exact official job URL and fresh verified job description from the source
checker. Tenant, career site, and requisition ID form the job scope. A career
homepage, login route, or a different requisition is not that job. Inspect the
registry's preparation and submission capabilities separately.

Application interaction uses `WorkdayCLI` and the registered Browser Use CLI,
the existing daemon/CDP endpoint, the browser-lane lock, and one owned tab.
Read `skills/browser-use/SKILL.md` for browser access. Independent planning may
run in parallel; each browser operation must reattach its exact target and check
the job identity and submission guard. Preserve existing drafts and rows.

Choose the SDE or ML resume from verified role facts and the actual job
description. Keep document provenance and exact PDF bytes. Skills and coursework
must come from that chosen resume and verified records. For qualitative prompts,
use the shared candidate writing guidance and public company material; emphasize
what the company does and why the role matters. Use detailed personal experience
only when the prompt requests it. Never add screening facts to a prose answer.

Workday's wizard can contain My Information, My Experience, Application
Questions, Voluntary Disclosures, Self Identify, and Review. Use observed labels
and controls rather than assuming all tenants have the same steps. The planner
may choose Next, Continue, Review, or Save and Continue. Final submission is
handled only by a separately enabled, explicitly authorized audited dispatcher;
this preparation adapter keeps its guard enabled.
Retain an inventory of every discovered question across all wizard steps,
including optional questions. Each question must be recorded as answered,
blank, or explicitly declined. The review portal shows that exact inventory,
documents, and final screenshot. Only the user's per-application Approve action
authorizes its immutable reviewed packet; optional Leave Blank choices require
explicit acknowledgment. Changed fields or answers invalidate that approval.
If a prompt prohibits AI-written responses, use only the candidate's explicitly
provided, scoped wording. Do not draft or adapt prose for that question.

For owned question legends, year-only education dates, hierarchical sources,
selected skill chips, asynchronous steps, and safe draft preservation, read
[the observed wizard mechanics](../../docs/workday-observed-mechanics.md) when
those widgets appear. These live observations do not enable registry capabilities.

Observed mechanics retained in `workday_runtime.py`:

- Bind repeated education and employment controls by their observed record
  index and semantic column, using verified original records. DOM row IDs need
  not start at zero. Add only missing rows inside the correct section; keep
  surplus existing rows for review. Preserve employment bullet line breaks.
- Workday dropdown accessible names include their current selection. Bind the
  stable question label. Search catalogs may use `type="selectinput"`; typing a
  query is not a committed selection. Choose one exact approved visible option
  and verify the retained chip/selection within the owned popup.
- Dates can be separate month/year/day inputs. Type native digit keys and
  verify the whole retained date after all segment edits and blur, then after
  saving or inspecting the native review. A later year edit can change a month. A verified YYYY-MM date supplies month/year,
  and does not authorize an invented day. Preserve expected education dates
  and avoid declaring an expected degree completed.
- Resume inputs may have no ID. Verify the observed Resume/CV upload container
  and its exact input; a photo or unidentified upload is not a resume control.
  Check filename, approved PDF bytes, and the fresh upload receipt. Saved
  attachment cards need an independent retained-document audit before terminal
  automation is enabled.
- A legitimate Sign In `data-automation-id="click_filter"` overlay can be the
  canonical AX button above a native sibling. Use that observed accessible
  control in the approved login form. Unrelated overlays remain handoffs.
- Background native-input stalls can justify one activation of the exact owned
  guarded tab. Re-read identity, guard, and click geometry; ordinary background
  operation does not require activation. Use native wheel/input through CDP,
  rather than JavaScript scrolling or arbitrary overlay clicks.

Reuse the existing session. Google SSO is the default authentication preference.
An already recorded, verified exact-origin password exception may authorize
reuse of that site's existing OS-keyring credential. Do not register by fallback,
print passwords, accept unknown login consent, or infer an account choice.
Candidate Home after sign-in can be an authentication route, not a receipt;
return only to the exact approved job and inspect its current state.
A landing page with only a Sign In navigation control remains an authentication
handoff until the actual owned login form is observed. A review page with saved
cards and no editable fields remains an unsupported audit handoff; the absence
of fields does not prove that required answers and documents are complete.

Missing factual answers, unsupported widgets, ambiguous controls, authentication,
and verification challenges remain explicit handoffs. A saved review page or
expired session is not a confirmed submission. Before resuming an older attempted
application, verify its actual status; never replay an uncertain terminal action.
Keep candidate values, browser evidence, credentials, and receipts private. Report
synthetic fixture checks separately from live-site validation.
