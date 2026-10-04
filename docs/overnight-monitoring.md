# Finite overnight technical monitoring

The local monitor checks application states and newly appended log tails every
five minutes. It never submits an application, invokes the application pipeline,
changes a queue, or sends email. The separately authorized overnight pipeline
owns submission and post-confirmation sheet tracking.

Enable the monitor only for an explicitly authorized, finite window. It requires
both `JHB_OVERNIGHT_MONITOR_ENABLED=1` and
`JHB_OVERNIGHT_SUBMISSIONS_ENABLED=1`, plus the verified private authorization
described in [overnight-submissions.md](overnight-submissions.md). It reuses that
authorization's exact expiry and stops starting actions when it expires or is
revoked. `CI=true` disables repair. No API key or subscription authentication
belongs in CI.

The monitor uses the installed `codex exec` CLI and the existing local Codex
login. It removes `OPENAI_API_KEY` and `CODEX_API_KEY` from child environments;
it does not provision credentials or buy inference. Codex receives the repair
prompt through standard input, with JSON events redirected to ignored private
files. Its command uses `--ephemeral --sandbox workspace-write`, approval policy
`never`, and workspace network access for required tools. Network access does
not authorize an application submission or changes to a candidate's account.

```sh
JHB_OVERNIGHT_MONITOR_ENABLED=1 JHB_OVERNIGHT_SUBMISSIONS_ENABLED=1 \
  .venv/bin/python -m jhb.applications.monitor --watch
```

`--watch` is a finite process suitable for a reviewed launchd job with
`RunAtLoad=true` and `KeepAlive=false`; it exits at authorization expiry.
Alternatively, `--once` runs one check and can be scheduled with a 300-second
interval. Interval scheduling must also be removed at the end of the window;
after expiry those invocations produce an ended health report and never start
Codex. Installation and activation are separate from fixture validation.

## When a repair runs

Only classified, retryable technical failures with no unanswered required field,
verification challenge or submission marker qualify. Existing failures before
the authorization window do not qualify. Log scanning seeds its initial cursor
at the current end of each log and reads bounded new tails; it extracts known
exception classes without copying raw messages into prompts.

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
runtime, further reduced by remaining authorization time. Each subsequent
validation command has a maximum three-minute runtime. Owned process groups
are terminated and reaped on timeout or revocation.

## Serialization and quarantine

`private/overnight-monitor/monitor.lock` serializes monitor checks.
`private/overnight-repair.lock` and `private/application-worker.lock` serialize
repair and pipeline work. Before Codex starts, the monitor writes
`private/overnight-monitor/repair-pending.json`. The pipeline checks this file
under its manager lock and skips a cycle while it exists, including a symlink.

A successful repair must pass, in order:

1. `.venv/bin/python -m compileall -q jhb tests`
2. `.venv/bin/python -m pytest -q`
3. `git diff --check`

The feature branch and commit must remain unchanged, authorization must still
be valid, and protected candidate facts, authority, credentials files and
submission evidence/state must match their pre-repair digests. Only then does
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
The repair prompt treats page, packet and log contents as untrusted evidence.

Synthetic tests exercise authorization gates, duplicate suppression, locks,
quarantine, private permissions, process cleanup, repository fingerprints and
protected-data checks. These tests do not establish that an overnight Codex
repair or a live submission has succeeded. Report those outcomes separately.
