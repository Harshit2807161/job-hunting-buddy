# Submission tracking

The final step after a confirmed, explicitly authorized application submission
is to record its receipt and update the existing application spreadsheet.
Preparation workers still stop at review. Spreadsheet logging never clicks
Submit, releases a browser guard, or infers success from an attempted click.

## Configuration

The Composio CLI supplies Google Sheets access through its authenticated local
session. Credentials are not copied into the repository or used in CI. Configure
an existing sheet in ignored `private/application-tracker.json`:

```json
{
  "enabled": true,
  "spreadsheet_id": "YOUR_SPREADSHEET_ID",
  "sheet_name": "YOUR_EXISTING_TAB",
  "headers": ["Company", "Role", "Location(s)", "Date applied", "Initial OA?", "Status last checked", "Verdict", "Link"],
  "timezone": "America/Los_Angeles",
  "date_style": "ordinal_day_short_month",
  "account": "YOUR_CONNECTED_ACCOUNT"
}
```

`JHB_TRACKER_CONFIG` may select another private configuration file. Without an
enabled configuration, confirmed receipts remain local and no Sheet calls occur.
The row uses verified employer, role, posting location and job URL. The application
date comes from the confirmation timestamp in the configured timezone, using the
existing ordinal-day and lowercase short-month style. Unknown OA, status-check
and verdict fields remain blank.

## Confirm and reconcile

After inspecting the actual success page, save its private receipt and job
metadata, then run:

```sh
.venv/bin/python -m jhb.applications.cli confirm-submission \
  --job-file private/confirmed-job.json \
  --receipt private/submission-receipt.json
.venv/bin/python -m jhb.applications.cli sync-tracker
```

Confirmation is recorded durably before automatic sheet synchronization. A receipt
must establish submission for the exact ATS job, with actual confirmation evidence.
A preparation result or a queue state alone is insufficient. Explicit candidate
reports are distinguished from live site confirmations and retain their original
statement as private evidence.

The private job file supplies `company`, `title`, `url`, and `location` or
`locations`. A live receipt supplies `state: "submitted"`, the exact job `url`,
a timezone-aware `confirmed_at`, `target_id`, the observed `confirmation` text,
and captured `body` containing that text. Its `source` identifies the live ATS
success page. Save actual observations; an attempted click is insufficient.
For a candidate-reported submission, use `source: "explicit_user_confirmation"`
and `user_evidence_path` pointing to a private JSON file with `role: "user"` and
the original affirmative statement in `content`.

The earliest local Greenhouse CLI receipts used `confirmation_url` and
`confirmation_text` without retaining a browser target ID. An explicit historical
reconciliation can wrap that original private artifact with
`source: "archived_browser_confirmation"` and `archived_evidence_path`. This
separate provenance requires the original successful state, exact Greenhouse
confirmation URL, captured positive text, authorization record and original
timestamp. The wrapper must preserve the text and timestamp exactly. Both files
remain hash-bound for later reconciliation; changing either blocks delivery and
replay. No current browser target, new application, or current live observation
is inferred from the archive.

Synchronization validates the existing headers, reads the current grid, and
checks canonical ATS job links before appending. Legacy rows without a usable ATS
link use the normalized employer, role and application-date combination. A
previous application to a different role at the same employer is a separate entry.
Writes use raw cell values and verify actual placement and readback. Existing rows
are preserved; no status or result is guessed. A local lock serializes agents.

A durable write intent protects retries after a timeout or interrupted append.
Uncertain writes are reconciled by reading the sheet before any further action;
absence of an immediate receipt does not authorize a duplicate append. Subsequent
pipeline cycles drain already-confirmed pending tracking work. This does not
change which boards are eligible for automatic application preparation.

## Delayed browser confirmations

An uncertain Submit outcome is never clicked again. For 15 minutes after the
persisted native click, the scheduled worker may inspect its original tab through
the official Browser Use CLI. Reads have a durable 30-second cooldown and a maximum
of two per cycle; each CLI call is bounded to 25 seconds. The observer neither opens
tabs nor navigates, focuses, fills, releases guards, or sends input. A closed or
changed target remains uncertain. The original attempt lock prevents observation
from racing a terminal operation.

The observer requires the original exact job, target, two identical retained-answer
audits, document hashes and independent review evidence. Greenhouse confirmation
must use the original HTTPS origin and exact job path followed by `/confirmation`;
this does not broaden source classification. Other reviewed boards must retain
the exact job identity. Explicit received/submitted text and the absence of active
form controls and terminal buttons are required. A generic thank-you, pending
verification, rejection text or another job never establishes success.

