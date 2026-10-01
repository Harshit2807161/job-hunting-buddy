# Job Hunting Buddy — Research Synthesis

**Date:** 2026-09-12
**Question:** How are agentic new-grad job-application pipelines actually built in practice, what breaks them, and what architecture does the evidence point to?
**Method:** Six parallel research agents (data source, prior art, ATS internals, agent architecture, documents, risk/effectiveness). Raw findings in [`research/`](research/).

---

## The three numbers that drive the design

| Number | Meaning |
|---|---|
| **35%** | End-to-end completion of a 10-step form at 90% per-step reliability. Reliability compounds; step count is the enemy. |
| **31%** | Share of *currently-active* listings on the four easiest ATSes (Greenhouse, Ashby, Lever, SmartRecruiters). |
| **2,843 → 1** | Applications to offers in the most-cited mass-apply run. Volume is not the lever. |

---

## Six findings that close design questions

### 1. Every ATS submission endpoint is credentialed — there is no API shortcut

Documented submission endpoints exist, and all of them require a key only the **hiring employer** or an approved job-board partner can hold:

- Greenhouse — `POST boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` (Basic Auth, employer's Job Board API key)
- Lever — `POST api.lever.co/v0/postings/{site}/{id}?key=APIKEY` (employer-issued key, 2 req/sec)
- Ashby — `applicationForm.submit` (documented `_systemfield_*` schema + presigned upload)
- SmartRecruiters — `/postings/{uuid}/candidates` (OAuth `candidate_applications_manage`)
- Workable — `/jobs/:shortcode/candidates` (OAuth `w_candidates`)
- iCIMS, Taleo, SuccessFactors — no candidate-facing API published at all

There is a gray-area path: Greenhouse and Ashby hosted board pages fire their own board-token-scoped JSON calls from the browser, reverse-engineerable from the Network tab. It's fragile, and Greenhouse's opt-in invisible reCAPTCHA exists precisely to close it.

**Implication:** browser automation is the only route. The real question is how much of it runs under a model.

### 2. Reliability compounds — minimise steps under model control

No tool anywhere, commercial or open-source, publishes a verified job-application success rate. Every available number is a general web benchmark, a single-user anecdote, or marketing. Best proxy: **Skyvern's own write-task baseline of ~60–64%**.

Combined with compounding, this is decisive:

| Per-step reliability | 5 steps | 10 steps | 14 steps |
|---|---|---|---|
| 95% | 77% | 60% | 49% |
| 90% | 59% | **35%** | 23% |
| 85% | 44% | 20% | 10% |

Two consequences:

- **Collapse steps.** A deterministic adapter filling a known Greenhouse form in one scripted pass has effectively one step, not twelve.
- **Checkpoint, don't restart.** A run that dies on step 9 of 12 should resume at 9 — re-running the first eight is where compounding bites again.

### 3. DOM and accessibility tree beat vision for form filling

Four independent projects converged here:

- **Skyvern's own data**: switching DOM serialisation from JSON to raw HTML cut cost 11.8% and raised success 3.9pts — its own evidence argues against its vision-first default.
- **Stagehand** grounds on the Chrome accessibility tree.
- **browser-use** grounds on an indexed DOM interactive-element list.
- **Playwright MCP** `browser_snapshot` returns an a11y tree with stable `ref` ids — a few hundred to low-thousands of tokens vs ~4,784 for one computer-use screenshot (5–20× cheaper).

WebBench splits it neatly: Skyvern leads WRITE tasks (form filling), Claude Computer Use leads READ tasks.

**Implication:** a11y-tree grounding as primary; vision reserved for canvas, CAPTCHA detection, and post-submit verification.

### 4. The submit click stays human

This is the line every credible tool converged on independently:

- **Simplify Copilot** (mainstream, funded) autofills everything and still requires a human Submit click.
- **`neonwatty/job-apply-plugin`** explicitly never auto-submits.
- **Workday Site Terms** ban "automated software, scripts" outright, with no applicant carve-out. LinkedIn User Agreement §8.2 likewise.

Legally, *hiQ v. LinkedIn* (9th Cir. 2022) and *Van Buren v. US* (SCOTUS 2021) mean a person autofilling their own truthful application has essentially no CFAA exposure. But that's legal risk, not contractual — platforms can still suspend accounts, and enforcement is technical (CAPTCHA walls, session flagging), not litigation. No case found of an ATS vendor suing an individual applicant.

Notably, ban risk correlates with automating **inside a live logged-in session** (LinkedIn), not with per-posting form fill on public ATS pages. SimplifyJobs URLs go straight to employer ATS pages, which is the safer surface.

### 5. Tailored and fast beats high-volume

- Baseline: ~2–3% interview conversion, ~75% filtered before human review, ~191 applicants per tech hire.
- **Tailored vs generic: 5.75% vs 2.68%** interview rate (Huntr, ~1.39M applications). Treat the magnitude as vendor-reported; the direction is credible.
- **Speed**: TalentWorks (~1,600 applications) found up to 8× higher interview odds applying within four days. More recent claims cite 2–3× within 24–48h.
- Referrals convert ~30% vs ~7% for cold applications.
- Market context: entry-level dev postings down ~40% vs pre-2022; NACE projects 1.6% hiring growth for Class of 2026.

Speed and quality are both real and neither substitutes for the other. The system should be **fast and tailored, deliberately rate-limited** — not a 150/day blaster, which is exactly the pattern that reads as bot-like.

### 6. Employers now screen for exactly this

- **Greenhouse Real Talent** (with CLEAR) — AI fraud/bot/mass-application detection plus biometric ID verification, surfaced to recruiters as a "Verified" badge.
- **Phenom** shipped a 2025 fraud-detection agent using facial recognition, voice matching, and AI-answer detection.
- **LinkedIn** rolled out underqualified-applicant warnings (Aug 2026) after automated-application limits failed; reports blocking 78.2M fake accounts and flagging 23.5M automated sessions in a quarter.
- Detection signals: generic AI phrasing ("I am writing to express my strong interest", "delve", "spearheading transformative initiatives" from entry-level candidates), submission-burst clustering, duplicate blasting.
- 67% of hiring managers claim they can spot AI-written cover letters; 54% view them negatively (TopResume 2026).

**Implication:** pacing and genuine per-role tailoring, not evasion. Residential proxies and fingerprint spoofing are out of scope — they exist to defeat the same fraud controls a legitimate personal tool has no reason to evade.

---

## Where the jobs actually are

Counted from all 20,132 records in `SimplifyJobs/New-Grad-Positions`. **The active-listing mix differs sharply from all-time, and the active column should drive build order.**

| ATS | Tier | % active | % all-time | Reported autofill accuracy |
|---|---|---:|---:|---:|
| **Greenhouse** | Build first | 11.0 | 7.9 | 85–90% |
| **Ashby** | Build first | 7.0 | 4.0 | 85–90% |
| **SmartRecruiters** | Build first | 6.5 | 4.9 | 85–90% |
| **Lever** | Build first | 6.4 | 2.7 | 85–90% |
| Workday | Defer | 27.4 | 45.5 | ~70% |
| Oracle / Taleo | Defer | 5.5 | 12.9 | 40–50% |
| iCIMS | Defer | 4.0 | 4.3 | 40–50% |
| Long tail (~2,120 hosts) | Model fallback | ~32 | — | — |

Accuracy figures are what commercial tools report for their own products (Simplify, Teal) — relative signal, not guarantees.

**Difficulty ranking (easiest → hardest):** Lever → Ashby → Greenhouse → Workable → SmartRecruiters → Jobvite → SuccessFactors → iCIMS → Eightfold → Phenom → Taleo/Oracle → **Workday**.

Workday is 27% of live postings at the hardest tier — per-employer account creation plus a 5–8 page wizard. `LeoLaborie/claude-apply` implements Lever/Greenhouse/Ashby/Workable and leaves **Workday scan-only**. Teal reports ~60% there.

**Four adapters covering 31% at 85–90% is a far better first build than one covering 27% at 70%.**

---

## Data source: `SimplifyJobs/New-Grad-Positions`

**Schema** — `.github/scripts/listings.json` on branch `dev`, flat JSON array:

```
source, category, company_name, id (UUID), title, active (bool),
date_updated, date_posted (unix epoch), url, locations (array, free text),
company_url, is_visible (bool), sponsorship, degrees (array)
```

Two properties that simplify everything:

- **`url` is already the direct employer ATS link.** Only 3 of 20,132 (0.015%) route through `simplify.jobs`. No unwrapping needed. (`company_url` is a different field and *is* always a Simplify company page — don't confuse them.)
- **`id` is a stable UUID, never reused.** Closures flip `active:false` in place; reposts get a fresh id. New-job detection is an exact set-diff, not a heuristic.

**Cadence:** commits alternate every ~30 min — a data commit, then a GitHub Actions README regeneration. The README is a *filtered view*; the JSON is the source of truth.

**Polling strategy (layered):**
1. Poll the commits Atom feed (`.../commits/dev.atom`) every 5–15 min to cheaply detect a data commit.
2. Conditional GET on raw.githubusercontent.com with `If-None-Match` — avoids re-downloading 13.7 MB when unchanged.
3. Diff by `id`.

Rate limits: unauthenticated REST 60 req/hr/IP, PAT 5,000 req/hr. A 304 is quota-free only when authenticated. Webhooks aren't usable (can't register on a repo you don't own).

**Filter enums (exact, verified):**
- `sponsorship` ∈ `{"Other"` (99.5%), `"U.S. Citizenship is Required"`, `"Does Not Offer Sponsorship"`, `"Offers Sponsorship"}` — exclude the middle two.
- `degrees` ∈ `{Bachelor's, Master's, PhD, Associate's, Certificate, MBA, Bootcamp, MD, JD, Incomplete, PharmD, DO, DDS, DVM}` (empty = unspecified).
- `category` mostly ∈ `{Software, AI/ML/Data, Hardware, Quant, Product}`.
- **No country field** — `locations` is unnormalised free text, must be pattern-matched.

**⚠️ The `active` flag is not live-verified.** Stale community rows are auto-flagged only after 4 months (`mark_stale_listings()`); bulk closures come from maintainers pasting URLs into a labeled GitHub issue with **zero HTTP verification**. Treat `active && is_visible` as necessary but not sufficient — HEAD/GET the posting before spending effort on it.

**Alternative sources:** `vanshb03/Summer2026-Internships` has verified public JSON (different schema: singular `season`, no `degrees`/`category`, only 471 records). `speedyapply/2026-SWE-College-Jobs` is Markdown-only. No verified free public JSON found for Hiring Cafe, JobRight, or Levels.fyi.

---

## Prior art: mostly negative results

| Tool | Status | Takeaway |
|---|---|---|
| **AIHawk** (30.3k ★) | **Rebranded away from job applications entirely**; applier code only in unmerged forks | The one durable idea: `answers.json` cache keyed by sanitized question text. LLM called only on cache miss. Top issue category: selector/xpath breakage. |
| **JobFunnel** | **Archived** — maintainer cited "aggressive anti-automation" | Lightweight scraping stopped being viable |
| **JobSpy** (4.3k ★) | Active | Works via requests + heavy proxy rotation, constant 429s |
| **browser-use** (114k ★) | Active, $17M seed | Indexed DOM grounding, vision optional. 89.1% self-reported WebVoyager |
| **Stagehand** (24k ★) | Active, Browserbase $67.5M | A11y-tree grounding, "self-healing" = re-ask LLM on selector break. Published evals: 74–87%, $0.50–$10.02/task |
| **Skyvern** (23k ★) | Active, ships a "Jobs Agent" | Planner→Actor→Validator. **No job-specific success rate published** |
| **`neonwatty/job-apply-plugin`** (107 ★) | Most substantial Claude-ecosystem example | Explicitly never auto-submits |
| **`LeoLaborie/claude-apply`** | Lever/Greenhouse/Ashby/Workable | **Workday scan-only** — independent confirmation it's the universal hard case |

**Anecdotes worth knowing:** AIHawk's founder applied to ~1,000 jobs in 2 days (50 interviews), got his LinkedIn restricted by Trust & Safety (reversed same day). A 404 Media/TechCrunch reporter ran 2,843 applications → 4 interviews → 1 offer.

**Commercial tools** (Simplify, Teal, Huntr, Jobscan, LazyApply, Sonara, Massive, JobRight, Careerflow, AI Apply): all closed-source, no public APIs. "Autofill" vs "auto-apply" is systematically blurred in marketing — every tool claiming true autonomy shows a documented gap vs reported behaviour. Billing dark patterns recur (Massive, AIApply, JobRight). Sonara died Feb 2024 from reliability/funding issues.

**Failure mode ranking (most → least universal):**
Workday/multi-page dynamic forms > custom free-text questions > file upload > selector/DOM breakage > CAPTCHA (universally unsolved by careful projects) > login/2FA (avoided via session reuse, not solved) > aggregator→ATS handoff breaks > platform ban risk.

---

## Recommended architecture

A **deterministic spine with a scoped model escape hatch**. Anthropic's "Building Effective Agents" distinction maps directly: everything except free-text answers and unfamiliar-form recovery is a closed decision, so it belongs in code. A documented postmortem found "LLMs are optimistic — without hard constraints they find reasons to match rather than reject"; deterministic scoring cut false positives ~60%.

| # | Stage | Owner | Notes |
|---|---|---|---|
| 01 | Poll | code | Atom feed every ~15 min; conditional `If-None-Match` GET |
| 02 | Diff | code | Set-diff on `id` — exact, not heuristic |
| 03 | Filter | code | Sponsorship + `active && is_visible` + location match, then **live URL check** |
| 04 | Classify & route | model | Cheap model picks resume variant; route on ATS hostname. Closed-set, checkable |
| 05 | Build documents | code | Model returns **slot values only**, never prose. Code reassembles + validates |
| 06 | Fill — adapter fast path | code | Known ATS, known selectors, one scripted pass. ~31% of live postings |
| 07 | Fill — model fallback | model | A11y snapshot with stable refs, **hard turn cap**, strict action allowlist |
| 08 | Answer unknown questions | model | Answer-bank miss only. Low confidence escalates rather than guesses |
| 09 | **Submit gate** | **human** | Blocked in code by `PreToolUse` hook — runs *before* permission modes, holds under bypass |
| 10 | Record & crystallise | code | Ledger + trace + screenshots; promote working selector paths into generated adapters |

**Stage 10 matters more than it looks.** When the fallback succeeds on a novel form, recording the working selector path and promoting it to a deterministic adapter is the escape from the selector-rot treadmill that archived JobFunnel and dominated AIHawk's issue tracker. (Formalised in recent work as "Blueprint First, Model Second" / "Progressive Crystallization".)

### Runtime choices

- **Browser:** Playwright (or Playwright MCP) against a **dedicated non-default Chrome profile**. ⚠️ Chrome 136+ blocks `--remote-debugging-port` against the *default* profile.
- **Models:** Haiku 4.5 for classification/routing, Sonnet 5 for field mapping (the workhorse), Opus 5 for cover letters and confidence judgment. Estimated ~$0.10–0.25/application — **cost is not the binding constraint, reliability is.**
- **Prompt caching:** cache the profile + resume + answer-bank prefix with an ephemeral breakpoint; batch jobs per poll cycle to keep it warm.
- **State:** SQLite with `jobs` / `applications` / `answers` tables. Dedupe via `sha256(company + title + external_id-or-url)` with a UNIQUE constraint doing double duty as the idempotency guard. Resume via checkpointed `current_step` / `form_state_json` / Playwright `storage_state`. Permanent failures → `needs_human` rows as the dead-letter queue. No Celery/Temporal needed at single-machine scale.
- **Observability:** full Playwright tracing on *every* attempt (not just failures), replayed with `npx playwright show-trace`. Per-application artifact folder: `runs/<id>/{trace.zip, action_log.jsonl, screenshots/, storage_state.json}`, linked from the ledger.

### Human-in-the-loop

Batch-review-then-submit as default; dry-run always on. Confidence banding: >90% auto-proceed, 60–90% flag for review, <60% escalate. Always hard-gate: the submit click, sensitive questions, and first-time novel questions. LangGraph's `interrupt()` four-way vocabulary (approve / edit / reject / respond) is worth mirroring even without LangGraph.

### The answer bank

The highest-leverage single component — it's what makes application #50 nearly free.

```
normalize → exact match → fuzzy match (rapidfuzz, ~85–90 threshold)
          → embedding similarity (local model + sqlite-vec/FAISS, ~0.88)
          → cheap-LLM disambiguation → ask human once and persist
```

Factual answers reused verbatim; narrative answers reused as templates and re-tailored. AIHawk notably lacks a persistent ledger for this — a gap to close.

---

## Privacy boundary: two stores, one wall

Enforced by an **allowlist projection in code** — a `to_llm_context()` that can only ever read the left-hand store. **Not** by a prompt instruction saying "don't mention this."

| `profile/resume.json` — **visible to model** | `profile/application_profile.json` — **sealed** |
|---|---|
| JSON Resume schema | Gitignored, encrypted at rest, outside the repo tree |
| Work history, education, projects | Address, phone, date of birth |
| Skills, links, public portfolio | Work authorisation & sponsorship status |
| Job descriptions, company pages | EEO self-ID, veteran status, CC-305 disability |
| | Salary expectation, start date, references |
| | **Never in a prompt. Never in a log. Filled by deterministic code only.** |

Storage: Windows Credential Manager/DPAPI, or `age`/`sops`/SQLCipher.

**This boundary also absorbs the prompt-injection risk.** Anthropic's Claude-in-Chrome guidance flags indirect prompt injection from untrusted web content — and this agent reads arbitrary job descriptions and ATS pages, then acts on them. Because a job description can only ever produce *extractive slot values that are regex-validated against a fixed template*, and because the submit action is gated in code rather than by model judgment, a hostile posting has no reachable lever.

---

## Never-infer fields

False I-9 statements carry criminal exposure (up to 5 years cited). EEO self-ID is legally voluntary and must be kept separate from the hiring record. These route to stored human-supplied values or to a prompt — **never to a generation**:

- Work authorisation
- Visa sponsorship, now or future
- Race and ethnicity
- Gender
- Veteran status
- Disability — CC-305 (OMB 1250-0005, updated form mandatory since July 2023)
- Criminal history
- Prior employment dates and reasons for leaving
- Education verification
- Salary history
- Reference contacts
- **Truth attestation / e-signature** — always a deliberate human action

Offer rescission for application inaccuracies is well-documented, and lying can create civil fraud exposure if the employer relied on it.

---

## Documents

**Profile schema:** JSON Resume (mature JSON Schema) for resume-facing data + a separate AIHawk-shaped `application_profile.json` for form logistics. JSON Resume has zero fields for autofill logistics — that's the gap the second file fills.

**Cover letter — minimal edits, verifiably.** Structure: 3–4 paragraphs, 300–400 words (hook → proof via projects → company-specific paragraph → close). Template-with-slots beats full rewrite for hallucination safety.

The validation approach makes "modify minimally in set places" a *checkable invariant*:

1. Force structured output — the model returns **only the slot values**, never the reassembled letter.
2. Code rebuilds the letter from the template.
3. Verify with a regex compiled from the template (literals escaped, placeholders as capture groups), anchored end-to-end.
4. Reconstruction equality check + difflib sanity pass.
5. Any mismatch → reject and retry.

`{{WHY_THIS_COMPANY}}` must be **extractive** from the job description — the claimed source quote has to be a locatable substring of the JD, or it's rejected as fabrication. Never parametric memory.

**Resume tailoring:** ATS keyword matching is mostly parsing + recruiter search/rank, **not a hard auto-reject gate** — that popular myth is overstated. Keyword stuffing hurts once a human reads it. Recommendation: maintain **3–5 pre-built variants** (backend / ML / full-stack / infra), classify the JD to pick one, then light templated tailoring. Safer and cheaper than per-job generation.

**File format:** PDF with a real text layer is fine with modern parsers (Textkernel/Sovren, Daxtra, HireAbility, Affinda — 92%+ field accuracy reported). DOCX remains the safest lowest-common-denominator, especially against **Workday's own parser, which practitioners report as weaker**. Universal rules: single column, no tables, nothing in headers/footers, no graphics, standard fonts, real text layer.

**Toolchain (Windows):** JSON Resume → **Typst** templates → PDF, or **RenderCV** (wraps Typst with YAML input, documented ATS compatibility). Single-binary install, no MiKTeX flakiness, fast compiles, clean text layer. `docxtpl` as the DOCX fallback.

---

## Guardrails to implement

- **Never-infer list** hard-blocked from LLM authorship, routed to human input.
- **Mandatory approval points:** final review before submission; explicit attestation click (never auto-ticked); fresh EEO prompts (never auto-filled from stored profile); CAPTCHA/anti-bot handoff; first submission per employer.
- **Pacing:** hard daily cap well under the ~150/day threshold industry reporting flags as abusive; randomised human-scale delays; no submission bursts; auto-cooldown on repeated errors or challenges.
- **Honesty invariant:** every generated sentence must trace to an explicit profile/resume fact.
- **Audit log:** immutable timestamped record of what was submitted where, human- vs tool-authored fields, approvals, anti-bot events.
- **Kill switch:** instant unconditional halt, user-triggerable anytime, auto-triggered on any platform "unusual activity" warning.
- **Never build CAPTCHA solving.** Detect and hand off.

---

## Build order

Each phase ships something useful even if you stop there.

### Phase 1 — Discovery, with no browser at all
Poll, diff, filter, ledger. No automation, no forms, no risk. Given up to 8× interview odds inside four days, simply *knowing* about a matching role within 15 minutes is most of the available edge.
**Ships:** a daily digest of new matching roles + a SQLite ledger.

### Phase 2 — Profile and documents
JSON Resume + sealed application profile. 3–5 resume variants via Typst/RenderCV. Cover letter as a fixed template with three slots, reassembled and regex-validated in code.
**Ships:** a correct tailored PDF pair per job, on demand.

### Phase 3 — Four adapters, fill only
Greenhouse, Ashby, Lever, SmartRecruiters via Playwright against a dedicated non-default Chrome profile. Fill, screenshot, queue. You click submit.
**Ships:** ~31% of live postings, filled and waiting for review.

### Phase 4 — Answer bank and model fallback
Normalise → exact → fuzzy → embedding → ask once and persist. Model fallback handles the long tail behind a turn cap. Crystallisation loop starts here.
**Ships:** coverage of novel questions and unknown ATSes.

### Phase 5 — Workday, deliberately last
Per-tenant account creation, 5–8 page wizard, ~70% even for funded commercial teams. Worth attempting only once crystallisation is producing adapters reliably — and **semi-manual is a legitimate end state here**.
**Ships:** the remaining 27%, at materially lower confidence.

---

## Open decisions

1. **How far does autonomy go?** The evidence points hard at fill-then-human-submits, and that's what's designed above. Unattended submission on the four easy ATSes changes the review queue and hook design — decide now rather than retrofit.
2. **Language and runtime.** Python + Playwright is the better-trodden path. TypeScript + Claude Agent SDK gives tighter hook/permission-mode integration for the submit gate. Recommend Python unless you'd rather live in TS.
3. **What does your filter actually look like?** Sponsorship needs, target locations, categories, whether non-software roles are in scope. Phase 1 is largely this filter.
4. **What exists already?** Current resume, any cover letter you're happy with, and the LaTeX/Word source. Phase 2 goes much faster from your real document.

---

## Caveats

- No independently verified job-application success rate exists anywhere. All accuracy figures are vendor self-reports or single-user anecdotes.
- Greenhouse/Lever DOM selectors are widely reported but were not re-verified live in this research pass. Ashby's `_systemfield_*` naming and Workday's `data-automation-id` values are verified from official docs and near-universal tooling reports respectively.
- The Huntr tailoring lift (5.75% vs 2.68%) is vendor-reported; direction credible, magnitude not independently confirmed.
- Research agent 5 exhausted the session WebSearch budget partway; its later sections lean on WebFetch-verified vendor docs rather than fresh search citations (flagged inline in `research/research-05-documents.md`).

---

*Full findings: [`research/research-01-datasource.md`](research/research-01-datasource.md) · [`02-priorart`](research/research-02-priorart.md) · [`03-ats`](research/research-03-ats.md) · [`04-architecture`](research/research-04-architecture.md) · [`05-documents`](research/research-05-documents.md) · [`06-risk`](research/research-06-risk.md)*
