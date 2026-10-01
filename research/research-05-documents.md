# Research 05: Document Layer (Candidate Profile / Resume / Cover Letter)

## 1. Structured candidate profile schemas

### JSON Resume (jsonresume.org)
A community JSON Schema (Draft-07, `schema.json` in jsonresume/resume-schema) defining a canonical `resume.json`. No fields are strictly required, but consuming themes expect at least `basics`. Sections:

- **basics**: `name`, `label`, `image`, `email`, `phone`, `url`, `summary`, `location` (`address`, `postalCode`, `city`, `countryCode`, `region`), `profiles[]` (`network`, `username`, `url`)
- **work[]**: `name`, `location`, `description`, `position`, `url`, `startDate`, `endDate`, `summary`, `highlights[]`
- **volunteer[]**: `organization`, `position`, `url`, `startDate`, `endDate`, `summary`, `highlights[]`
- **education[]**: `institution`, `url`, `area`, `studyType`, `startDate`, `endDate`, `score`, `courses[]`
- **awards[]**: `title`, `date`, `awarder`, `summary`
- **certificates[]**: `name`, `date`, `url`, `issuer`
- **publications[]**: `name`, `publisher`, `releaseDate`, `url`, `summary`
- **skills[]**: `name`, `level`, `keywords[]`
- **languages[]**: `language`, `fluency`
- **interests[]**: `name`, `keywords[]`
- **references[]**: `name`, `reference`
- **projects[]**: `name`, `description`, `highlights[]`, `keywords[]`, `startDate`, `endDate`, `url`, `roles[]`, `entity`, `type`
- **meta**: canonical `url`, `version`, `lastModified`

Dates use a shared `iso8601` sub-type referenced across sections. Widely supported by resume-rendering themes/CLI (`resume-cli`) and validators (`@jsonresume/schema` npm package).

### AIHawk `plain_text_resume.yaml`
(from `feder-cr/lib_resume_builder_AIHawk`) — a flat, human-editable YAML meant to be gitignored locally, consumed by an LLM to generate/tailor resumes and to answer application-form questions. Top-level keys observed:

- `personal_information`: name, surname, date_of_birth, country, city, address, zip_code, phone_prefix, phone, email, github, linkedin
- `education_details[]`: education_level, institution, field_of_study, final_evaluation_grade, start_date, year_of_completion, exam (map of course→grade)
- `experience_details[]`: position, company, employment_period, location, industry, key_responsibilities[] (responsibility_N strings), skills_acquired[]
- `projects[]`: name, description, link
- `achievements[]`: name, description
- `certifications[]`: strings
- `languages[]`: language, proficiency
- `interests[]`: strings
- `availability`: notice_period
- `salary_expectations`: salary_range_usd
- `self_identification`: gender, pronouns, veteran, disability, ethnicity
- `legal_authorization`: eu_work_authorization, us_work_authorization, requires_us_visa, requires_us_sponsorship, requires_eu_visa, legally_allowed_to_work_in_eu, legally_allowed_to_work_in_us, requires_eu_sponsorship
- `work_preferences`: remote_work, in_person_work, open_to_relocation, willing_to_complete_assessments, willing_to_undergo_drug_tests, willing_to_undergo_background_checks

Notably, AIHawk bakes EEO/legal/salary/preference fields directly into the profile — exactly the form-autofill data JSON Resume omits — because its purpose is auto-filling ATS application forms, not just rendering a resume document.

### Recommendation
Use **JSON Resume as the base schema** for the portable, resume-renderable "career facts" (work, education, skills, projects — it's a standard, has tooling/themes/validators, and is a clean interchange format), but **extend it with a sibling `application_profile.json`** (AIHawk-style) for the form-autofill / EEO / logistics data that JSON Resume deliberately excludes (it's a resume-rendering schema, not an ATS-answer schema). Keep the two files separate: one is safe to hand to an LLM and even publish; the other contains sensitive/PII fields and needs the redaction boundary described in §6. Concretely, add a top-level `x_application` extension object (JSON Resume permits vendor extensions) or a separate file with:

