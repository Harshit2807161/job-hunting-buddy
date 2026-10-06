# Local workflow controls and Phase 1 openings

The dashboard's **Full autonomy** switch creates an eight-hour private user
authorization from an explicit same-origin portal action. It delegates final
review to the separate reviewer on currently enabled submission adapters;
recognizing a board does not enable its terminal adapter. Complete inventory,
unknown-fact handoffs, retained-value checks and receipt-before-tracking rules
remain required. No per-job portal approvals are synthesized.

The switch uses `GET` and CSRF-protected `POST /api/v1/workflow-policy`. Every
change includes the revision the user saw, rejecting concurrent or stale writes.
Updates lock and atomically replace the existing authorization artifact, keeping
its prior version privately for auditing. Turning the switch off revokes that
broad authority. Expiry returns to individual approval without requiring another
request. In-flight terminal workers must revalidate authority before clicking;
a click already sent cannot be undone. Review mode does not erase confirmed
receipts or retry uncertain outcomes.

A separately disabled runtime still blocks automatic submission. The API does
not change environment variables, clear pipeline pauses or enable unvalidated
adapters. The UI distinguishes requested authorization from an actually active
runtime, displays the expiry, and treats unverified mode status as unavailable.
Neither GET nor dashboard rendering creates authority.

**New openings from Phase 1** reads the jobs ledger through bounded
`GET /api/v1/openings` pages (25 by default, at most 100). A stable discovery-time
and job-hash cursor handles equal timestamps without offset duplicates. Source
classification and actual application state are separate columns. A resolved
ATS destination alone is not a filled or submitted application. Only an exact
job identity links to the application's review. Filtered openings retain their
recorded requirement evidence. Raw source payloads, private artifact paths and
candidate records are not exposed.

The newest 25 openings refresh every five seconds; older pages load explicitly.
The portal's confirmed-submission totals and sheet status continue to depend on
submission receipts and tracker delivery records, not on discovery counts.

Validation uses synthetic SQLite ledgers, CSRF/API tests and an isolated headless
Chromium dashboard fixture. These tests neither connect to a candidate browser
nor provide live job-board submission evidence.
