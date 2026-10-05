# Expiring overnight submission policy

The current Full autonomy mode requires fresh, finite user delegation of final
review to an independent agent. It retains complete-question inventory, current
screenshot and candidate/document bindings, two live retained-value checks,
one-shot terminal attempts, exact positive receipts and verified spreadsheet
delivery. Each optional blank needs a grounded reviewer decision. Unknown facts,
unvalidated board adapters and uncertain submissions remain handoffs.

`approval_mode: "independent_reviewer"` and `require_complete_inventory: true`
distinguish this mode from historical broad authorization. The default portal
gate remains enabled; only the active delegated window and separate overnight
runtime gate permit this exception. Ending the window restores per-draft review.

**Historical narrow mode.** The default local policy requires a candidate Approve click
for each exact draft through the portal. `JHB_REQUIRE_PORTAL_APPROVAL=1` rejects
the broad authority described below, even if its environment flag is enabled.
See [portal-review.md](portal-review.md). Compatibility tests retain the older
finite policy, but it is not the active submission mode.

Normal Phase 2 preparation stops before Submit. An explicitly authorized overnight
window can opt in to submission of **new Phase 1 Greenhouse jobs only**. Existing
drafts and manually prepared jobs remain outside this window; a separate explicit
request to submit a named draft does not broaden scheduled authorization.

Two gates must be enabled after the implementation is reviewed:

- `JHB_OVERNIGHT_SUBMISSIONS_ENABLED=1` in the ignored local environment.
- `private/overnight-submission-authorization.json`, containing verified user
  evidence, `enabled: true`, `status: "verified"`, `role: "user"`, the exact scope
  `new Phase 1 Greenhouse jobs discovered during this authorization window`, and
  `board: "greenhouse"`. The actual user instruction must explicitly authorize
  continued overnight submission. `authorized_at` and `expires_at` require
  timezones; the window must be active and at most 24 hours long. The current
  user request is seven hours; its private expiry is authoritative.

The authorization also requires `require_browser_double_check`,
`pause_unknown_answers`, and `require_receipt_before_sheet` to be true. Removing
or disabling the file, clearing the environment flag, or reaching its expiry
stops new submission attempts. The manager and browser runtime both re-read the
file; changing it during an attempt invalidates the previous authorization hash.
There is no automatic renewal.

A job qualifies only when its durable Phase 1 `jobs.first_seen` falls inside the
window and its source check resolved to that exact Greenhouse identity. The
canonical application must be `waiting_review`, have no required missing answers
or unresolved required questions, and have a fresh verified official description
that passes the standing citizenship/clearance filter. Rediscovery through a new
wrapper does not make an old canonical draft eligible.

The normal bounded pipeline prepares the application first. The authorized
submission adapter then uses the registered local Browser Use CLI, preserves
tabs, attaches the exact owned job, checks approved document provenance, and
performs two fresh retained-answer audits. Selected SDE or ML resume variants
remain separate. Optional letters are audited when attached; the policy does not
invent a new letter or unknown factual answer. Newly revealed required questions
return to the private question ledger. Login, MFA, unresolved verification,
unsupported controls, and ambiguous job tabs remain handoffs.

Before invoking the submission adapter, the manager commits a per-job row in
`authorized_submission_attempts` and writes
`private/authorized-submissions/<job-hash>/attempt.json`. The browser records its
terminal-click marker before clicking. A safe pre-click handoff can resume only
after a changed successful preparation packet, or after classified technical
backoff, with affirmative persisted proof that no terminal click began. These
attempts stop after three and retain their previous private audits. An
interruption or uncertain outcome cannot trigger another automatic click.
An uncertain job enters `submission_uncertain`, remains protected from resumption,
and counts against draft capacity. It requires receipt reconciliation or an
explicit human review. No browser tab is deleted to reclaim capacity.

A positive live receipt is saved before success is returned. Only an exact-job
receipt carrying the authorization identity and two-check evidence can be
recorded with `tracking.record_confirmed`; that API persists confirmation before
synchronizing the configured spreadsheet. Pending sheet delivery can retry
without another application submission. Receipt reconciliation remains read-only
with respect to the browser and can complete after the window expires. Submitted
jobs are excluded from review-ready email.

Submission attempts are sequential and bounded by the pipeline application batch
(default three). Browser operations retain the existing global lane lock. A
technical monitoring repair quarantine defers the manager before source checks,
preparation, or submission; Phase 1 can continue discovering jobs independently.

## Validation

```sh
.venv/bin/python -m pytest -q tests/applications/test_overnight.py
```

These tests use synthetic candidates, private temporary authorization files,
injected submission clients, and synthetic positive receipts. They cover scope,
expiry and revocation, write-ahead attempts, missing answers and eligibility,
uncertain outcomes, deduplication, and receipt recovery. They make no live browser
calls or real submissions. Live checks and actual confirmed submissions must be
reported separately with private evidence.
