# Application field inventory and evidence

Reviewed 2026-10-02. Live field discovery uses Browser Use's CLI on the actual
OneStream Greenhouse application and authenticated Simplify Profile and Personal
Info subpages. Synthetic fixtures are reported separately.

| Group | Booklet fields | Population rule |
|---|---|---|
| Identity/contact | First/last/full/preferred name, suffix, email, phone, birthday, city/state/country, street, address lines 2/3, ZIP | Explicit Simplify/resume values; user corrections override imports; unset fields remain unknown |
| Links | LinkedIn, GitHub, portfolio, Google Scholar | Extract PDF link annotations |
| Documents | Role-specific resume, cover-letter template and generated PDF, transcript | Preserve role variants; private paths only |
| Education | Institution, degree, major, dates, GPA, coursework | Resume evidence, expected graduation kept distinct |
| Experience | Employer, role, dates, location, achievements | Resume evidence; full role history retained |
| Skills | Languages, technologies, supported skills summary | Separate SDE/ML values; no invented skill |
| Screening | Authorization by country, sponsorship now/future, over 18, non-compete, employee relatives, recent US military/government employment, relocation, salary/currency/period, availability, prior employment, referral | Explicit profile/user decisions; standing rules retain their scope; salary follows the user-approved midpoint rule when a base range is known |
| Voluntary disclosure | Gender, race, ethnicity, Hispanic/Latino, veteran, disability, pronouns | User decision; never infer from name, school, or country |
| Declarations | Truthfulness, privacy consent, terms, background-check consent, signature/date | User decision; no blanket automatic assent |
| Role questions | Why company, technical evidence, years using a technology, writing sample | Store question-specific approved answers; unknown questions pause |

Observed on a real [Code for America application](https://job-boards.greenhouse.io/codeforamerica/jobs/8165204):
contact and document inputs, role-specific written questions, work authorization
acknowledgement, salary acknowledgement, and voluntary gender/ethnicity/veteran
self-identification. Its senior role is evidence for form structure only, not a
candidate job recommendation. Custom consent cannot be treated as a generic Yes.

The actual [OneStream application](https://job-boards.greenhouse.io/onestream/jobs/4425208009)
has required mailing address lines 1 **and 2**, city, state, postal code and
country; an independent phone-country dropdown; city autocomplete; resume
upload; optional school/degree; age, authorization, sponsorship, non-compete,
employee-relative and recent military/government screening; yearly salary
expectation; a truthfulness certification; and optional gender, Hispanic/Latino
and veteran disclosure. Selecting No for Hispanic/Latino reveals a race dropdown;
the worker must observe again before declaring the form complete. No cover-letter
input or login was required on this form.
Its sponsorship instructions include currently used work-authorization
arrangements; generic resume details cannot replace that exact question.

Education supports repeated school/degree rows. Both approved resume records were
filled live. The employer catalog lacked the second institution, so an observed
`Other` option was stored as an employer-scoped display mapping; the underlying
institution remains unchanged. No major/date/GPA controls were shown on this form.
Application location and mailing city use separate answers, as requested by the
candidate. Preferred first name and conditional explanations remain blank when
unknown or inapplicable.

Google SSO unlocked the supplied Simplify profile. Twenty observed answers,
forty profile skills, work history and education were imported privately; local
resume variants remain available separately. Profile contact values that differ
from resume contacts preserve the earlier source in answer history. The user
then corrected the mailing city and supplied standing screening/disclosure rules.
Private values and the user's profile identifier are excluded from this document.
