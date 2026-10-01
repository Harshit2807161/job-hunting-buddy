# Research 06 — Risk, Policy, and Effectiveness Layer for a Personal Job-Application Agent

Scope note: this is for a personal-use tool that applies on the user's own behalf with truthful information. That is legally distinct from third-party scraping-for-resale or credential-stuffing style abuse, and the research below is organized to keep that distinction sharp.

---

## 1. Terms of Service Reality

### What the platforms actually say

**Workday** (End User Agreement / Site Terms, workday.com/en-us/legal/end-user-agreement.html and site-terms.html) — the most explicit and strict of the ATS vendors:
- "use automated software, scripts, or other methods of accessing or using the Website without Workday's consent" — prohibited
- "scrape from, or otherwise use, the Website content, data, material submitted, or other information whatsoever" — prohibited
- "use manual or automated software, devices, or other processes to 'crawl,' 'scrape,' or 'spider' any page of the Website" — prohibited
- "develop or use any applications that interact with our Sites without our prior written consent" — prohibited
- reverse engineering also prohibited
- No specific carve-out distinguishing "filling my own application form via a script" from scraping — the language is broad enough to sweep in autofill/auto-submit tooling. Practically, Workday's clause is aimed at bot-crawling job boards and building competing data products, not at an individual clicking through their own application with an extension's help, but a strict reading covers both.

**LinkedIn** (User Agreement §8.2, linkedin.com/legal/user-agreement) — also explicit:
- "Develop, support or use software, devices, scripts, robots or any other means or processes (such as crawlers, browser plugins and add-ons or any other technology) to scrape or copy the Services"
- "Use bots or other unauthorized automated methods to access the Services, add or download contacts, send or redirect messages, create, comment on, like, share, or re-share posts, or otherwise drive inauthentic engagement"
- "Override any security feature or bypass or circumvent any access controls or use limits"
- "Create a false identity on LinkedIn, misrepresent your identity, create a Member profile for anyone other than yourself"
- LinkedIn's Help Center separately maintains a "Prohibited software and extensions" list; using listed tools is grounds for account restriction, and "any prohibited tools they're using may become non-operational without notice" (LinkedIn Help).
- No explicit textual carve-out for automating your own Easy Apply flow, but LinkedIn's actual enforcement (see below) treats bulk/rapid automated application activity as a bot-detection signal regardless of whether the applications are truthful.

**Lever** (lever.co/legal/terms-of-service) — thin on this topic. The public ToS is largely a B2B contract with Lever's paying customers (employers) governing their use of the platform; it does not contain applicant-facing automation/scraping clauses comparable to Workday's or LinkedIn's. Restrictions found (§1.2) are customer-side (no reverse engineering, no reselling access) and don't speak to candidate behavior. This means Lever's own ToS is not the controlling risk for an applicant-side tool — the risk there is more about IP-based bot detection/CAPTCHA than an explicit contractual prohibition on the candidate.

**Greenhouse** — no applicant-facing ToS clause on automated submission was found in public documents. What does exist is the **Sourcing Automation Addendum** (greenhouse.com/sourcing-automation-addendum), but that governs *employer-side* recruiter tooling (looking up candidate emails, adding prospects) — it is not about applicants automating their own submissions. Greenhouse job boards are explicitly public/unauthenticated by design (same Boards API powers every embedded career-page widget), which is relevant to the hiQ-style "publicly accessible" analysis. The more relevant Greenhouse development is **Real Talent** (see §3) — a product response to fraud/spam/AI-applicant volume, not a ToS clause.

**Ashby** — no applicant-facing automation prohibition found; public materials are about Ashby's own AI recruiting features (screening, scheduling), not restrictions on candidates. Ashby states customer data isn't used to train models and PII is redacted before third-party LLM calls, but this doesn't bear on applicant automation risk.

**Simplify (Simplify Copilot)** — notable as a legitimate existing product: Simplify's autofill extension explicitly stops short of full autonomy — "the Copilot extension fills in form fields efficiently on major ATS platforms, but it does not submit applications without your action... you still need to manually click Submit on each application" (per Simplify's own help docs/product description). This is a deliberate design choice by an incumbent, mainstream tool in this exact space, and is strong evidence for where the industry consensus line sits: autofill yes, autonomous final submission optionally gated behind a human click.