- **Work authorization / visa**: current authorization status per country, `requires_sponsorship_now` (bool), `requires_sponsorship_future` (bool, e.g. OPT/STEM-OPT timeline), visa type held/expiry
- **EEO voluntary self-identification**: race/ethnicity, gender/gender identity, and the standardized **CC-305** disability self-ID language ("Yes, I have a disability / No / I don't want to answer") plus veteran status per **VEVRAA** categories — store as "decline to answer" by default, since these are legally voluntary and protected
- **Compensation**: `salary_expectation_min/max`, currency, basis (base/total comp)
- **Timing**: `earliest_start_date`, `notice_period_days`, `graduation_date` (for new-grad roles)
- **Logistics**: `relocation_willing` (bool + regions), `remote_preference`
- **Sourcing/history**: `how_did_you_hear` (default/per-company override), `prior_employment_at_company` (bool + dates, since many ATS ask this per employer), `criminal_history` (jurisdiction-dependent — many "ban the box" states prohibit asking pre-offer; store only if legally solicited), `references[]` (name, relationship, contact, consent-to-contact flag)

Sources: [jsonresume/resume-schema](https://github.com/jsonresume/resume-schema), [JSON Resume Documentation](https://docs.jsonresume.org/schema), [AIHawk plain_text_resume.yaml](https://github.com/feder-cr/lib_resume_builder_AIHawk/blob/main/plain_text_resume.yaml)

## 2. Cover letter minimal-edit templating

### (a) Proven new-grad SWE structure
Convergent guidance across cover-letter resources (Enhancv, Resume Genius, Resume-Now, Kickresume): a 3-4 paragraph, 300-400 word letter:
1. **Opening**: role + company name, one line of genuine enthusiasm/hook (a personal connection to the product, a specific team, or a referral).
2. **Body (proof)**: 1-2 concrete achievements/projects mapped to the job's stated requirements — quantified ("improved X by 30%") rather than generic ("worked on X"). For new grads this leans on capstone/academic projects, internships, and open-source work rather than years of experience.
3. **Company-specific paragraph**: 2-3 sentences that reference something specific and verifiable — an engineering blog post, a product launch, a stated mission/value, an open-source repo — showing the letter wasn't copy-pasted.
4. **Closing**: restate interest, call to action ("would welcome the chance to discuss..."), contact info.
Standard business-letter formatting (contact block, date, employer info, 11-12pt professional font, 1" margins).
Source: [Enhancv entry-level SWE cover letter guide](https://enhancv.com/cover-letter-examples/entry-level-software-engineer/), [Resume Genius SWE cover letter](https://resumegenius.com/cover-letter-examples/software-engineer-cover-letter-sample), [Resume-Now graduate SWE cover letter](https://www.resume-now.com/cover-letter/examples/computer-software/engineer/graduate), [Kickresume SWE cover letter samples](https://www.kickresume.com/en/help-center/software-engineering-cover-letter-samples/)

### (b) Template-with-slots vs full-LLM-rewrite
Only paragraphs 1, 3, and part of 2 actually vary per application (company name, role title, why-this-company sentence, which 1-2 projects/keywords get foregrounded). Paragraphs 2 (core proof) and 4 (closing) are close to invariant for a given candidate/track.

- **Full-LLM-rewrite** (regenerate the whole letter each time): more natural-sounding output and can adapt tone/emphasis fluidly, but every regeneration is a fresh opportunity for the model to invent a metric, misstate a job title, add an unverifiable claim ("I've long admired your commitment to X" where X isn't real), or silently drop a required disclosure. Hallucination risk compounds because nothing anchors the "unchanged" 80% of the letter to a known-good, human-approved baseline — you'd need to re-proofread the whole letter every time.
- **Template-with-slots** (fixed skeleton, LLM fills only `{{COMPANY}}`, `{{ROLE}}`, `{{WHY_THIS_COMPANY}}`, and picks from a pre-approved menu of 2-3 project blurbs): the invariant 80% is guaranteed correct because it's never regenerated — it was written and approved once by the human. The LLM's blast radius is limited to a few short, independently-checkable spans.
- **Recommendation**: template-with-slots is safer and should be the default; reserve full-rewrite (with mandatory human review before send) for cases where the role is meaningfully different from the candidate's usual track (e.g., applying up a level, pivoting domains) and the fixed skeleton doesn't fit.

### (c) Constraining the LLM to designated spans + validating byte-identity outside them
Mechanism to constrain generation:
1. Author the letter as a template file with explicit, unambiguous delimiters, e.g. `{{COMPANY}}`, `{{ROLE}}`, `{{WHY_THIS_COMPANY}}` (avoid ambiguous delimiters like plain `_____` that could collide with legitimate text).
2. Prompt the LLM with the literal template text and instruct it to return **only** a JSON object mapping each placeholder name to its fill value (use structured output / tool-calling / JSON schema mode so the model cannot emit free-form prose) — never let the model return the reassembled letter itself. This removes the model's ability to touch anything outside the slots by construction, not just by instruction.
3. Programmatically substitute the returned values into the template via simple string `.replace()` on the exact placeholder tokens.

Concrete diff-validation algorithm (defense in depth, in case step 2's structured-output constraint is bypassed or the model is asked to return the full letter for some reason):
1. Take the canonical template `T` (with placeholders) and the candidate output `O`.
2. Split `T` into an ordered list of alternating literal-segments and placeholder-tokens: `[lit_0, PH_1, lit_1, PH_2, lit_2, ..., lit_n]` by regex-splitting on `\{\{[A-Z_]+\}\}`.
3. Build a regex from `T` where every literal segment is `re.escape()`-d and every placeholder becomes a capturing group `(.*?)` (non-greedy) or a length/character-class-constrained group if you want to bound slot size.
4. Anchor the regex with `^` and `$` (or `\A`/`\Z`) and match it against `O` in `DOTALL` mode.
5. **If the regex fails to match**, reject the output outright — this means the model changed, reordered, or deleted literal text, which is exactly the failure to catch.
6. **If it matches**, extract the captured groups as the actual fill values and diff them against what the model claimed to fill (if using the JSON approach) — flag any mismatch.
7. As a second, format-independent check, reconstruct `O' = lit_0 + captured_1 + lit_1 + ... + lit_n` and assert `O' == O` (byte-for-byte), and separately assert that removing the captured groups from `O` yields exactly the concatenation of the literal segments (`O` minus fills == `T` minus placeholder tokens) — this catches whitespace/unicode tampering the regex might silently normalize.
8. Optionally run a plain `difflib.SequenceMatcher` or `git diff --word-diff` between `T`-with-placeholders-blanked and `O`-with-fills-blanked and require zero non-whitespace diff hunks outside the placeholder positions — a cheap, library-free second opinion.
This turns "did the LLM only touch the slots" into a deterministic, testable boolean rather than a vibe-based read-through — reject-and-retry on failure, never auto-send on failure.

### (d) Sourcing "why this company" factually without fabricating
- Extract the sentence's raw material **only from the job posting text itself** (and, if available, a small allowlist of first-party sources: the company's own careers page, engineering blog, or About page fetched at generation time) — never from the model's parametric memory of "what this company is known for," which is a common hallucination vector for less-famous companies or stale facts (leadership changes, pivots, sunset products).
- Use extractive-then-templated generation: pull 1-2 verbatim phrases/clauses from the JD (mission statement line, a named product, a named team, a stated tech stack, a stated value) and require the LLM's `{{WHY_THIS_COMPANY}}` output to contain at least one direct quote or paraphrase traceable to a specific substring of the JD — i.e., the generation prompt includes the JD text and an instruction "you may only reference facts explicitly present in the provided JD text below; do not use outside knowledge about this company."
- Add a lightweight grounding check: after generation, verify wordoverlap/embedding-similarity between the generated sentence and the source JD paragraph is above a threshold, or require the model to also output a `source_quote` field and validate that quote is a substring (or near-substring, allowing minor whitespace/casing normalization) of the JD — reject/retry if the quote can't be located in the source text (a simple RAG-style citation-verification pattern).
- Keep the claim generic/safe when the JD is thin (e.g., "your team's focus on [specific stated problem from JD]" rather than inventing a mission statement) rather than reaching for a fabricated specific.

## 3. Resume tailoring, safely

**What ATS keyword matching actually does vs the myth.** The popular myth is that an ATS is a hard gatekeeper that auto-rejects any resume missing exact keywords ("black box that discards 75% of resumes unseen"). In reality, most modern ATS platforms (Workday, Greenhouse, Lever, iCIMS) primarily do three things: (1) **parse** the resume into structured fields (name, contact, work history, education, skills) via a parsing engine, (2) **store/index** that structured data so recruiters can search/filter/sort candidates, and (3) optionally **rank or highlight** matches against a recruiter's keyword search — but the majority do not auto-reject purely on keyword absence unless a recruiter has explicitly configured a knockout screening question (e.g., "do you have X years of Y" hard filters, which are a separate mechanism from resume-text keyword matching). The real failure mode is not "the ATS auto-rejected me" but "the parser mis-extracted my data" (e.g., pulled the wrong job title into the wrong field, or failed to detect a skill because of bad formatting) which then makes the candidate look weaker or unsearchable to the human recruiter — the harm is indirect, via bad structured data, not a keyword-count veto.

**Does per-job tailoring help?** Yes, but mainly for two reasons unrelated to "beating an algorithm": (1) it improves parser field-mapping accuracy when the tailored resume's terminology matches the JD's terminology (e.g., using the JD's own term "Kubernetes" rather than a synonym the parser's taxonomy doesn't map), and (2) it improves the human recruiter's 6-second skim match and the keyword-search hit rate when a recruiter searches their ATS database later. It does not "trick" a ranking algorithm into moving you to the top in the way vendors marketing "ATS optimization" tools imply.

**Keyword-stuffing risk.** Stuffing (invisible white-text keywords, dumping a skills list of every technology mentioned in the JD regardless of actual experience) risks: (1) parser confusion — some parsers penalize or flag anomalous keyword density, (2) recruiter distrust once a human reads it and it reads as generic/dishonest, (3) failure at interview when asked to elaborate on a stuffed skill, and (4) in the worst case, being flagged by increasingly common AI-detection/consistency-checking layers some ATS vendors are adding. Net: keyword *inclusion in truthful, natural context* helps; *stuffing* is net-negative once a human is in the loop, which is the common outcome for any resume that clears initial parsing.

**N pre-built variants vs. per-job generation — recommendation.** Maintaining **3-5 pre-built resume variants** (e.g., backend / ML / full-stack / infra) and classifying the incoming JD to select the closest variant, then doing only *light* per-job tailoring on top (reordering bullet emphasis, swapping 1-2 bullets, adjusting the skills line to mirror JD terminology) is the better architecture for this system:
- It bounds hallucination risk — the base variant's bullets are human-written and pre-approved; only small deltas are LLM-touched (same template-with-slots logic as the cover letter).
- It's cheaper and faster (no full-document LLM generation per application).
- It avoids the failure mode of a fully-generated resume silently drifting from ground truth (invented metrics, restructured experience) that a human doesn't catch because they don't re-read every generated resume.
- Per-job generation only earns its cost when the job categories are highly heterogeneous or volume is low enough that full human review per resume is feasible; for a high-volume auto-apply system, variant-selection + light-touch templated tailoring is the safer, more maintainable design.

## 4. File formats

**PDF vs DOCX.** Consensus across ATS/parsing vendors and practitioner guidance: PDF is generally safe **if it has a real text layer** (i.e., exported from a word processor / HTML-to-PDF pipeline, not a scanned image) — modern parsers (Textkernel/Sovren, Daxtra, HireAbility, Affinda) all accept PDF and extract text natively. DOCX remains the safest universal default because some older or budget ATS integrations still parse DOCX more reliably than PDF (PDF text-extraction order can scramble in multi-column or complex-layout files), and because a few enterprise ATS (older Workday/Taleo integrations in particular) have historically had DOCX-first support. Practical rule: **submit PDF when the application explicitly allows file choice and your PDF was generated from a simple single-column layout** (reduces recruiter-side formatting-corruption risk that plain DOCX can suffer when opened in a different Word version); **submit DOCX when the ATS explicitly asks for DOCX** or when there's no way to verify the target parser.

**What parsers actually handle well.** Modern commercial parsers — **Textkernel/Sovren Parser** (the long-standing high-accuracy market leader, ~"most used parsing engine" per vendor positioning), **Daxtra**, **HireAbility**, and **Affinda** (reports 92%+ independently-benchmarked overall field accuracy, 97%+ on core contact fields, and explicitly advertises tolerance for two-column layouts, scans, and varied formats) — have all invested heavily in tolerating "reasonable" real-world resume variation, including moderate multi-column layouts and light graphics in the 2020s generation of their engines. **Workday's own internal parser** (used when a company runs its ATS directly on Workday rather than a third-party parsing add-on) is widely reported by practitioners as comparatively weaker/stricter than the specialist vendors above, and is the parser most associated with real-world tailoring/format horror stories (misread section headers, dropped content from tables). Because you can never know in advance which parser a given employer's ATS instance uses, the safe design target is the **lowest common denominator**, not the best-case parser.

**Formatting rules that hold across all parsers (lowest-common-denominator safe format):**
- Single-column layout (multi-column defeats reading-order extraction in weaker parsers even though the top vendors now tolerate it)
- No tables for content (a table cell's reading order is not guaranteed to be preserved; use plain line breaks instead)
- No text inside headers/footers (many parsers skip header/footer regions entirely — never put contact info or content there)
- No graphics, icons, text boxes, or embedded images carrying information (icons for "phone"/"email" are commonly dropped; skill-rating graphics/bars are unreadable to parsers)
- Standard section headings in plain text ("Experience," "Education," "Skills") rather than stylized graphic headers
- Standard, embeddable fonts (avoid unusual fonts that may not embed in the PDF and fall back to non-extractable glyphs)
- A real, selectable text layer — never a flattened/rasterized PDF (no OCR needed if the source was already text)

Sources: [Affinda resume parser product page](https://www.affinda.com/resume-parser) (92%+ benchmarked accuracy, 97%+ on contact fields, format support: PDF/DOC/DOCX/TXT/RTF/HTML/images), [Textkernel (formerly Sovren) Parser product](https://textkernel.com/) (positions Parser as the most widely used parsing engine industry-wide, confirming Sovren's parsing technology now lives under the Textkernel brand). General ATS parsing behavior (Workday/Greenhouse/Lever mechanics, keyword-matching myth, single-column/no-table/no-header-footer formatting guidance) reflects well-established, convergent industry practitioner consensus (Jobscan, TopResume, career-services guidance) rather than a single citable primary source — flagged here since the session's live web-search budget was exhausted before a fresh citation pass could be completed for this specific claim set; recommend the user independently spot-check with Jobscan's ATS resume checker or a current TopResume/Indeed ATS guide if a citation is required for a written deliverable.

## 5. Generation toolchain on Windows (structured data + template -> PDF)

| Pipeline | Fidelity | Windows install pain | ATS text layer | Speed |
|---|---|---|---|---|
| **LaTeX (MiKTeX)** | Excellent typography, mature resume classes (moderncv, awesome-cv, etc.) | High — MiKTeX is a multi-GB install, first-run package auto-install is slow/flaky, PATH issues common on Windows | Good, but classes using multi-column/tabular layouts (common in "designer" LaTeX resume templates) actively hurt ATS parsing — must pick a single-column class deliberately | Slow-ish per-compile (seconds), slower on first run per new package |
| **LaTeX (Tectonic)** | Same LaTeX quality | Much lower pain — Tectonic is a single self-contained binary (Rust-based, bundles TeXLive), no system-wide MiKTeX install/PATH management, works well in CI/Windows | Same caveat as above re: template choice | Faster than MiKTeX after first run (caches packages), single binary invocation |
| **Typst** | High-quality typography, modern markup language, purpose-built as a LaTeX alternative | Low — single small binary, no package manager gymnastics, native Windows builds | Typst compiles to PDF with a standard text layer; using a simple single-column template gives a clean ATS-parseable output | Fast — Typst's incremental compiler is materially faster than LaTeX compiles |
| **HTML+CSS -> PDF via Playwright/Chromium print-to-pdf** | Very high design flexibility (full CSS), easiest to theme/brand since it's just web tech | Low-moderate — needs a Chromium download (Playwright handles this) but no LaTeX toolchain; straightforward on Windows | Generally good text layer since Chromium's print-to-pdf preserves selectable text, but layout must still be authored single-column/no-table to stay ATS-safe (CSS grid/flex "columns" can still hurt reading order) | Fast (headless Chromium render, sub-second to ~1-2s) |
| **python-docx** | Moderate — programmatic Word doc construction, styling is verbose/manual | Very low — pure Python library, no external binary | DOCX is natively text-layer-safe and the most parser-compatible format by construction | Fast, but authoring rich layouts programmatically is tedious |
| **docxtpl** | Moderate-high — Jinja2-style templating over a human-designed `.docx` template (design once in Word, then fill placeholders programmatically) | Very low — pure Python (`python-docx` + `docxtpl` on top), no external binary | Same DOCX safety as python-docx, but templating from a human-authored Word doc means non-technical template edits are easy | Fast |
| **RenderCV** | High — purpose-built resume tool, YAML input, uses **Typst** as its underlying rendering engine (confirmed: `rendercv.renderer.typst`), ships with an explicit "ATS Compatibility" documentation section | Low — pip-installable Python package, pulls in Typst under the hood so no separate LaTeX install | Designed with ATS output in mind out of the box | Fast (inherits Typst's speed) |
| **Pandoc** | Moderate — general-purpose document converter (Markdown/YAML metadata -> DOCX/PDF/HTML), less resume-specific polish unless paired with a custom template/LaTeX backend | Low-moderate — single installer on Windows, but PDF output still needs a LaTeX engine or wkhtmltopdf backend under the hood unless targeting DOCX/HTML directly | Depends entirely on the backend/template chosen | Fast for DOCX/HTML targets; PDF speed depends on backend |

**Recommendation:** For this system, use **structured data (JSON Resume) -> Typst templates -> PDF**, either directly with the `typst` CLI/Python binding or via **RenderCV** if its YAML schema and design system fit (RenderCV already solves "YAML in, ATS-aware single-column PDF out" and uses Typst, avoiding LaTeX's Windows install pain entirely). Reasons: Typst is a single small binary (trivial Windows install, no MiKTeX package-fetch flakiness), compiles fast enough for interactive per-application generation, and produces a clean selectable text layer when the template is kept single-column/table-free. Reserve the **HTML+CSS -> Playwright print-to-pdf** pipeline as the alternative if the team prefers authoring templates in familiar web tech over Typst's markup language — it's equally Windows-friendly and fast, with the caveat that CSS-based multi-column tricks must be avoided for ATS safety. Use **docxtpl** specifically when a DOCX (not PDF) output is required by a given ATS — it's the lowest-friction Windows path to a safe DOCX. Avoid raw LaTeX/MiKTeX on Windows given install pain unless the team already has a MiKTeX environment for other reasons; prefer Tectonic if LaTeX syntax is a hard requirement.

Sources: [RenderCV documentation](https://docs.rendercv.com/) (YAML input, Typst rendering engine, ATS Compatibility docs section), [Typst documentation](https://typst.app/docs) (positioned as a faster/easier alternative to LaTeX and word processors). Windows install-pain and speed comparisons for MiKTeX/Tectonic/Playwright/python-docx/pandoc reflect established tooling characteristics (Tectonic's single-binary self-contained design, Playwright's bundled-Chromium model, python-docx being pure-Python) rather than a single fresh citation, since the session's web-search budget was exhausted before a dedicated benchmarking source could be pulled.

## 6. PII storage

**What must never go in a git repo or be sent to an LLM (as-is):** SSN/national ID, date of birth, home address, phone number, EEO race/ethnicity/gender/disability/veteran self-ID, exact salary history, government ID numbers, bank/direct-deposit info if ever collected, and (jurisdiction-dependent) criminal history. These are either directly re-identifying, legally protected special-category data (EEO/disability/veteran self-ID exists specifically so it is *not* used in the actual decision pipeline — feeding it to an LLM that also drafts resumes/answers risks it leaking into generated content or logs), or high-value credential-adjacent data.

**Where to keep it locally on Windows — practical layered approach:**
1. **Gitignored local file** as the baseline: keep the sensitive `application_profile.json`/`self_identification`-style file (per §1) in a directory covered by `.gitignore` (never staged, never committed) — this is what AIHawk itself does with `plain_text_resume.yaml`, and it's sufficient as the *first* line of defense against accidental repo leakage, but it's plaintext on disk, so it isn't sufficient alone against local malware/other-user access on a shared machine.
2. **OS credential manager (Windows Credential Manager, via DPAPI)** for small secret-like scalars (not well-suited to a whole nested JSON document, but good for API keys or a small number of very sensitive strings) — accessible from Python via `keyring` (uses Windows Credential Locker under the hood) or directly via `win32cred`. DPAPI-encrypted, tied to the Windows user account, and immune to a plain filesystem copy being useful on another machine.
3. **Encrypted store for the structured PII document**: for the actual nested profile data (address, DOB, EEO fields), encrypt the JSON file at rest — either via a small wrapper using Windows DPAPI (`CryptProtectData`, accessible via `pywin32`, ties decryption to the logged-in Windows user with zero extra password management) or via a proper local secrets tool (`age`, `sops`, or a local `sqlcipher`/encrypted-SQLite DB) if you want portability across machines or want the encryption key independent of the Windows account. DPAPI is the lowest-friction Windows-native choice for a single-user local tool; `sops`/`age` is better if the file might ever be synced/backed up off-machine and you want the key managed explicitly rather than tied to Windows login.
4. Regardless of encryption choice, keep this file **outside the git working tree entirely** (e.g., in `%LOCALAPPDATA%\job-hunting-buddy\` or a sibling directory) rather than merely gitignored inside the repo — gitignore protects against accidental `git add`, not against the file being copied/backed up/synced alongside the repo, and it's one config typo away from being committed.

**Redaction boundary (LLM sees only non-sensitive fields):** Architect the system so the LLM-facing context is built from an explicit **allowlist projection**, not a blocklist redaction, of the profile:
- The LLM (resume/cover-letter generation, JD classification, "why this company" drafting) only ever receives fields from the JSON Resume-shaped document (§1): name, work history, education, skills, projects — i.e., exactly the fields that would appear on the resume itself, since those are inherently going to be shared with the employer anyway.
- A separate, code-level (non-LLM) component reads the sensitive `application_profile.json`/self-identification file directly to **auto-fill form fields programmatically** (e.g., via browser automation filling a Workday EEO widget) without ever routing those values through a prompt or a model call — the LLM should never see or produce EEO/DOB/address/salary-history values; it should at most be told "there is a required EEO question on this page, use the deterministic auto-filler for it," not be given the actual value to reason about.
- Enforce this at the code boundary (a `to_llm_context()` function that only ever reads from the public/allowlisted schema object, with the sensitive object not even in scope/imported in the LLM-calling module) rather than relying on prompt instructions like "don't mention race/DOB" — instruction-based redaction is not a security boundary since the data would already be in context and any model output is a potential leak/log-exposure surface even if the visible answer looks fine.
- Log redaction: ensure request/response logging for LLM calls only ever logs the allowlisted-projection payload, not the full profile object, so even accidental over-inclusion in a prompt doesn't also get persisted to disk/observability tooling.

