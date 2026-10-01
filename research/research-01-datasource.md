# Data Source Research: SimplifyJobs New-Grad-Positions / Summer2026-Internships

Verified by downloading and parsing the actual `listings.json` files (13.7MB, 20,132 records for
New-Grad-Positions; 12.5MB, 16,686 records for Summer2026-Internships) on 2026-09-12, plus fetching
the repos' Python build scripts and README source via raw.githubusercontent.com and the GitHub REST API.

## 1. Machine-readable format: exact schema

File: `.github/scripts/listings.json` in both repos (default branch `dev`), e.g.
https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json

It's a **flat JSON array of objects**, no wrapper, no pagination. New-Grad-Positions schema (14 keys,
100% present in all 20,132 records):

```json
{
  "source": "Simplify",
  "category": "AI/ML/Data",
  "company_name": "Johnson & Johnson",
  "id": "792fa1b4-a570-49ed-9e6a-36007b97f5fc",
  "title": "Postdoctoral researcher Omics",
  "active": false,
  "date_updated": 1768478903,
  "date_posted": 1768478903,
  "url": "https://jj.wd5.myworkdayjobs.com/JJ/job/Beerse-Antwerp-Belgium/Postdoctoral-researcher-Omics--R-D-Data-Science---Digital-Health_R-052122",
  "locations": ["Ambler, PA", "Cambridge, MA"],
  "company_url": "https://simplify.jobs/c/Johnson-Johnson",
  "is_visible": true,
  "sponsorship": "Other",
  "degrees": ["PhD"]
}
```

Field types:
- `source` (string): `"Simplify"` (99.5%, official Simplify-curated feed) or a GitHub username (community
  contributor via issue, e.g. `AlmondCroffle`, `Devonav`, etc.)
- `category` (string enum, see below)
- `company_name` (string)
- `id` (string, UUID v4) — **globally unique**, verified 20,132/20,132 unique. A re-posted job gets a
  brand-new `id`, it does not reuse the old one.
- `title` (string, free text)
- `active` (bool)
- `date_updated` (int, Unix epoch seconds)
- `date_posted` (int, Unix epoch seconds)
- `url` (string, URL) — see Q2
- `locations` (array of free-text strings, e.g. `"Remote in Canada"`, `"SF"`, `"Ambler, PA"`) — not
  normalized/geocoded, no separate country field
- `company_url` (string) — `https://simplify.jobs/c/<Slug-Or-UUID>` (Simplify's own company page, not
  an apply link)
- `is_visible` (bool)
- `sponsorship` (string enum, see Q6)
- `degrees` (array of string enum, possibly empty `[]`, see Q6)

The **Summer2026-Internships** repo (same script family) has one schema difference: an added
`"terms"` field (array of strings, e.g. `["Summer 2026", "Fall 2026"]`), values seen: `Summer 2026`,
`Fall 2026`, `Summer 2027`, `Winter 2026`, `Spring 2026`, `Winter 2027`, `Spring 2027`, `N/A`, and
combinations. There is **no field literally named `season`** in Simplify's own repos (that name is used
by the unaffiliated `vanshb03` fork instead — see Q7). `checkSchema()` in `util.py` only hard-requires:
`source, company_name, id, title, active, date_updated, is_visible, date_posted, url, locations,
company_url, sponsorship` (degrees/category/terms are not schema-enforced, i.e. treat them as optional
when parsing defensively).

`category` enum values observed (New-Grad, with counts out of 20,132): `AI/ML/Data` (6025), `Software`
(5759), `Hardware` (4947), `Quant` (2230), `Product` (1050), plus legacy/inconsistent values from an
older taxonomy still present in old rows: `Software Engineering` (103), `Data Science, AI & Machine
Learning` (17), `Product Management` (1). Treat category matching as case/wording-fuzzy, not a clean enum.

## 2. Behavior of the `url` field