### Distinguishing the three activities

1. **Scraping job listings** (reading public postings to build a database) — legally low-risk per hiQ v. LinkedIn (see below), but still a ToS breach on platforms like LinkedIn/Workday that explicitly prohibit scraping regardless of CFAA exposure. Contract law (breach of ToS) can apply even where the CFAA doesn't — companies can still send cease-and-desist letters, IP-ban, or (in theory) sue for breach of contract/trespass to chattels, though such suits are rare against individual, low-volume personal users.
2. **Automated form submission on your own behalf** — the ToS language above (Workday, LinkedIn) is broad enough to technically cover this, but the practical enforcement mechanism is technical (bot/fraud detection, CAPTCHA, rate limits, account restriction) rather than litigation. No public evidence was found of an ATS vendor suing an individual applicant for using an autofill tool to apply for real jobs with truthful information.
3. **Creating accounts programmatically** — this is the activity most consistently and explicitly prohibited across the board (LinkedIn's false-identity clause, Workday's account-consent language) and is also the activity most tightly coupled to CAPTCHA/bot-detection systems that will actively block it. Programmatic account creation is a materially higher-risk behavior than autofilling a form using an account the human already created themselves — treat these as different risk tiers.

### Enforcement track record

- **Account-level enforcement is real and reportedly at scale but automated, not litigious.** LinkedIn's own transparency reporting claims blocking "78.2 million fake accounts" and flagging "23.5 million automated sessions" in a single quarter (per secondary reporting on LinkedIn's March 2026 Transparency Report) — this is about fake-account/bot networks generally, not narrowly about job-application autofill, but signals LinkedIn treats automated session detection as a major, active enforcement program.
- **IP blocks / CAPTCHA walls** are the dominant real-world consequence for scraping/automation tooling hitting Greenhouse, Workday, and Lever — reporting on tools like LazyApply notes that sending "one static resume to every job... violates Greenhouse, Workday, and Lever submission patterns and triggers CAPTCHA blocks."
- **No verified case law or reporting found of an ATS vendor (Greenhouse, Lever, Workday, Ashby) suing an individual job applicant** for using an autofill/automation tool on their own truthful application. The known legal fights (hiQ v. LinkedIn) are about *third-party scraping for resale/analytics*, a different fact pattern from a personal applicant tool.
- **LinkedIn has litigated against scraping companies** (hiQ, and per secondary sources, other scraping-for-profit outfits), but again against commercial scrapers, not individual users applying to jobs.

### hiQ Labs v. LinkedIn (9th Cir., final ruling 2022)

- Holding: automated capture of data from **publicly accessible** web pages that don't require an account/login does not violate the CFAA's "without authorization" prong — because there are no "access permissions" to violate on a page open to the world by design.
- The court explicitly noted a ToS provision *alone* (a cease-and-desist letter, browsewrap language) cannot convert public access into "unauthorized access" under the CFAA.
- Important limiting fact: this is about **scraping public data**, not about submitting content (applications) into a private, authenticated system, and not about civil breach-of-contract claims (which remain available to LinkedIn regardless of the CFAA outcome — hiQ still had to deal with ToS/contract exposure even after winning on CFAA grounds).

### Van Buren v. United States (SCOTUS, 2021)

- Holding: a person does not "exceed authorized access" under the CFAA merely by using access they legitimately have for an improper *purpose*. The CFAA's "exceeds authorized access" clause is about accessing information/areas that are actually off-limits (a "gates-up-or-down" reading), not about violating a use policy while accessing something you're otherwise allowed to access.
- Direct relevance here: an applicant using their **own account**, with **valid login**, to submit a **truthful application** through an automated UI-filling tool is not "hacking" in the CFAA sense — there's no gate being bypassed. This narrows federal criminal/civil CFAA exposure substantially. It does **not**, however, immunize the user from a platform's private contractual remedies (account suspension, ToS-breach claims) — those remain purely contractual/civil, enforced by the platform itself rather than by CFAA prosecution.
- Net legal picture: **submitting your own truthful application via automation is very unlikely to create CFAA/criminal exposure post-Van Buren/hiQ**, but it can still violate a platform's ToS as a contract matter, with the real-world consequence being account/IP-level technical enforcement (bans, CAPTCHA walls, flags) rather than lawsuits.

---

