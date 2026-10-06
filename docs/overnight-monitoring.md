# Finite overnight technical monitoring

The local monitor checks application states and newly appended log tails every
five minutes. It never submits an application, invokes the application pipeline,
changes a queue, or sends email. Preparation supervision can continue while the
portal is in Review mode. The separate submission worker still requires its
actual candidate approval or an explicitly enabled, valid delegated review window;
supervision grants neither. Post-confirmation sheet tracking also stays separate.

Enable `JHB_OVERNIGHT_MONITOR_ENABLED=1` only for an explicitly authorized, finite
window. `private/preparation-monitor-window.json` selects preparation supervision
independently of the submission environment gates or a revoked Full autonomy
authorization. The private record must contain:

- `scope`: `preparation monitoring and technical repair only`
- `role: user`, `status: verified`, `enabled: true`
- `submission_authority: false`, `repair_authority: true`
- A nonempty `source` and the actual user `content` authorizing monitoring and
  technical fixes; generated consent is not acceptable.
- Timezone-aware `authorized_at` and `expires_at`, with a positive duration of
  at most 24 hours. The current time must be within that window.

The loader never creates or extends consent. Expiry, revocation, CI or disabling
the monitor gate prevents new repairs. A pipeline pause also prevents repair and
validation subprocesses, including those already running. Resume does not extend
the finite window. No API key or subscription authentication belongs in CI.

When no preparation window exists, a valid `private/progress-report-window.json`
permits status observation only. It cannot start Codex or validation commands, and
the monitor does not send progress email. The separate hourly-report process
owns email delivery and its own gate. A malformed, revoked, expired or symlinked
dedicated window fails closed; it does not fall back to another grant. Only when
neither separate window is configured does the legacy monitor retain its original
finite submission-window dependency for compatibility. That path still requires
the gates in [overnight-submissions.md](overnight-submissions.md).

The monitor uses the installed `codex exec` CLI and the existing local Codex
login. It removes `OPENAI_API_KEY` and `CODEX_API_KEY` from child environments;
it does not provision credentials or buy inference. Codex receives the repair
prompt through standard input, with JSON events redirected to ignored private
files. Its command uses `--ephemeral --sandbox workspace-write`, approval policy
`never`, and workspace network access for required tools. Network access does
not authorize an application submission or changes to a candidate's account.

```sh
JHB_OVERNIGHT_MONITOR_ENABLED=1 \
  .venv/bin/python -m jhb.applications.monitor --watch
```

`--watch` is a finite process suitable for a reviewed launchd job with
`RunAtLoad=true` and `KeepAlive=false`; it exits at authorization expiry.
Alternatively, `--once` runs one check and can be scheduled with a 300-second
interval. Interval scheduling must also be removed at the end of the window;
after expiry those invocations produce an ended health report and never start
Codex. Installation and activation are separate from fixture validation.

The monitor binds its state to the exact window bytes. A different authorization
returns `authorization_changed`; it never clears repair history or quarantine to
adopt a new window. To begin a newly authorized window, stop the service, acquire
the monitor and worker locks, review any pending repair, and archive the prior
state privately before restarting. Never archive an unresolved quarantine to
make the worker proceed.

## When a repair runs

Only classified, retryable technical failures with no unanswered required field,
verification challenge or submission marker qualify. Existing failures before
the authorization window do not qualify. Log scanning seeds its initial cursor
at the current end of each log and reads bounded new tails; it extracts known
exception classes without copying raw messages into prompts.

Preparation evidence must also belong to a current, unleased `retry` row below
the normal three-attempt cap, due now or within the normal 600-second maximum
backoff. Repairs may run before that retry is due so a faster worker does not
consume its final attempt first; they never claim early or change queue timing.
Exhausted failures, nonclaimable rows and retries beyond that bounded horizon
remain visible in `deferred_application_issues`, with
their reasons and original evidence; they do not start a repair or renew the
retry budget. A shared error fingerprint does not mix those historical job
identities into an actionable job's repair prompt. The supervisor rechecks
current application and no-click attempt evidence after acquiring both worker
locks, before creating a repair quarantine. Log-only diagnostics retain their
existing separate handling.

The fingerprint consists of component, technical error class and operation.
The same issue affecting many jobs therefore requests one repair, with at most
20 evidence references. Deferred observations remain in private state while the
pipeline or repository is busy. Generic `RuntimeError`/`ValueError`, login and
CAPTCHA handoffs, and new candidate questions do not trigger repairs.