`url` is **the direct employer ATS application link in the overwhelming majority of cases** —
verified: only **3 of 20,132** New-Grad records (0.015%) have a `simplify.jobs` domain in `url`
(all for very early-stage/"founding engineer" startups like "Absurd" and "Vigil Labs" where Simplify
itself appears to host the application, i.e. there's no external ATS to link to). Everything else is a
raw ATS URL: `*.myworkdayjobs.com`, `job-boards.greenhouse.io`, `jobs.lever.co`,
`jobs.ashbyhq.com`, `jobs.smartrecruiters.com`, `*.oraclecloud.com`, `*.icims.com`, etc.

`company_url` (separate field) is always a Simplify company page (`simplify.jobs/c/...`), **never**
an apply link — don't confuse the two fields.

The README-rendering script (`util.py::getLink()`) appends UTM tracking params to the raw `url` for
the rendered markdown table (`?utm_source=Simplify&ref=Simplify` or `&utm_source=Simplify&ref=Simplify`
if a `?` already exists), and additionally renders a *secondary* Simplify quick-apply button
(`https://simplify.jobs/p/{id}?utm_source=GHList`) next to the ATS button when `source == "Simplify"`.
That secondary button is a README-only construct — **it does not exist in the JSON**, and the JSON's
`url` field is already the raw employer link with no redirect hop needed. Practical takeaway: **read
`url` straight from the JSON; optionally strip any `?utm_source=...` query params you add yourself** —
no need to follow any Simplify redirect to get the "real" apply URL.

## 3. Update cadence and README generation

Commit history (via `GET /repos/SimplifyJobs/New-Grad-Positions/commits`) shows a very regular,
automated **~30-minute cycle**, alternating two commit types:
1. `"Updating listings.json with postings from Simplify..."` — committed by a human/bot GitHub account
   (seen as "Jimothy Halpert" / "BigTunaHalpert"), i.e. Simplify's private backend pushes fresh scraped
   data into the JSON roughly every 30 min.
2. `"Updating README at <Month DD, YYYY HH:MM:SS>"` — committed by `github-actions[bot]` immediately
   after, generated by `.github/scripts/update_readmes.py`.

`update_readmes.py` pipeline: `getListingsFromJSON()` → `checkSchema()` → `filterListings(listings,
earliest_date=1748761200)` (hardcoded epoch cutoff, currently ~June 2025 — old rows are dropped from the
README but stay in listings.json) → `sortListings()` (active first, then date_posted desc, then company
name) → `embedTable()` which builds per-category HTML tables (active section + collapsed inactive
section), with a legend: 🛂 = no sponsorship, 🇺🇸 = US citizenship required, 🔒 = application closed
(non-active), 🔥 = FAANG+ company (hardcoded `FAANG_PLUS` set of ~50 companies), 🎓 = advanced degree
required. **Conclusion: the README is a filtered, re-sorted, decorated *view* of listings.json — the
JSON is the source of truth and is more complete (includes rows older than the README's cutoff and
rows filtered out by `filterListings()`'s title-keyword matching).**

`filterListings()` (used only for the README, not for the JSON itself) additionally drops rows unless
`is_visible` is true, `date_posted > earliest_date`, `company_url` isn't in a small `BLOCKED_COMPANIES`
set (currently just one blocked company, "Jerry"), and — for `source == "Simplify"` rows only — the
`title` matches a curated inclusion-term list (software/data/quant/product keywords) AND a "new grad"
term list. Community-contributed rows (`source != "Simplify"`) skip the title-keyword filter entirely.

Data freshness in the JSON itself: `date_updated` values range from 2026-01-15 to 2026-09-12 (today);
76 records were updated in the last 24h and 378 in the last 7 days (relative to the newest timestamp) —
consistent with the ~30-min commit cadence continuously touching a rolling subset of records.

## 4. Best polling strategy for detecting new postings

Recommended layered approach:

1. **Cheap change-detection**: poll the GitHub commits Atom feed or REST commits endpoint every
   5–15 min (matches the ~30 min upstream cadence with margin):
   - Atom feed (no auth, no rate-limit quota tied to API tokens): `https://github.com/SimplifyJobs/New-Grad-Positions/commits/dev.atom`
   - REST: `GET https://api.github.com/repos/SimplifyJobs/New-Grad-Positions/commits?path=.github/scripts/listings.json&per_page=5`
   Look for a new commit whose message starts with "Updating listings.json...".
2. **Conditional fetch of the real data** only when step 1 indicates a change:
   `GET https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json`
   with `If-None-Match: <last ETag>` (raw.githubusercontent.com is Fastly-fronted and returns
   `ETag`/`Last-Modified`; a 304 avoids re-downloading the full 13.7MB blob). Note: raw.githubusercontent.com
   rate limits are **undocumented/unpublished** (community reports of 429s under heavy polling, e.g.
   github/community discussion #157887, and an opencode issue about a client hammering raw.githubusercontent
   every 10 min) — poll conservatively (no tighter than every 5–10 min) and always send `If-None-Match`.
3. **Diff by `id`**: because a repost gets a brand-new UUID (verified — ids never repeat, and closed jobs
   don't disappear, they flip `active:false` and stay in the array), the correct diff algorithm is:
   keep a local set of previously-seen `id`s; any `id` not seen before with `active:true` is a genuinely
   new posting; any previously-active `id` now `active:false` is a closure event.

Rate limits: GitHub REST API unauthenticated = **60 requests/hour per IP**; with a PAT (or GitHub App
token) = **5,000 requests/hour**. A conditional request that returns 304 is **free (doesn't decrement
the quota) only when authenticated** — an unauthenticated 304 still consumes one request against the
60/hour budget (confirmed via GitHub docs + community reports, e.g. dev.to "an unauthenticated 304
still costs you a request"). So: use a PAT for the commits-API polling leg to get 5,000/hr and free 304s;
raw.githubusercontent.com fetches aren't governed by the api.github.com quota at all (separate CDN host).

**Webhooks are not usable** for a third party polling someone else's public repo — GitHub does not let
non-collaborators register webhooks on a repo they don't own/admin, and SimplifyJobs doesn't offer one
publicly. **git clone + diff** works but has no real advantage over the raw-file + ETag approach here
(you'd still be pulling the whole ~13.7MB blob whenever it changes, plus git protocol overhead) — not
recommended as primary strategy, conditional HTTP GET is simpler and cheaper.

## 5. How closed/inactive roles are marked; avoiding dead listings

Two independent mechanisms flip `active` to `false`:

1. **Automatic staleness heuristic** (`util.py::mark_stale_listings()`): for any listing where
   `source != "Simplify"` (i.e., community-submitted only), if `now - date_posted > 4 months`
   (`INACTIVE_THRESHOLD_MONTHS = 4`), it's force-marked `active = False`. This is a blunt age cutoff,
   not a real dead-link check.
2. **Manual crowd-sourced marking** (`.github/scripts/bulk_mark_inactive.py`): triggered from a labeled
   GitHub issue where a contributor pastes URLs they believe are closed; the script matches by `url`,
   sets `active = False` and bumps `date_updated`, and does **no HTTP verification at all** — no request
   is made to the ATS to confirm the posting is actually gone. It's purely trust-based crowd input.
3. **Not visible in the public repo**: the majority of `source == "Simplify"` rows that are already
   `active:false` (most of the 17,136/20,132 inactive rows) were presumably closed by Simplify's own
   proprietary backend/crawler before ever reaching the 4-month age threshold — that detection logic is
   NOT in this public repo (Simplify's crawler is closed-source); only the resulting `active` flag is
   synced in. **Unverified**: exact latency between a real-world closure and `active` flipping to false
   for Simplify-sourced rows.

Practical guidance to avoid applying to dead listings: filter on `active == true AND is_visible == true`
as a first pass, but treat this as necessary-not-sufficient — since neither mechanism is a live check,
build a defense-in-depth layer that does a live HEAD/GET (or a lightweight ATS-specific "still open" API
call, see Q8) against the `url` before auto-submitting, since most ATS platforms (Greenhouse, Lever,
Ashby, Workday) return a 404, redirect, or a "this position is no longer accepting applications" page
when truly closed.

## 6. Fields for filtering to "new grad FTE, US, sponsorship-OK, SWE"

- **Repo choice already encodes FTE vs. internship**: use `New-Grad-Positions` for FTE (no `terms`
  field at all); `Summer2026-Internships` for internships (has `terms`).
- **Role type**: `category` — filter to `"Software"` (and legacy `"Software Engineering"`) for SWE;
  `AI/ML/Data`, `Hardware`, `Quant`, `Product` for adjacent tracks. This is loosely/inconsistently
  applied (see legacy category values above) — supplement with title keyword matching (the repo's own
  `inclusion_terms` list in `filterListings()` is a good starting keyword set: "software eng", "software
  dev", "frontend", "backend", "full-stack", "founding engineer", "sre", etc.).
- **US location**: `locations` is free-text, unnormalized (`"SF"`, `"Remote in Canada"`,
  `"Ambler, PA"`, `"Laurel, MD"`) — there is **no dedicated country/is-US field**. You must
  pattern-match: two-letter US state abbreviations, "Remote in US"/"Remote in United States" style
  strings, and known US city names; explicitly exclude "Remote in Canada"/other-country patterns.
- **Sponsorship**: `sponsorship` field, **exact enum values found** (New-Grad, n=20,132):
  - `"Other"` — 20,032 (99.5%) — no explicit sponsorship stance stated (ambiguous, don't treat as
    either "offers" or "blocks")
  - `"U.S. Citizenship is Required"` — 67 (effectively blocks all non-citizens, stricter than "no
    sponsorship")
  - `"Does Not Offer Sponsorship"` — 18
  - `"Offers Sponsorship"` — 15
  To implement "sponsorship not blocked": exclude `"Does Not Offer Sponsorship"` and
  `"U.S. Citizenship is Required"`; keep `"Other"` and `"Offers Sponsorship"`. Because 99.5% of rows are
  `"Other"` (unlabeled), this field alone is weak signal — most listings simply don't state a
  sponsorship policy here.
- **Degrees enum** (`degrees` array, can be empty `[]` = unspecified/any), values found:
  `Bachelor's` (12,380), `Master's` (4,221), `PhD` (1,926), `Associate's` (1,153), `Certificate` (369),
  `MBA` (211), `Bootcamp` (65), `MD` (41), `JD` (40), `Incomplete` (14), `PharmD` (11), `DO` (3),
  `DDS` (3), `DVM` (3). For "new grad" filtering, keep rows where `degrees` is empty or contains
  `Bachelor's`/`Master's` (exclude PhD-only/MD/JD/PharmD/DO/DDS/DVM-only rows as targeting different
  populations).
- **Liveness**: `active == true AND is_visible == true` (20 of 20,132 rows have `is_visible:false` —
  rare, likely retracted/spam listings hidden without deletion).

## 7. Alternative / complementary aggregators

| Source | Format | Public JSON? | Notes |
|---|---|---|---|
| **SimplifyJobs/New-Grad-Positions** & **Summer2026-Internships** | `.github/scripts/listings.json` flat array | Yes, verified | Primary target of this research; ~30 min cadence; Simplify's own proprietary backend feed + community issues |
| **speedyapply/2026-SWE-College-Jobs** (and sibling `2027-SWE-College-Jobs`) | Markdown files only: `NEW_GRAD_USA.md`, `NEW_GRAD_INTL.md`, `INTERN_INTL.md` (HTML tables inside Markdown) | **No JSON found** — repo structure confirmed to contain only `.md` files + `.github/`, no `listings.json` equivalent visible. Updated daily per README. Backed by the SpeedyApply browser extension/company. To consume programmatically you'd have to scrape/parse the Markdown HTML tables. **Unverified**: whether they have a private API powering their own extension. |
| **vanshb03/Summer2026-Internships** (and `Summer2027-Internships`) | Same script family, `.github/scripts/listings.json` flat array — **verified by direct fetch**, 471 records | Yes, verified (`https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json`) | Independent from Simplify — no `source:"Simplify"` rows at all (`source` values are GitHub usernames: `vanshb03` 437/471, `aprameyak` 25, etc.), purely crowd-sourced via GitHub issues, much smaller corpus. **Schema differs slightly**: uses a singular `"season"` string field (e.g. `"Winter"`) instead of Simplify's `"terms"` array, and has no `degrees` or `category` field. Treat as a same-shape-but-not-identical-schema source; don't assume field parity across forks. |
| **Simplify's own consumer API** (`simplify.jobs`) | Unknown/private | **Not found** — web search turned up only Simplify's *unrelated* enterprise HR product at `help.simplify.hr` (a different "Simplify" brand for recruiting SaaS, with a documented REST API at `help.simplify.hr/reference` and an `llms.txt`), which is **not** the job-board consumer product. No public API docs found for `simplify.jobs` job search/autofill product. **Unverified** whether one exists privately for their browser extension. |
| **Hiring Cafe** (hiringcafe.com) | Web app; **no first-party public API** found | No official API; several third-party Apify scrapers exist (e.g. "Hiring.Cafe Scraper — 46 ATS Platforms") that resell scraped access as JSON/CSV via Apify, implying Hiring Cafe itself aggregates ~2.8M listings from Greenhouse/Lever/Workable/Workday/BambooHR/etc. **Unverified**: any official/free API; would need Apify (paid) or your own scraper. |
| **jobright.ai** | Web app | Search turned up no results specific to jobright — **could not verify** any public API. Treat as unresearched. |
| **Levels.fyi** | Web app, primarily comp data | Not evaluated in depth (out of scope focus — it's a comp/leveling database, not primarily a live job-postings feed); **unverified** whether it has a jobs API at all. |

Net: for *programmatic, low-effort, high-freshness* data, the two SimplifyJobs repos + vanshb03's repo
are the only **verified, genuinely public JSON** sources found. speedyapply requires Markdown scraping.
Hiring Cafe/jobright would require paid third-party scrapers or your own scraping — not verified as
having free public JSON APIs.

## 8. ATS domain frequency (New-Grad-Positions, n=20,132 total / 2,996 currently `active`)

Computed by parsing every `url`'s hostname and bucketing into ATS families:

| ATS family | All-time count (% of 20,132) | Active-only count (% of 2,996) |
|---|---|---|
| Workday (`*.myworkdayjobs.com` / `myworkdaysite.com`) | 9,153 (45.5%) | 821 (27.4%) |
| Oracle Cloud / Taleo (`*.oraclecloud.com`) | 2,602 (12.9%) | 166 (5.5%) |
| Greenhouse (`job-boards.greenhouse.io`, `boards.greenhouse.io`, EU variant) | 1,581 (7.9%) | 331 (11.0%) |
| SmartRecruiters (`jobs.smartrecruiters.com`) | 990 (4.9%) | 194 (6.5%) |
| iCIMS (`*.icims.com`) | 862 (4.3%) | 121 (4.0%) |
| Ashby (`jobs.ashbyhq.com`) | 798 (4.0%) | 211 (7.0%) |
| Lever (`jobs.lever.co`) | 542 (2.7%) | 192 (6.4%) |
| Workable (`apply.workable.com`) | 197 (1.0%) | 71 (2.4%) |
| Rippling ATS (`ats.rippling.com`) | 149 (0.7%) | 22 (0.7%) |
| Eightfold (`*.eightfold.ai`) | 109 (0.5%) | 27 (0.9%) |
| BambooHR | 67 (0.3%) | 21 (0.7%) |
| Jobvite | 53 (0.3%) | small |
| Everything else (company-specific custom career sites: `jobs.apple.com`, `amazon.jobs`,
  `lifeattiktok.com`, `jobs.bytedance.com`, `careers.jhuapl.edu`, `jobs.l3harris.com`,
  `www.tesla.com`, `careers.garmin.com`, etc.) | remainder, highly long-tailed — **2,120 unique hostnames total** | remainder |

Key takeaway for adapter prioritization: **Workday dominates the all-time archive (45.5%) but its share
of *currently active* postings drops to 27.4%**, while Greenhouse/Ashby/Lever/SmartRecruiters together
make up ~31% of active listings and each has a far more standardized, easier-to-integrate public JSON
API than Workday (which requires per-tenant, semi-documented CXS endpoints that vary by company). Big
mega-employers (Amazon, Apple, ByteDance/TikTok, Tesla, Boeing, L3Harris, Nvidia, JHUAPL, etc.) run
fully custom in-house career sites, which explains the long 2,120-unique-hostname tail — these need
one-off scrapers, not a generic ATS adapter. **Recommended adapter build order by ROI**: Greenhouse →
Lever → Ashby → SmartRecruiters → Workable (all have well-known/reverse-engineered public job-board
JSON APIs) → Workday (highest volume but per-tenant CXS API, more engineering cost) → iCIMS/Oracle
Cloud/Taleo (lowest ROI, more bespoke, declining share of active postings) → custom big-tech career
sites as one-offs only if those specific employers matter to the user.

## Unverified / lower-confidence items (flagged explicitly)
- Exact latency between a real-world job closure and Simplify's backend flipping `active:false` for
  `source:"Simplify"` rows — Simplify's crawler logic is private, not in this repo.
- raw.githubusercontent.com's exact undocumented rate-limit thresholds (only anecdotal 429 reports found).
- Whether Simplify's consumer job-board product (simplify.jobs) has any public/private API beyond what
  powers their own browser extension — not found; do not confuse with the unrelated `help.simplify.hr`
  enterprise HR API which is a different product.
- speedyapply's internal data pipeline (whether they maintain a private JSON that just isn't published)
  — only the public Markdown files were confirmed.
- jobright.ai and Levels.fyi API existence — not meaningfully researched, flagged as open questions.