## 2. Truthfulness Constraints — Fields That Must Never Be LLM-Inferred

### Categories found in application flows, with legal/consequence backing

- **Work authorization / visa sponsorship need** ("Are you legally authorized to work in the US?" / "Will you now or in the future require sponsorship?"). Governed in the US ultimately by Form I-9 obligations. Knowingly false attestation is a federal matter: an individual who knowingly makes a false statement on I-9 (or presents fraudulent supporting documents) is exposed to criminal penalties — reporting cites potential imprisonment (up to 5 years cited in secondary sources), criminal fines, and immigration consequences including deportation exposure, plus civil penalties under the Immigration Act of 1990 framework. This is the single highest-stakes field category — must always be entered by the human, never guessed/defaulted by an LLM based on resume content (e.g., inferring citizenship from a U.S. address or school).
- **EEO self-identification (race, gender/sex, veteran status, disability status)**. These are explicitly **voluntary** and are legally required to be **kept separate from the hiring decision and from the rest of the application record** — sample EEO self-ID forms state the data "will be used for EEO-1 reporting purposes only and will be kept separate from all other personnel records." An LLM must never infer or fill these from a resume/photo/name — that would (a) violate the voluntariness requirement in spirit and (b) risk introducing protected-class inference into a process that's supposed to firewall it off. This category should be either skipped entirely (leave blank, which is a legally protected non-answer) or explicitly, individually confirmed by the human each time — never carried over automatically from a stored "profile" without a fresh, deliberate opt-in, since answers can legitimately vary by employer/comfort level.
- **Criminal history** ("Have you ever been convicted of a felony?" etc.) — jurisdiction-dependent (many US states/cities have "ban the box" laws restricting when this can even be asked), answers have direct bearing on offer rescission and potential fraud liability; never inferable, always human-entered.
- **Prior employment / dates / reasons for leaving** — must match what the human can defend in a background check; an LLM extrapolating "reason for leaving" or exact dates from an unstructured resume risks introducing fabricated specifics that conflict with what the employer's background-check vendor finds.
- **Education verification fields** (degree conferral date, GPA, honors) — same logic; degree/date mismatches are one of the most commonly caught background-check discrepancies.
- **"I certify the above is true" / e-signature attestations** — these are the legal capstone on every other field. An LLM must never auto-check/auto-sign these; they must always require an explicit human action, both because (legally) the attestation is what creates fraud/misrepresentation exposure, and because a bot "signing" the applicant's attestation undercuts the very personal-representation theory that makes the tool legitimate in the first place (this is *your* application only if *you* attest to it).
- **Salary history / current compensation** — in many jurisdictions this question is itself illegal to ask, but where present, it's a fact only the human can supply accurately.
- **References contact info** — must be human-supplied and human-confirmed (the referenced person must have actually agreed to be a reference).

### Reporting on rescission for inaccurate answers

- Offer rescission for application/resume inaccuracies is a well-established employer practice: reporting on rescinded offers cites "providing false information about skills, licenses or work experience" as valid, common grounds for withdrawal, and failed/contradicted background checks as a leading rescission trigger.
- Lying on a job application can also expose the applicant to civil fraud liability if the employer can show damages resulting from reliance on the false statement (per legal-consumer reporting), separate from just losing the job.
- No large-scale, named "blacklist" registry akin to a credit bureau was found for general application dishonesty, but industry reporting does describe recruiters/agencies informally sharing "spam" or "bad actor" flags across their own ATS databases for mass-apply/bot behavior (see §3) — a related but distinct risk from truthfulness per se.

### Concrete never-infer field list (design conclusion)

The system must hard-code these as **human-required, LLM-write-blocked** fields (the LLM may at most surface the question to the user, never propose or pre-fill a default answer):
1. Work authorization status / need for visa sponsorship, now or future
2. Any EEO/voluntary self-identification field: race/ethnicity, sex/gender, veteran status, disability status
3. Criminal history / conviction disclosures
4. Prior employment dates, titles, reasons for leaving, eligibility for rehire
5. Education completion dates, degree conferral, GPA where used for a certified record (not just resume prose)
6. Salary history / current or desired compensation where legally requestable
7. Any "I certify/attest this is true and accurate" checkbox or e-signature
8. Reference names/contact information
9. Any government-ID-linked identity verification step (e.g., Greenhouse Real Talent / CLEAR-style flows)
10. Any question the ATS marks as legally sensitive/required-by-law (a generic catch-all: if a field is tagged in the ATS schema as EEO/compliance-related, route to mandatory human input regardless of whether it matches the above list)

