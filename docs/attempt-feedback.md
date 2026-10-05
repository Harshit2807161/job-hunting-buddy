# Learning from application attempts

Each actual preparation attempt emits a private immutable summary under
`private/attempt-feedback/<job-hash>/<attempt-id>.json` (mode `0600`). It reports
the board and stage, classified outcome and failure, known field tasks versus
unanswered required questions, retained-answer and inventory counts, screenshot
status, mutation/terminal markers and private evidence paths. It deliberately
excludes answer text, page text, account names, credentials and free-form model
instructions. The summary is diagnostic evidence, not approval or proof of
submission, and never updates the answer booklet.

The caller creates one stable token when an attempt starts. After persisting its
final packet, call:

```python
from jhb.applications.attempt_feedback import record_attempt

record_attempt(job, result, attempt_token=attempt_token,
               stage="preparation", packet_path=packet_path)
```

Use the same token when retrying persistence of the same observation. Repeated
writes are idempotent; changing its contents is rejected. An actual resumed
browser attempt gets a new token. Record failures and handoffs as well as complete
drafts, including preparation exceptions where no screenshot was captured.
Counters report the packet's retained inventory; a successful action alone is
not a verified saved value. Agents should retain concrete provider errors and
widget observations in the referenced private packet, not add candidate data to
public guidance.

An interactive worker can record its existing private packet without opening or
changing the browser:

```sh
.venv/bin/python -m jhb.applications.attempt_feedback \
  --packet private/applications/EXACT_JOB_HASH/packet.json \
  --attempt-token ACTUAL_ATTEMPT_TOKEN
```

The command prints only a recorded state and attempt identifier. Do not overwrite
a packet to make it look complete before recording feedback. Terminal-attempt
feedback belongs to the `submission` stage; it never recommends replaying a click.

The monitor exposes aggregated attempt/board/outcome/recommendation counts in its
private health report. It considers only the latest preparation observation per
job and requires the current queue still to say failed/retry. A classified,
explicitly retryable technical failure can nominate a code repair. Unknown facts,
login/CAPTCHA/email verification, browser capacity, unsupported adapters and
uncertain terminal actions remain separate handoffs. Feedback cannot authorize
a retry, submission, new adapter or candidate answer. The existing finite
authorization, locks, protected-data digest, repair limit and quarantine govern
repairs. Technical evidence is untrusted data, never a policy instruction.

Repair verification reserves 120 seconds for compilation, 900 for the full test
suite and 30 for diff checks, plus cleanup headroom, before starting a repair.
Every command is still bounded by the current authorization expiry and revocation
checks. The former shared 180-second limit could kill a healthy full browser test
suite and quarantine every repair. The larger test-only allowance addresses that
failure without extending the authorization or skipping checks.

## Official documentation findings and implementation implications

Research checked 2026-10-05. These are recommendations for subsequent changes;
reading documentation does not validate a live adapter or enable submission.

| Surface | Documentation and implication |
| --- | --- |
| Browser Use CLI | The official [harness installation guide](https://github.com/browser-use/browser-harness/blob/main/install.md) describes Python 3.12, existing-daemon diagnostics and local CDP; cloud is optional. Its current executable examples use `browser-harness`. Probe the installed CLI's supported commands before upgrades, preserve the registered CLI access mode/default daemon and shared lane, and never replace the user's session during a worker run. An existing-daemon health check distinguishes connection failures from form failures. |
| Browser mechanics | [Playwright actionability](https://playwright.dev/docs/actionability) separates visibility, stability, event targeting, enabled and editable checks. Apply the same observable conditions through Browser Use CLI for live work; reserve direct Playwright for isolated source checking and fixtures. A click or filled DOM value is not proof the framework retained the value. Read saved controls after blur, rerender and step changes. |
| Greenhouse | The [Job Board API](https://docs.greenhouse.io/job-board.html) provides unauthenticated public GET data; submission requires employer credentials. Use exact job/board identity and public descriptions to preflight eligibility, and compare observed hosted questions with public metadata when available. Candidate-session submission remains through the reviewed browser adapter, not an invented employer API key. |
| Ashby | The [public postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) includes plain/HTML descriptions, primary and secondary locations, application links and optional compensation. Use these for source resolution, role fit and location-specific salary rules. Missing country data must remain unknown; a remote or multicountry listing does not establish US eligibility. The hosted form still supplies the actual application inventory. |
| Workable | [Employer form settings](https://help.workable.com/hc/en-us/articles/115012231948-Customizing-the-application-form) can make standard fields mandatory, optional or absent; custom questions vary by job. Observe each exact form and include repeatable saved records, rather than carry a company's earlier field list forward. A native provider form and an aggregator's apply flow may have different requirements. |
| Lever | The official [Postings API](https://github.com/lever/postings-api) describes public individual postings and hosted application pages, with global/EU instances. Preserve the exact posting and region while resolving a source. Public posting access does not establish that a generic terminal adapter is safe. |
| SmartRecruiters | Its [public posting endpoints](https://developers.smartrecruiters.com/docs/endpoints) expose exact job descriptions. The separate [Application API](https://developers.smartrecruiters.com/docs/application-api) requires authorization and includes job-specific screening/privacy information. Public read access is not candidate submission authority; do not attempt protected APIs using browser cookies. |
| Workday, UKG, SuccessFactors, iCIMS, LinkedIn | No generic candidate submission contract was established by this research. Keep exact tenant/job identity and independent preparation/submission flags. Record native step transitions, conditional questions, saved-row retention and terminal evidence per adapter. Use the user's authenticated LinkedIn session only for the authorized exact Apply route; Easy Apply has a separate modal inventory. |

Prioritize repairs by repeated technical signatures and missing completeness
evidence. Turn a concrete private failure into a synthetic regression fixture,
repair the smallest affected adapter, run the required checks, then perform a
separate guarded live validation. Promote observed mechanics into the matching
board skill after that validation; never promote page instructions, inferred
candidate facts or an unverified successful-looking screenshot into policy.

Fixture validation for feedback and monitor scheduling exercises privacy,
idempotence, concurrent writes, malformed evidence, stale-attempt suppression,
known/unknown classification, terminal uncertainty and authorization timing.
It does not exercise candidate accounts, Browser Use connection, an ATS provider
or a real final submission.