The first repair requires a clean committed `feat/phase2-greenhouse-agent`
checkout. A later repair may use only the exact diff previously validated by
this monitor; other concurrent changes defer it. The monitor never commits,
pushes, merges or changes `main` or `v0.1.0`. There are at most eight distinct
repair attempts per authorization. Each Codex process has a maximum 15-minute
runtime, further reduced by remaining authorization time. Compile, full pytest
and diff checks have limits of 120, 1200 and 30 seconds respectively; the complete
validation budget is reserved before starting a repair. The child runs focused
synthetic regressions; the supervisor owns the single full-suite pass.

After a successful diagnostic child that changes no repository content, the
monitor can reuse its exact `validated_repository` snapshot. Reuse requires the
same active authorization and unchanged protected candidate/application data
throughout the locked operation. The complete repository fingerprint, including
untracked public files, is checked again before quarantine is cleared. Missing
validation history or any repository change still requires the full checks;
failed children and authority/protected-state changes cannot gain approval by
reuse. The private validation result records `reused_validated_repository` so it
does not claim that tests ran again, or that the diagnosed application recovered.
This snapshot binds repository content, not installed dependencies, ignored runtime
helpers, browser state or the operating system. The repair child is instructed to
make repository-only changes and must not install/upgrade dependencies or modify
ignored helpers/environment configuration. Required environment changes are a
separate handoff. This operating constraint is not proof of arbitrary environment
immutability or OS containment; it does not expand the repair's authority.

Repair tools can start nested commands in separate sessions. The supervisor
tracks descendants by PID and creation time while the command runs. A unique
inherited command token identifies quickly reparented children during cleanup;
their environments are not logged. Cleanup freezes verified owned processes,
rechecks their descendants, stops children before the leader, and reaps the
leader. It signals individual identities rather than a possibly shared or reused
process-group ID. Unrelated processes remain untouched. PID reuse, unreadable
ownership or surviving children leaves the repair quarantined, including after
normal child exit, timeout, pause or revocation. This uses `psutil`, declared in
the applications dependencies. Repair commands must preserve the inherited
ownership token in subprocess environments. This is an operating contract, not
OS process containment: an intentionally token-scrubbing child that reparents
between ancestry samples cannot be attributed safely. Uncertain enumeration
still attempts to stop every positively identified member before reporting
quarantine; unrelated or reused identities are never signaled. Cleanup scans
check their deadline between process reads and retain uncertainty on expiry.

## Serialization and quarantine

`private/overnight-monitor/monitor.lock` serializes monitor checks.
`private/overnight-repair.lock`, `private/application-worker.lock` and
`private/approved-worker.lock` serialize repair, preparation and approved
submission work. Before Codex starts, the monitor writes
`private/overnight-monitor/repair-pending.json`. The pipeline checks this file
under its manager lock and skips a cycle while it exists, including a symlink.

A changed repair, or a checkout without an exact prior validated snapshot, must
pass, in order:

1. `.venv/bin/python -m compileall -q jhb tests`
2. `.venv/bin/python -m pytest -q`
3. `git diff --check`

The feature branch and commit must remain unchanged, authorization must still
be valid, and protected candidate facts, manual-answer packets, actual submission
authority, candidate approvals, credentials and application/submission state must
match their pre-repair digests. A supervision window cannot conceal a change to
the separate submission authorization. Only then does
the monitor clear quarantine. A failed, interrupted, expired or untrusted
repair leaves quarantine in place and does not repeatedly launch Codex.

Recovery requires reviewing the private repair transcript and public diff,
resolving the issue, rerunning these checks, and confirming submission evidence
and guards remain intact. Do not clear quarantine merely to make cron progress.
Never replay a submission with uncertain or in-progress evidence. A new
candidate answer remains a question handoff rather than an inferred value.

## Private evidence and reporting

The ignored `private/overnight-monitor/` directory contains `health.json`,
`state.json`, `morning-report.json`, repair prompts, Codex JSONL events, stderr
and validation logs. Files are mode 600. Health reports include aggregate queue
states, confirmed and uncertain submission counts, pending question counts and
sanitized technical fingerprints. They do not include candidate field values.
Health also exposes `monitoring_kind`, `repair_authority`, `submission_authority`
(always false) and `preparation_paused`, distinguishing observation-only reporting
from preparation repair. Review-mode preparation failures are scanned using the
supervision window's start time. Submission-attempt repair evidence remains
exclusive to its original matching legacy authorization.
The repair prompt treats page, packet and log contents as untrusted evidence.

Synthetic tests exercise authorization gates, duplicate suppression, locks,
quarantine, private permissions, process cleanup, repository fingerprints and
protected-data checks, Review-mode observation versus repair consent, and
fractional-second hourly-report startup. These tests do not establish that an overnight Codex
repair or a live submission has succeeded. Report those outcomes separately.