---

## 3. Employer-Side Detection and Backlash

### Detection signals reported in 2024–2026 coverage

- **Textual AI-detection**: A cited 2026 TopResume survey (800+ hiring managers) reports 67% say they can identify AI-generated cover letters, 54% view them negatively; separately, 33.5% of hiring managers claim they can identify an AI-written application in under 20 seconds, and 19.6% report rejecting such candidates outright (TopResume, cited May 2025 figure). Common linguistic tells cited: generic openers ("I am writing to express my strong interest in..."), buzzwords ("detail-oriented professional," "proven track record," "leverage," "delve," "spearheading transformative initiatives"), and register mismatches (entry-level applicant using C-suite language). Recruiters reportedly treat automated-detector output as a soft signal for human review, not an auto-reject trigger, because detectors are unreliable — the real rejection driver remains genericness/irrelevance, not AI-authorship per se.
- **Velocity / pattern-based fraud detection**: reporting describes HR teams noticing "bursts of submissions within minutes," "recycled text blocks," and applications to "irrelevant roles" or the "same role multiple times" as classic bot signatures, independent of text-detector tools.
- **Greenhouse Real Talent** (launched with CLEAR partnership) is the most concrete, named ATS-vendor product response: it combines (a) AI-based fraud/bot/mass-application/impersonation pattern detection, (b) CLEAR-powered biometric/government-ID identity verification surfaced to recruiters as a simple "Verified" badge, and (c) AI talent-matching. Greenhouse frames this explicitly as combating "fake job applicants," "fraud and spam," and AI-generated application floods.
- **LinkedIn**: rolled out an "underqualified-applicant warning system" (reported August 2026) as its latest volume-management measure, after "prior limits on automated applications failed to stem volume growth." Separately, LinkedIn has built out an identity-verification badge system (government ID, workplace email, or institutional verification), live in 60+ countries as of April 2026, aimed at building trust signals — adjacent to, though not identical to, an anti-bot measure. LinkedIn also reports application-volume growth directly tied to generative AI adoption: submissions per applicant on LinkedIn are up 46% vs. February 2020 and up 22% since ChatGPT's late-2022 release (LinkedIn data cited via secondary reporting).
- **Recruiter/agency-level blacklisting**: reporting states that being flagged as a "spammer" (via bot behavior — irrelevant-role blasting, duplicate applications) in one ATS "can follow you" because "recruiters share data" across "major agency databases." This is informal/reputational rather than a single central registry, but is treated in industry commentary as a real and durable reputational cost.

### Reported effect on candidates who mass-apply

- Directionally negative and fairly consistently reported: mass-apply/generic applications correlate with materially lower interview conversion (see §4 numbers), plus the added downside risk of being tagged as spam/bot and facing extra friction (CAPTCHA walls, account flags, agency-level reputational flags) that a targeted, low-volume applicant doesn't encounter.
- Caveat: much of this reporting comes from vendors in the "quality over quantity" resume/career-coaching space (TopResume, Huntr-adjacent, various "AI job search" blogs) who have a commercial incentive to make this argument — treat the qualitative direction (mass, generic, fast-fire behavior looks bad and gets penalized) as well-supported, but treat precise percentages from single-vendor blog posts as commentary/marketing-adjacent rather than rigorously verified statistics.

---

## 4. Does Volume Even Work? Conversion Data

