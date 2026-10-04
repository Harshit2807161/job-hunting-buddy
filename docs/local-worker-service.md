# Local worker service

The preparation worker and approved-application worker are separate local loops.
Preparation uses the existing pipeline manager lock and bounded queue claims.
The approved worker polls every five seconds, so an explicit portal approval
does not wait for a long preparation batch. It calls only `approvals.drain`;
it cannot approve a draft or consume a blanket overnight authorization.
Atomic approval claims prevent duplicate dispatch when the ordinary pipeline
and this service overlap. Browser operations still use the registered Browser
Use CLI, the default daemon, an existing loopback CDP endpoint, and the shared
browser lane. Draft tabs remain intact.

Both modes require `JHB_REQUIRE_PORTAL_APPROVAL=1`. Preparation also requires
`JHB_APPLICATIONS_ENABLED=1`; approved dispatch requires
`JHB_PORTAL_SUBMISSIONS_ENABLED=1`. A pause file or repair quarantine prevents
either mode from starting browser work. Service watches never remove these
files. CI exits before loading local credentials, opening the ledger, or
starting a browser. Remote CDP endpoints are rejected.

```sh
# One bounded preparation batch; suitable for the existing local scheduler.
.venv/bin/python -m jhb.applications.service --prepare-once

# One bounded drain of explicitly approved portal drafts.
.venv/bin/python -m jhb.applications.service --approved-once

# Separate terminal/LaunchAgent processes for the current finite test window.
.venv/bin/python -m jhb.applications.service --prepare-watch
.venv/bin/python -m jhb.applications.service --approved-watch
```

The finite watches require the current verified
`private/progress-report-window.json`. This bounds their uptime and grants no
submission privilege. Preparation polls every 30 seconds; approved dispatch
polls every five seconds. Both stop on expiry, revocation, or a changed window,
and do not renew consent. An already running bounded operation completes its
outcome handling before the loop exits, preserving uncertain attempts and
receipts. New work requires a separate service start under the current window.
The existing Phase 1 scheduler remains independent and continues its normal
15-minute cadence. For ongoing prompt portal dispatch, use a separate persistent
LaunchAgent running `--approved-once` with `StartInterval` set to 15 seconds and
`RunAtLoad` enabled. This one-shot mode does not depend on the report window;
each job still requires its own explicit portal approval, which expires after
two hours and binds the exact reviewed answers and documents. It never renews
permission, approves drafts, or consumes a blanket overnight authorization.
Avoid a `KeepAlive` restart loop. Launchd does not start another copy of the
same job while it is running, and the service lock also excludes other copies.
Keep the supervised preparation watch finite during the five-hour test.

Preparation defaults to eight source checks, three applications, two concurrent
planners, and twenty active drafts. Existing bounded environment settings override
these defaults: `JHB_SOURCE_BATCH_SIZE`, `JHB_APPLICATION_BATCH_SIZE`,
`JHB_PIPELINE_CONCURRENCY`, and `JHB_MAX_ACTIVE_DRAFTS`. The approved batch defaults
to three and accepts `JHB_APPROVED_BATCH_SIZE` from one through ten. Browser
actions remain serialized even when planners run concurrently.

`private/pipeline-status.json` reports preparation activity.
`private/pipeline-submit-status.json` reports approval activity independently,
including active job hashes and aggregate approval states. Submission heartbeats
refresh every five seconds and become stale after twenty seconds. Routine
stdout contains only states, reason codes, and aggregate counts; private answers
and credentials are absent. Identical idle watch messages are coalesced.

The approved service holds `approved-worker.lock` to prevent duplicate service
instances from replacing one another's heartbeat. Repository repair holds both
that lock and `application-worker.lock` before editing or validating code.
A locked service is reported busy; it is never killed to reclaim capacity.

Before dispatch, the approved service briefly tries the preparation manager
lock without waiting. Only ownership of both worker locks proves that an old
`submitting` approval has no active owner. If preparation holds its lock,
orphan recovery is deferred while ordinary approved dispatch remains available.
Recovery reconciles positive receipts only when their job, immutable authority,
approved document hashes, two audits, and independent review all match.
Verified pre-click crashes return to review or expire; they are never approved
again automatically. Clicked or unverifiable attempts become uncertain and
cannot be replayed. Original authority and attempt files remain intact, and
confirmed submissions are never downgraded. Recovery uses no browser clicks.

LaunchAgent installation and activation are separate operator actions. Use the
repository's absolute `.venv/bin/python`, an explicit working directory, the
installed CLI tool locations in `PATH`, and ignored log destinations. Add new
labels for these services rather than replacing existing poll or dashboard
labels. This implementation does not install, start, or renew a LaunchAgent.

Validation uses injected workers, synthetic SQLite ledgers, controlled clocks,
and fixture file locks. It covers pauses, quarantine, CI, local-browser scope,
bounded settings, idle approvals, concurrent ownership, live heartbeat timing,
and expiry. These checks do not submit real applications or constitute live
browser validation.

Both local service lanes and the scheduled `jhb-apply pipeline` entry check the
existing loopback Chrome endpoint before opening SQLite or claiming candidate
jobs. Legacy HTTP endpoints use a bounded, credential-free GET of `/json/version`
to identify Chrome and a matching local browser websocket. Chrome's newer
default-profile debugging can return HTTP 404 while its direct websocket works.
For configured direct websockets, or that HTTP 404 case, a bounded local
`DevToolsActivePort` record must match the configured port and websocket path
where present. The official `browser-use doctor --json --require-existing-daemon`
must then confirm the existing default daemon has a healthy attached browser;
the port record must remain unchanged through that check. This strict doctor
does not start, repair, discover, or reconnect a daemon. Redirects, proxies, remote endpoints and
oversized responses are rejected. A closed or unreachable browser yields
`blocked / local_browser_disconnected`, refreshes private dashboard status, and
leaves job attempts untouched. This check never starts Chrome or sends email.
Phase 1 discovery and separate isolated source classification remain available;
the candidate reconnects Chrome explicitly before preparation resumes.
