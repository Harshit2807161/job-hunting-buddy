# Candidate approval before submission

The current workflow prepares applications autonomously and stops for a portal
review. Only the candidate's explicit **Approve and submit** click creates a
per-application approval. Saving an answer only resumes filling.

The local portal shows the live application link, chosen resume, retained final
review screenshot, and an inventory of every discovered question. Optional
questions remain visible. Each blank optional answer needs its own unchecked
**Leave blank** acknowledgment; required missing answers cannot be approved.
Older packets without a complete field inventory need a fresh preparation pass.
Prompts asking for the candidate's own non-AI wording require candidate input or,
when optional, an explicit decision to leave them blank.

`approvals.py` stores a durable approval and private two-hour authorization. The
revision binds the exact packet, relevant candidate facts, PDF hashes and review
screenshot. The API rejects stale views and duplicate approvals. The worker
rechecks the revision and live fields, invokes a separate read-only reviewer,
and performs two fresh retained-value audits. New questions, changed answers,
changed documents, revocation or expiry stop the terminal action.

`JHB_REQUIRE_PORTAL_APPROVAL=1` rejects legacy broad overnight authority even if
its old environment flag is accidentally enabled. The separate
`JHB_PORTAL_SUBMISSIONS_ENABLED=1` enables draining candidate-approved records;
it grants no authority on its own. Private pause/quarantine files take precedence.
Agents must never call the real approval endpoint on the candidate's behalf.

Before a terminal click the browser persists a write-ahead marker. Interrupted
or uncertain attempts cannot replay. Only a positive exact-job receipt records
a submission and starts the deduplicated spreadsheet synchronization step.
Failed spreadsheet delivery retries without submitting another application.

The dashboard runs on loopback and serves an explicit private artifact allowlist.
Host/client/origin checks and a same-origin CSRF token protect state-changing API
routes. It never serves the answer booklet, credentials or arbitrary local files.
Tests use synthetic candidates and mock approvals; real approval must come from
the candidate's portal interaction.
