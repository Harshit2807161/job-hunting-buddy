# Browser tab lifecycle

All registered Browser Use CLI dispatchers pass through the tab-lifecycle
wrapper while holding the shared browser lane. The private
`browser-tab-ledger.json` records exact ownership and departures; it never
claims an existing tab merely because its URL matches a job.

An official `new_tab` return establishes ownership only when that exact target
was absent from both the earlier tab list and attached target. The helper can
reuse a user's blank tab, so blank reuse stays unowned. Existing exact job tabs
are reused. User tabs, mail, SSO windows, unfinished drafts, unknown controls and
uncertain submissions are preserved.

The default limit is six live worker-owned targets. `JHB_MAX_OWNED_TABS` accepts
integers from 1 through 12. New LinkedIn source creation reserves space for its
possible destination. A native Apply popup becomes owned only with an observed
matching expected job, absence before the action, and CDP `openerId` identifying
that exact LinkedIn source. A same-target navigation keeps the original proven
ownership and becomes an application draft. Unproven new destinations remain
open and halt further native LinkedIn Apply clicks until departed. They are
never silently claimed or closed.

Authenticated source checking first reads the exact observed Apply control's
public HTTPS href through the registered CLI. It creates no destination popup;
its link goes to the isolated official Playwright MCP for exact-job validation.
Existing source tabs can be reused, and fresh read-only source tabs need one
slot. Unclaimed destinations also consume this path's capacity, so a full
browser stays deferred. Button-only Apply controls retain the native popup guard.
A proven-created read-only source closes only after private, hash-bound evidence
confirms the same source job, an actual isolated MCP destination navigation,
and fresh verified official job-description metadata for that exact destination.
Existing user source tabs and unclaimed destinations stay open.

Capacity is `BrowserCapacityError`, condition `browser_capacity`, with
`mutation_started=False`; it is technical backpressure, not a candidate
question. The queue should defer the job without consuming its preparation
attempt budget. Reusing an existing exact job needs no new slot.

Receipt cleanup occurs before the next open/source resolution or through
`await client.cleanup_tabs()`. Closing an application requires all of:

- Proven exact-target ownership in the ledger.
- A durable `confirmed_submissions` record whose private live-success receipt
  passes the tracker validator and its stored hash.
- The receipt's exact target and success URL still present, positive
  confirmation still visible, and no visible application Submit control.
- A complete retained review packet and a fresh capture manifest validated
  against the PNG bytes, job identity and exact owned capture target.

The screenshot hook remembers only its private directory; cleanup reads the
published packet after capture finishes. An old PNG or failed current capture
cannot authorize closure. Cleanup calls official `close_tab(target_id)` with an
explicit ID and verifies that ID disappeared. An unconfirmed close is not
replayed. The previous attachment is restored if it remains open.

A proven-created transient LinkedIn source may close after an actual separate
external destination is observed. Its destination draft remains open. Same-tab
navigation preserves that draft. Departure tombstones remain in the ledger;
another tab at the same URL does not inherit ownership. Raw CLI scripts outside
the registered wrapper and legacy tabs remain unclaimed.

Tests exercise helper-return ownership, blank/existing-tab reuse, popup opener
proof, capacity, durable receipt validation, fresh-capture checks and exact
closure using synthetic tabs. They are not live-browser validation.
