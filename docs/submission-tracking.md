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