A positive page is read twice around one native screenshot. Its private receipt
retains the observed timestamp, screenshot hash, original audits and document
hashes, then enters the existing confirmation and spreadsheet reconciliation path.
It also closes the matching historic portal approval as submitted without renewing
that approval. Submission authority may expire while this read-only follow-up is
pending. Explicit Pause, technical quarantine or a discarded job stops new browser
reads; existing positive receipt files can still be recorded and synchronized.

Synthetic tests cover delayed success, expiry, pause, discard, missing click proof,
changed target/job, active forms, missing screenshots, bounded polling and no replay.
An actual Greenhouse confirmation on 2026-10-06 motivated this recovery after
arriving several minutes after its initial receipt timeout. That case was inspected
read-only and reconciled separately; the new scheduled observer's live validation
remains distinct from its passing fixtures.

## Import existing application history before preparation

`jhb.applications.historical.import_sheet` reads the same configured tab through
Composio CLI GET tools. It validates the complete grid and headers, saves a private
hash-bound snapshot, and imports dated rows into `sheet_application_history`.
The importer shares `application-tracker.lock` with the append worker, caches
successful reads for 15 minutes, and never appends a row or creates a submission
receipt. CI cannot use live Composio authentication. Missing, malformed or
unavailable reads retain existing history and report a sanitized pending state.

Immediately before the terminal submission call, the authorized worker bypasses
that cache and reads the configured sheet again. This catches manual applications
added while preparation or independent review was running. An exact match blocks
the duplicate; a cautious legacy match remains a hold. If the read is unavailable
or its lock is busy, submission waits for a bounded retry. This read-only check
does not alter existing rows or turn a manual entry into an employer receipt.

Call `historical.match(conn, job)` before source-browser access and again after
resolving the exact ATS URL, including the original `source_url`. An existing
canonical ATS/LinkedIn job identity returns an `exclude` decision. Tracking
parameters and posting/application route differences do not create another job.
Two distinct recognized ATS requisitions remain distinct even if their employer
and title are identical.

Older rows often have no usable job link. Matching employer and role, with no
conflicting explicit location, returns a cautious `hold` for identity
reconciliation. Punctuation, a terminal employer/remote title suffix, and observed
city abbreviations can be normalized for that hold only. No fuzzy employer-wide
exclusion or submission confirmation is inferred. An unresolved matching company
job URL also produces a hold rather than an invented ATS identity. Damaged
matching evidence blocks reapplication until reconciled.

Location comparison distinguishes country, region and city scope. Country aliases
such as US/United States and NYC/New York do not create different applications;
remote or unspecified geography cannot prove a mismatch. A multi-location job
conflicts only when every pair has an explicit incompatible component. Role
word order and Roman level I–V formatting are normalized while preserving every
level, cohort and specialty token. These rules create cautious holds, never
confirmed submissions.

An independent audit can retain a specific near-match with
`historical.record_hold(conn, job, entry_key=..., reason=...)`. It binds an exact
source/job identity to a verified imported row and preserves its evidence.
Employer aliases require a separate explicit reason for that one pairing.
Rediscovery, sheet refreshes and renamed titles cannot clear the hold. Only
`historical.release_hold(..., reason=...)` resolves it; other independent history
checks still apply. Different explicit ATS identities cannot be joined this way.

The existing ordinal-day/month application date is retained verbatim; the importer
does not infer a year, timezone, confirmation timestamp, browser receipt or new
submission count. Undated/planned/formula rows are not imported. Previously seen
evidence is retained across subsequent reads, including sheet row moves/deletions.
Correcting a historical exclusion requires explicit reconciliation, not erasing
the import to permit another application.

## Validation

Synthetic tests exercise receipt gating, ATS identity, legacy deduplication,
formatting, concurrent state changes and uncertain-write reconciliation without
accessing a candidate account. Live sheet validation is reported separately and
kept in ignored artifacts. Confirmed submissions remain terminal even if an
older preparation worker later tries to save a draft or error.

Live Google Sheets validation on 2026-10-03 added four missing, confirmed
applications and verified that the existing rows were preserved. The production
receipt command then recorded five confirmed applications, matching their
existing entries without another append. A subsequent synchronization made zero
changes, and a fresh complete-grid read verified all 184 populated rows unchanged.
This validates receipt recording and reconciliation against the configured sheet;
it does not establish submission for a draft or automate final submission.
