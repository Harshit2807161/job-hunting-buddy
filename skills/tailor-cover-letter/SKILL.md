---
name: tailor-cover-letter
description: Prepare role-specific LaTeX cover letters using the candidate's existing fte-hunt skill and preserved reference templates.
---

Read the local `cover_letter_skill` from the private booklet before editing a
letter. Choose its SDE or ML template. Copy it into the application's private
artifact directory; never edit a reference original.

Honor the original skill's allowed zones: salutation; role/company references;
opening and closing company-specific sentences; supported additions to the skills
paragraph. Preserve all other prose, metrics, availability, contact details,
LaTeX setup, and the original voice. Confirm unsupported claimed skills with the
candidate. If the job description or company facts cannot be obtained, pause.

Use `python -m jhb.applications.cli cover-letter` with an approved replacements
JSON. The compiler validates the allowed paragraph zones, escapes replacement
text, compiles with XeLaTeX, checks one page, and preserves a diff. Check the PDF
visually before approving it for application upload. A compiler error or overflow
is a handoff, never permission to alter the template's layout.