- **Baseline funnel**: CareerPlug's 2025 Recruiting Metrics Report (10M+ applications) found only ~3% of applicants reach an interview; broader commentary places average interview-conversion around 2–3%, with roughly 75% of applications filtered out by ATS screening before a human ever sees them.
- **Tech-specific**: reporting cites tech roles requiring roughly 191 applicants per hire — a materially harder funnel than average, consistent with the "flooded pipeline" narrative driving ATS vendors to build fraud/volume tooling.
- **New-grad market conditions (2025–2026)**: entry-level developer job postings reportedly down ~40% versus pre-2022 levels; NACE projects only a 1.6% increase in hiring for the Class of 2026; secondary reporting states 58% of recent graduates are still job-hunting post-graduation. This is a real headwind independent of any tooling choice — it raises the ceiling on how much a well-designed tool can compensate for a genuinely tight market.
- **Referral vs. cold application**: referred candidates are reportedly hired at ~30%, versus ~7% for all other application methods combined — a large, consistently cited gap that argues for the tool prioritizing warm paths (referrals, networking) over blind mass-apply where possible.
- **Targeted/customized vs. generic applications**: one dataset (Huntr, cited as Q2 2025, ~1.39M tracked applications) reports customized applications achieving a 5.75% interview rate vs. 2.68% for generic — a ~115% relative improvement for tailoring. This is the most directly relevant number for the "quality vs. volume" design question, though it comes from a single vendor's internal data rather than an independent academic study, so treat the direction as credible and the exact multiplier as vendor-reported.
- **Speed-to-apply / "apply within 24 hours"**: multiple sources converge on a real and fairly large early-mover effect:
  - A 2017 TalentWorks analysis (~1,600 applications) found applicants submitting within 4 days of a posting going live were up to 8x more likely to get an interview than those applying later; a commonly repeated companion claim (same lineage of research) states 90% of people who eventually get interviews applied within 24 hours of posting, and that ~72% of offers went to people who applied within the first 5 days.
  - More recent (2025-era) secondary reporting citing a "GoApply" study of 10,000+ job seekers reports applying within 24–48 hours yields 2–3x more interviews than waiting a week+; some blog restatements inflate this to "8x" by conflating it with the older TalentWorks figure — treat the "8x" figure as sourced to the original 2017 TalentWorks study specifically, and the "2-3x" as the more recent, independently-sourced claim; both point the same direction.
  - Mechanistic explanation given across sources: ATS applicant lists are typically reviewed in submission order, recruiters give the first 20–30 applicants the most attention/freshest scrutiny, and popular roles can accumulate 100–250+ applications within the first 24–48 hours, so later applicants are competing against a much larger, already-triaged pool.
  - **Caveat**: the underlying rigorous data point (TalentWorks 2017) is nearly a decade old and predates both modern ATS AI-ranking (which increasingly re-sorts by relevance/match score rather than pure timestamp) and the AI-driven application-volume surge described in §3 — so the effect direction (earlier is better) is credible, but the exact magnitude in 2025–2026's much higher-volume environment is less rigorously established and largely vendor-blog-sourced.

### Design implication on speed vs. quality

The data supports **both** being true and non-substitutable: speed-to-apply materially helps (get in before the queue fills and before recruiter fatigue sets in) AND generic/mass-produced content materially hurts (lower conversion, higher fraud-flag risk). This argues for optimizing for **fast, high-quality-per-application** submission — i.e., the system's value should be in helping the human get a genuinely tailored application out within hours of a posting going live, not in maximizing raw daily application count. Pure volume-maximization is against both the effectiveness data (§4) and the detection/backlash data (§3).

---

## 5. CAPTCHA, Anti-Bot Ethics, and Pacing

### Practical stance: no CAPTCHA-solving

