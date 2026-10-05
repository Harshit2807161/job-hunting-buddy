# Cover-letter preparation

An observed cover-letter file control starts the local `CoverLetterRunner` when
the selected role has no available approved letter. This includes the exact
combined label “Portfolio or Cover Letter”; unrelated portfolio uploads and
textareas do not start PDF generation. An explicit scoped answer or decision to
leave the document blank takes precedence.

The runner reads the complete candidate source skill and the matching original
reference, verifies the selected resume bytes and a fresh official description
for the exact job, and uses the existing signed-in Codex CLI. It needs no API key.
The model can propose only the opening and closing company-specific sentences;
Python supplies the exact company and role. Existing skills, experience, metrics,
availability, contact details and layout remain unchanged. No speculative skill
is added. The compiler retains a diff and verifies that the reference is unchanged.

XeLaTeX must produce one page. `pdftoppm` renders a private preview, and a separate
Codex image review checks readability, protected content and supported company
facts. A failed generation, overflow, unavailable compiler or unverified visual
review produces a document task, preserving other verified application fields.
It never asks the candidate to type a letter or supply a generated file path.
Transport failures use the existing bounded retry budget; validation failures
remain an explicit technical handoff. Employer requests for candidate-authored
wording or no AI remain candidate handoffs.

After verification the finished company-named PDF is delivered beside the
matching resume/reference. A different existing company PDF is backed up
privately before replacement. Each job also keeps an immutable PDF snapshot
under `private/applications/<job-hash>/cover-letter/<input-digest>/`; the uploaded
document and booklet record use this snapshot, so another role at the same
company cannot alter an existing draft's document. The provenance records the
source skill, reference, resume, description and PDF hashes, visual-review
checks, final delivery and per-job snapshot paths.

To explicitly generate a letter from an existing private packet without browser
interaction:

```sh
.venv/bin/python -m jhb.applications.cover_letter_runner \
  --job-file private/applications/<job-hash>/packet.json \
  --booklet private/answer-booklet.json --role sde
```

The command prints local document and preview paths. Inspect the preview before
a live demonstration. Application upload still uses the official Browser Use
CLI, and every application still requires its own portal approval before any
submission. A refreshed form or changed document invalidates an older approval.

Fixture coverage includes a real synthetic XeLaTeX/PNG build, independently
injected image-review decisions, exact source/hash mismatches, two-page overflow,
immutable snapshots, backup delivery and generation during observed form
preparation without submission. This is not live candidate validation. CI never
uses Codex subscription authentication; synthetic tests inject the engine.