- CAPTCHA-solving is explicitly the trigger point where "automating my own application" tips into "circumventing a security control," which is the one CFAA-adjacent act (Van Buren's "gates-up-or-down" framing) that plausibly *does* cross from contract-breach territory into something closer to unauthorized-access territory, and is also the most reliable way to get an account/IP banned outright. Reporting on existing tools confirms this is already a real failure mode: static/naive automation "triggers CAPTCHA blocks" on Greenhouse, Workday, and Lever.
- Existing legitimate tools model the right behavior: **Simplify Copilot autofills but requires the human to click Submit** — the product deliberately stops short of full autonomous submission, which both sidesteps a large share of anti-bot detection (a human-driven final click looks human) and keeps the human as the last line of truthfulness/attestation defense (ties back to §2's "certify this is true" requirement).
- Correct design pattern: detect a CAPTCHA/anti-bot challenge, **stop and hand control to the human** with a clear notification ("this site requires you to complete a verification step — please switch to the browser tab and finish, then resume"), never attempt to solve, defeat, or route around it programmatically (no CAPTCHA-solving services, no headless-browser fingerprint evasion).

### Rate limiting / pacing

- Concrete numbers exist in the wild for what "aggressive" looks like: one tool (LazyApply) markets up to 150/day on LinkedIn or Indeed and up to 750/day on other platforms — reporting frames this volume itself as a red flag ("HR teams see patterns no human would produce"), and the wider commentary explicitly ties "bursts of submissions within minutes" and "dozens of applications in minutes" to getting "labeled as noise" and flagged.
- Recommended posture from the ethics/practicality commentary: "rate-limited auto-apply with a quality-over-quantity focus improved response rates" — i.e., pacing isn't just about evading detection, it correlates with the same tailoring/quality behaviors that independently improve conversion (§4).
- Concrete pacing guardrail to adopt (synthesized, not a single-source stat): submissions spaced with human-realistic gaps (minutes, not seconds, between actions on any one site; a hard daily ceiling well under the ~150–750/day range that's flagged as abusive; no bursts within a single ATS session that would produce an obviously bot-like timestamp cluster).

### Why residential-proxy / fingerprint-spoofing is out of scope

- These techniques exist specifically to defeat the anti-bot/anti-fraud systems described in §3 (Greenhouse Real Talent's bot detection, LinkedIn's automated-session flagging, generic IP/device fingerprinting). For a **personal, truthful-application tool**, there is no legitimate reason to defeat identity/fraud detection — the entire premise of this tool's legitimacy is "I am a real, single human applying truthfully for myself," which is exactly what those systems are designed to confirm, not obstruct. Adopting evasion techniques would functionally convert the tool from "assistant for a legitimate applicant" into "tool for defeating anti-fraud controls," which is a materially different (and much higher-risk, both legally and ethically) posture, and is explicitly the kind of behavior current ATS-vendor tooling is being built to catch and penalize.

---

## 6. Design Implications — Concrete Guardrails

**A. Never-infer field list (hard block on LLM authorship)** — see §2's list of 10 categories. Implementation: these fields must be schema-tagged in the application-field mapper and routed to a mandatory human-input UI step; the LLM may draft *surrounding* prose (cover letter, "why this role") but must never write into, default, or silently carry forward a stored value into any field in this list without a fresh explicit human action each time.

**B. Mandatory human approval points**
1. Final review of the complete application package (resume variant, cover letter, all answers) before any submission — no "fire and forget" mode.
2. Explicit click-through on the "I certify this is true" attestation — never auto-checked.
3. Any EEO/voluntary self-ID field — presented fresh each time, never auto-filled from memory, with a visible "you may skip this" affordance preserved.
4. Any field the ATS schema flags as legally sensitive (work authorization, criminal history, compensation, prior-employer specifics).
5. CAPTCHA/anti-bot challenge encountered mid-flow — always pause and hand off, never attempt automated resolution.
6. First-time-per-employer submission (don't let repeat/duplicate submissions to the same company happen silently).

**C. Pacing limits**
- Hard per-platform daily cap set well below the ~150/day figure industry reporting already flags as abusive (e.g., a low double-digit ceiling), and mandatory randomized human-scale delays (minutes, not seconds) between actions within a single ATS session.
- No concurrent/parallel submission bursts to the same ATS provider.
- Configurable "cooldown" if the tool detects a challenge/rejection/error pattern that might indicate it's been flagged, rather than retrying immediately.

**D. Honesty invariants**
- Every generated sentence in a cover letter or "why us" answer must be traceable to an explicit fact in the user's stored profile/resume (a claims-to-source mapping the tool can show on request) — no LLM invention of experience, skills, or achievements not present in the source material.
- No LLM-side inference of protected-class status, citizenship/immigration status, or criminal history from indirect signals (name, photo, school, address, accent in transcript, etc.) for the purpose of pre-filling any field.
- Resume/cover-letter tailoring may rephrase and reorder truthful content but must not fabricate metrics, dates, titles, or responsibilities.

**E. Audit log requirements**
- Immutable, timestamped log of: what was submitted, to which employer/ATS, which fields were human-entered vs. tool-assisted, what the human explicitly approved, and any pacing/CAPTCHA events encountered.
- Log should be sufficient for the user to reconstruct, after the fact, exactly what any given employer received and when — both for the user's own defense (if a discrepancy is ever alleged) and for debugging the tool's behavior.

**F. Kill switch**
- A single, always-available control that immediately halts all pending/scheduled automated actions (no new form-fills, no new submissions) without needing to explain why — triggerable by the user at any time, and auto-triggered by the system itself if it detects repeated CAPTCHA challenges, repeated errors, or account-warning language from a platform (treat any explicit "we've detected unusual activity" message as an automatic hard-stop condition, not something to route around).

---

## Sources

- [Greenhouse | Legal – Sourcing automation addendum](https://www.greenhouse.com/sourcing-automation-addendum)
- [Greenhouse Jobs Scraper: ATS Job Listings API · Apify](https://apify.com/automation-lab/greenhouse-jobs-scraper)
- [Lever Terms of Service](https://www.lever.co/legal/terms-of-service)
- [Workday US End User Agreement](https://www.workday.com/en-us/legal/end-user-agreement.html)
- [Workday US Site Terms of Service](https://www.workday.com/en-us/legal/site-terms.html)
- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)
- [Prohibited software and extensions | LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a1341387)
- [Is LinkedIn Automation Safe in 2026? ToS & Scraping Rules](https://connectsafely.ai/articles/is-linkedin-automation-safe-tos-scraping-guide-2026)
- [Is Scraping LinkedIn Legal in 2026? (I Was Sued by LinkedIn)](https://nubela.co/blog/is-scraping-linkedin-legal-in-2026/)
- [Verification badge on job posts | LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a1492056)
- [Verifications on your LinkedIn profile | LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a1359065)
- [Simplify Copilot | Autofill Job Applications and Track Jobs](https://simplify.jobs/copilot)
- [Manage Autofill Settings in the Simplify Extension - Simplify](https://help.simplify.jobs/articles/8686025-manage-autofill-settings-in-the-simplify-extension)
- [Ninth Circuit Holds Data Scraping is Legal in hiQ v. LinkedIn - California Lawyers Association](https://calawyers.org/privacy-law/ninth-circuit-holds-data-scraping-is-legal-in-hiq-v-linkedin/)
- [HiQ Labs Scrapes by Again — Fenwick](https://www.fenwick.com/insights/publications/hiq-labs-scrapes-by-again-the-ninth-circuit-reaffirms-that-data-scraping-does-not-violate-the-cfaa-1)
- [HIQ LABS, INC. V. LINKEDIN CORPORATION, No. 17-16783 (9th Cir. 2022) :: Justia](https://law.justia.com/cases/federal/appellate-courts/ca9/17-16783/17-16783-2022-04-18.html)
- [Ninth Circuit Reaffirms Data Scraping from Public Websites Does Not Violate CFAA — Seyfarth Shaw](https://www.tradesecretslaw.com/2022/05/articles/computer-fraud-and-abuse-act/ninth-circuit-reaffirms-that-data-scraping-from-public-websites-does-not-violate-the-computer-fraud-and-abuse-act/)
- [Van Buren v. United States — Congress.gov CRS](https://www.congress.gov/crs-product/LSB10616)
- [US Supreme Court Narrows Scope of CFAA in Van Buren — Cooley](https://www.cooley.com/news/insight/2021/2021-06-09-us-supreme-court-computer-fraud-abuse-act-van-buren)
- [Supreme Court Ends Circuit Split over CFAA "Exceeds Authorized Access" — Proskauer](https://newmedialaw.proskauer.com/2021/06/06/supreme-court-ends-long-running-circuit-split-over-cfaa-exceeds-authorized-access-issue-adopting-a-narrow-interpretation-that-will-reverberate-in-scraping-disputes-and-litigation-ov/)
- [Van Buren v. United States — Wikipedia](https://en.wikipedia.org/wiki/Van_Buren_v._United_States)
- [Voluntary Self-Identification of Disability - United Site Services](https://www.unitedsiteservices.com/voluntary-self-identification-disability/)
- [EEO Voluntary Self-Identification form (Clark State)](https://www.clarkstate.edu/media/maho0tzog0ferfxl1kupwazqbu/eeo-self-id-form.pdf)
- [INS Memo on Misrepresentations on Form I-9](https://www.aila.org/ins-memo-on-misrepresentations-on-form-i-9)
- [Penalties | USCIS](https://www.uscis.gov/i-9-central/legal-requirements-and-enforcement/penalties)
- [Can You Fire Someone for Lying on their Job Application or Resume? — LegalMatch](https://www.legalmatch.com/law-library/article/lying-on-a-job-application-or-resume.html)
- [Penalties For Lying on a Job Application — Super Lawyers](https://www.superlawyers.com/resources/employment-law-employee/penalties-for-lying-on-a-job-application/)
- [What to Do If Your Job Offer Is Rescinded — Cornell Career Center](https://career.cornell.edu/blog/2025/04/02/what-to-do-if-your-job-offer-is-rescinded/)
- [Can Recruiters Detect AI Cover Letters? [2026]](https://coverlettercopilot.ai/blog/are-ai-cover-letters-detectable-by-recruiters)
- [How Recruiters Detect AI Cover Letters 2026 | Textora](https://www.textora.org/blog/how-recruiters-detect-ai-cover-letters)
- [Real Talent | Talent matching, verification and fraud detection software — Greenhouse](https://www.greenhouse.com/real-talent-candidate-matching)
- [Introducing Greenhouse Real Talent with CLEAR](https://www.greenhouse.com/blog/introducing-greenhouse-real-talent)
- [New AI Verification Tech Detects Fake Job Applicants | SUCCESS](https://www.success.com/ai-verification-fake-job-applicants/)
- [Greenhouse Real Talent™ Launches — Newsroom](https://www.greenhouse.com/newsroom/greenhouse-real-talent-tm-launches-to-fix-overwhelming-candidate-pipelines-while-combatting-fraud-and-spam-in-hiring)
- [Why AI Job Application Tools Hurt Your Job Search (2026 Data) - jobstrack.io](https://jobstrack.io/blog/ai-job-application-tools)
- [Recruiters Add Friction to Job Apps After AI Spam Floods Pipelines](https://www.skillfuel.com/recruiters-friction-job-applications-ai-hiring/)
- [Why Mass Apply Strategies Fail in 2026 — ResumeYourWay](https://www.resumeyourway.com/blogs/news/the-post-application-economy-why-mass-apply-strategies-are-failing-in-2026)
- [The AI hiring arms race is pitting bots against recruiters — eMarketer](https://www.emarketer.com/content/ai-hiring-arms-race-pitting-bots-against-recruiters)
- [Recruitment Funnel Benchmarks 2026: Conversion Rates by Stage - Pin](https://www.pin.com/blog/recruitment-funnel-benchmarks/)
- [Software Engineer Interview Statistics 2026 — Rockstar Developer University](https://rockstardeveloperuniversity.com/software-engineer-interview-statistics/)
- [Job Application Funnel Statistics for 2026 — onehour.digital](https://onehour.digital/blog/job-application-funnel-statistics)
- [Why 90% of Interviews Go to People Who Apply in the First 24 Hours](https://mypivot.substack.com/p/90-of-interviews-go-to-people-who)
- [The First-Mover Advantage: How to Apply Early to Tech Jobs in 2026 - jobstrack.io](https://jobstrack.io/blog/first-mover-advantage-applying-early-tech-jobs)
- [Applying Early Beats Applying Often, According To The Timing Data · Vinayak Kapoor](https://vinayakapoor.com/applying-early-beats-applying-often/)
- [Best Time to Apply for Jobs in 2025 (Data-Backed Guide) | GoApply](https://www.goapply.ai/blog/best-time-to-apply)
- [2025 Job Application Statistics — HiringThing](https://blog.hiringthing.com/2025-job-application-statistics-updated-data-you-need-to-know)
- [AI-Powered Job-Hunting Automation in 2025 — Gracker.ai](https://gracker.ai/blog/ai-job-apply-bots-2025)
- [LazyApply Review 2026 — Jobloo Blog](https://jobloo.co/blog/lazyapply-review-2026/)
- [The Real Problem with Automated Job Applications — scale.jobs](https://scale.jobs/blog/real-problem-automated-job-applications)
- [AI Auto-Apply for Jobs: 7 Red Flags HR Sees When Candidates Overuse Bots | Sprad Blog](https://sprad.io/blog/ai-auto-apply-for-jobs-7-red-flags-hr-sees-when-candidates-overuse-bots)
- [Is Automated Job Application Ethical? A Deep Dive - Resumly](https://www.resumly.ai/blog/is-automated-job-application-ethical)
- [Job Application Automation: Is it Safe and Ethical? — FastApply](https://blog.fastapply.co/job-application-automation-is-it-safe-and-ethical)
