# Agent Architecture Patterns for an Autonomous Job-Application Browser Agent (Windows 11)

Research date: 2026-09-12. Sources cited inline; unmarked technical detail (SQLite schema design, Playwright trace-viewer mechanics, retry/backoff theory) is well-established engineering practice synthesized from documentation and standard patterns rather than a single citation.

---

## 1. Orchestration topology

**Anthropic's core framework** ([Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)):
- **Workflows** = predefined code paths orchestrating LLM + tool calls; you own control flow. Predictable, testable, cheap.
- **Agents** = the model dynamically directs its own tool use and control flow; you own the goal/guardrails, not the branches. Flexible but expensive, harder to test, error-compounding over long horizons.
- Explicit recommendation: find the simplest architecture that works; only add agentic autonomy where the task's complexity truly requires open-ended, unpredictable tool sequences. "Most production LLM systems are workflows, not agents."
- Named workflow patterns worth mapping onto the pipeline: **prompt chaining**, **routing**, **parallelization**, **orchestrator-workers**, **evaluator-optimizer**, and the plain **augmented LLM** (single call with tools/retrieval). Human oversight matters most for verifying outcomes align with intent, not micromanaging every tool call — agents still need "extensive testing in sandboxed environments" and guardrails for consequential actions.

**Applied to poll -> filter -> dedupe -> route -> fill -> review -> submit -> log:**

| Pipeline stage | Pattern | Why |
|---|---|---|
| Poll source -> filter -> dedupe | Plain code, no LLM | Fully deterministic, cheap, no judgment needed |
| Route by ATS (Workday/Greenhouse/Lever/iCIMS/...) | **Routing** | Classify into a known, small category set from URL/DOM fingerprint; LLM classification only as fallback when the rules-based fingerprint fails |
| Fill standard/structured fields | Deterministic code (profile -> field mapping) | No judgment once the field is identified |
| Free-text/essay questions, cover-letter tailoring | **Augmented LLM call**, or **evaluator-optimizer** for prose that matters (one call drafts, a second critiques against a rubric — tone, length, factuality — before accepting) | Judgment-requiring; iterative refinement pays off specifically for cover letters |
| Unrecognized/broken form, unmapped field | **Orchestrator-workers** escape hatch: a bounded, tool-restricted sub-agent (accessibility-tree snapshot in, structured decision out, hard turn/time cap) | Genuinely unpredictable, but scoped — never a free-running loop |
| Review before submit | Human checkpoint (not an Anthropic "pattern" per se, but explicit in their guardrail guidance) | Consequential, irreversible action |
| Log / ledger write | Plain code | |

Parallelization also applies operationally: ATS-detection/dedupe and background pre-classification of screening questions can run concurrently while a page loads; independent applications can run in parallel browser contexts.

**Real-world evidence this wins for form-filling specifically:**

- **Postmortem — "I Built an AI Agent That Hunts Jobs Autonomously"** ([dev.to/tushar_sangwan](https://dev.to/tushar_sangwan_25f0bd5499/i-built-an-ai-agent-that-hunts-jobs-autonomously-heres-what-actually-worked-2hem)): built with a decoupled architecture (API + separate browser-automation service + BullMQ job queue, deterministic weighted scoring, LLM only at the "deep scoring" step) rather than one agentic loop. Direct quote: **"LLMs are optimistic. Without hard constraints, they find reasons to match rather than reasons to reject."** Structured deterministic scoring cut false positives ~60% versus letting the LLM freely judge fit. Other lessons: unbounded LLM-written memory grew out of control (hard-capped at 50-100 entries), synchronous LLM calls blocked request handlers ("the hardest part was not the AI, it was the plumbing" — moved to an async queue), and giving the LLM control over presentation let hallucinated narrative leak into rendered output.
- **"Deterministic vs Agentic"** ([dev.to/waveassist](https://dev.to/waveassist/deterministic-vs-agentic-the-quiet-architectural-bet-every-ai-agent-company-is-making-33p)): thin-harness (intelligence at build time, code runs indefinitely) vs. fat-harness (model replans every step). Cites top coding agents at ~70% on SWE-bench Verified dropping to **~23% Pass@1 on SWE-Bench Pro** for longer/multi-step tasks, with commercial agentic apps often under 20% end-to-end success. "If you can describe a task as a stable set of steps with measurable outputs, the agent will often make it worse — more expensive, harder to debug, riskier to deploy." Deterministic wins for scoped, repeated, triggered work — exactly this domain.
- **"Blueprint First, Model Second"** ([arXiv:2508.02721](https://arxiv.org/pdf/2508.02721)) — design the deterministic workflow blueprint first, insert LLM calls only at scoped decision points with type-safe I/O contracts; confining LLM calls this way cuts hallucination-driven errors and makes cost predictable.
- **"Agentic Compilation"** ([arXiv:2604.09718](https://arxiv.org/pdf/2604.09718)) and **"Progressive Crystallization"** ([arXiv:2607.07052](https://arxiv.org/pdf/2607.07052)) — let an agent explore a *new* site/flow once, then "crystallize" the successful trace into a deterministic replayable script (selectors/refs + branch conditions), falling back to full agentic reasoning only on structural drift. Directly applicable per-ATS: Workday/Greenhouse/Lever/iCIMS/SuccessFactors are a small, closed set of templates — record a deterministic filler per platform once, invoke the LLM only when the page doesn't match the known template or a free-text answer is needed.
- A CUA-style pure vision/screenshot agent reportedly needs ~3 minutes per form and still doesn't reliably complete it — evidence that pure agentic screenshot-loop control is too slow/unreliable for this domain.

**Recommendation:** Build **(c) hybrid — deterministic skeleton + LLM escape hatch**, converging with (a) in practice. The skeleton is a state machine (see §4) calling per-ATS deterministic "fillers" driven by a profile-to-selector mapping. Drop into a scoped, turn-capped LLM subroutine only for: free-text question answering, matching an unmapped field label to the profile schema, and recovering when a selector isn't found. Never let stage transitions (fill -> review -> submit) be LLM-decided — always human- or rule-gated. This keeps ~90% of a run token-free and deterministic, avoids the "LLM is optimistic, submits things it shouldn't" failure mode, and avoids the ~20-25% completion ceiling seen in long-horizon fully agentic automation.

---

## 2. Claude Agent SDK / Claude Code as the runtime

**Four surfaces, per the `claude-api` skill's framing:** (1) Client SDK / raw Messages API with your own tool loop, (2) Client SDK's Tool Runner (`client.beta.messages.tool_runner`) — SDK loops over tools *you* define, no built-in tools, (3) **Claude Agent SDK** (`claude-agent-sdk` / `@anthropic-ai/claude-agent-sdk`) — "Claude Code as a library": full harness (agent loop, context management, built-in tools, hooks, subagents, permissions, sessions) that you host, and (4) **Managed Agents** — Anthropic hosts both the loop *and* a per-session sandbox. Managed Agents is disqualified here: the agent must drive the user's own already-authenticated Chrome on their own Windows box to reuse LinkedIn/Workday/Greenhouse logins, which requires **local** execution — a cloud sandbox has no access to that.

**What the Agent SDK offers concretely** ([code.claude.com/docs/en/agent-sdk](https://code.claude.com/docs/en/agent-sdk)):
- **Hooks**: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStart/Stop`, `PreCompact`, `UserPromptSubmit`, `Notification`, `PermissionRequest`. Hooks run **first** in the permission-evaluation order — before deny rules, permission mode, and allow rules — so a `PreToolUse` hook can hard-block the "click Submit" tool call even in `bypassPermissions` mode. This is the concrete mechanism for "never submit without approval," independent of whatever the model decides. Also the natural place to log every action unconditionally.
- **Permission modes**: `default` (asks via `canUseTool` callback), `dontAsk`/`plan`-adjacent modes that deny anything requiring a prompt (good for headless runs that shouldn't hang), `acceptEdits` (auto-approve file/fs ops), `bypassPermissions` (approve nearly everything — dangerous), `plan` (explores/plans, never auto-approves writes — maps directly onto a "fill but don't submit" dry-run mode), and an `auto` mode where a model classifier approves/denies. Full evaluation order: hooks -> deny rules -> ask rules -> permission mode -> allow rules -> `canUseTool` callback.
- **Subagents**: scoped agents with their own system prompt/tools/permission mode (e.g., a profile-only "question-answerer" subagent, a Playwright-MCP-only "form-filler" subagent) — keeps each node's context small and cache-friendly. A subagent never silently inherits `bypassPermissions` unless the parent itself is in that mode.
- **MCP**: connect the Playwright MCP server (§3) as the tool surface, scoped per-subagent so tool schemas don't bloat the main context.
- **Sessions**: transcripts auto-persist to `~/.claude/projects/<encoded-cwd>/*.jsonl`. Three continuation modes: `continue` (most recent session, no ID tracking — fits a single always-on daemon), `resume` with a captured `session_id` (needed for concurrency — one session per job application so a stalled one resumes exactly where it left off), and `fork` (branch history without mutating the original — useful for "try a different answer to this ambiguous question without discarding filled-so-far state"). Sessions persist *conversation*, not filesystem/browser state — DOM/cookie/form-state snapshotting is a separate concern (your own ledger, §4).
- Skills/commands/memory/plugins load automatically from `.claude/` — a job-hunting-buddy skill (answer-bank rules, ATS routing rules) is picked up with no extra wiring.

**Is "skill + hooks + cron" real for a recurring autonomous task?** Yes, several documented options, but all require **local** execution here:
- **Headless CLI + OS scheduler**: `claude -p "<prompt>" --output-format json` runs one-shot, clean stdin/stdout — the documented target for cron/systemd timers/**Windows Task Scheduler**.
- Claude Code's own `/loop` (session-scoped recurring execution at an interval) and `/schedule` (cron-based **cloud** routines) skills exist, but `/schedule`'s cloud routines run in Anthropic's cloud sandbox — not applicable here for the same local-browser-access reason Managed Agents is disqualified.
- GitHub Actions cron has the same disqualifying limitation (no access to the user's local logged-in browser).

**Trade-offs — skill+hooks+headless-CLI vs. standalone Python/TS+Messages API vs. embedded Agent SDK:**

| Need | `claude -p` + Task Scheduler | Standalone Python/TS + raw Messages API | Agent SDK embedded in a standalone process |
|---|---|---|---|
| Ledger/state | External — still hand-write your own SQLite ledger, skill reads/writes via Bash/file tools (extra indirection) | You own everything directly — cleanest | Same as CLI route for the ledger itself; SDK only persists conversation |
| Scheduled polling | Native fit (one-shot process per invocation) | You still need Task Scheduler/cron to invoke your script | Same — SDK doesn't replace the OS scheduler |
| Windows Playwright automation | Shells out via Bash tool or a custom MCP server — indirection | Direct library call, easiest debugging | Direct too, if Playwright is wrapped as an MCP/custom tool |
| Long unattended runs / cache warmth | Fresh short-lived process per run — good crash-blast-radius, but re-pays system-prompt/profile cost each launch unless cache TTL still warm | Single long-lived process holds a warm prompt cache and in-memory state across many jobs — best for cache reuse | Same advantage if the process stays alive across multiple `query()`/`ClaudeSDKClient` calls (designed for this) |
| Guardrails/approval gates | Hooks + permission modes enforced *underneath* the model — strong safety story | Hand-roll every gate in your own tool-execution code | Inherits Claude Code's hook/permission machinery "for free" — biggest edge over raw Messages API here |
| Debuggability | Auto-saved `.jsonl` session transcripts, resumable/forkable | Fully custom logging — more effort but full control | Gets `.jsonl` transcripts too, pairable with your own structured logs |

**Recommendation:** Use the **Agent SDK embedded in a standalone Python/Node long-running process**, invoked once per poll cycle (or kept resident) by Windows Task Scheduler — not per-job `claude -p` shells, not a hand-rolled Messages-API loop. Rationale: (1) the deterministic-skeleton architecture wants poll/filter/dedupe/route/ledger logic in plain code with the LLM only at hard nodes — arguing against Claude Code as the top-level driver of the *whole* pipeline; (2) at the LLM-invocation nodes, the Agent SDK's hooks/permission-mode machinery gives a ready-made, enforced-below-the-model gate on the one dangerous action (final submit) that a hand-rolled loop would have to reimplement; (3) a single long-lived process preserves prompt-cache warmth (~5 min ephemeral TTL) across consecutive applications in one poll cycle, whereas per-job `claude -p` processes would each pay a fresh prefix cost unless invocations are tightly clustered. Concretely: your own daemon owns the ledger/scheduler/dedupe/ATS-routing; it calls a `ClaudeSDKClient` per job with a Playwright MCP tool surface, and a `PreToolUse` hook hard-blocks the submit tool pending your own approval-queue state.

**Model tiering** (pricing per the `claude-api` skill, cached 2026-06-24):

| Model | ID | $/MTok in/out | Fit for this pipeline |
|---|---|---|---|
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1 / $5 | Cheap classification: ATS-vendor detection fallback, question-bank fuzzy-match arbitration, field-type classification, job-relevance filtering |
| Claude Sonnet 5 | `claude-sonnet-5` | $2 / $10 | Default workhorse: field-mapping/filling from an accessibility-tree snapshot, moderate free-text answers — highest-volume LLM node, benefits most from prompt caching |
| Claude Opus 5 | `claude-opus-5` | $5 / $25 | Cover-letter/essay tailoring (highest value-per-token, once per application), the confidence-gate judgment on whether a novel question needs human review |
| Claude Fable 5.1 | `claude-fable-5-1` | $10 / $50 | Only if budget allows and the confidence-gate/tailoring quality bar demands the most capable model — otherwise Opus 5 is the practical ceiling for this use case |

This is a textbook cheap-classify/expensive-generate split. **Cost-per-application estimate at 50 applications/day**: for a mid-complexity ATS form (~6-10 accessibility-tree snapshots at ~1.5K-4K tokens each, a cached ~3K-6K-token profile prefix, ~500-1500 output tokens per node): Haiku classification calls total well under $0.01; Sonnet field-mapping/filling across the form (~20K-40K largely-cached input + ~3K-5K output) runs roughly $0.05-$0.15 once the cache is warm; Opus for one cover-letter tailoring plus 1-2 confidence-gate calls runs roughly $0.03-$0.06. **Total: ~$0.10-$0.25 per application, i.e. $5-$12.50/day at 50/day** — token cost is not the binding constraint; spend the marginal cost on higher effort/Opus at the ambiguity-judgment node rather than penny-pinching there.

**Prompt caching for "profile + resume + Q&A bank" prefix:** Render order is `tools` -> `system` -> `messages`; any byte change earlier in the prefix invalidates everything after it. Structure: fixed tool defs -> `system` prompt with (a) fixed agent instructions then (b) the full profile + resume + answered-question bank, with a `cache_control: {type: "ephemeral"}` breakpoint immediately after this static block -> `messages`, where only per-job/per-field/per-snapshot content varies and comes *after* the breakpoint. Never interpolate a timestamp, job ID, or unsorted JSON before the breakpoint — that silently busts the cache. Practical savings: strong **within a single continuous run** (a batch of jobs processed back-to-back in one session — every field-fill/classification call re-uses the cached prefix, collapsing a 5K-token profile reused dozens of times to essentially one full read). Payoff **across separate scheduled invocations** depends on the default ~5-minute ephemeral TTL: a Task-Scheduler run every 2-3 minutes stays warm, an hourly/daily poll does not. This is a second, independent reason (beyond hook-based guardrails) to **batch**: accumulate newly-discovered jobs per poll tick and process them consecutively in one warm session rather than firing one invocation per job. Verify actual hit rate via `usage.cache_read_input_tokens` in each response.

Sources: https://code.claude.com/docs/en/agent-sdk ; https://www.anthropic.com/engineering/building-effective-agents ; https://platform.claude.com/docs/en/agent-sdk/permissions ; https://mindstudio.ai/blog/claude-code-headless-mode-autonomous-agents ; https://claudefa.st/blog/guide/development/scheduled-tasks ; https://supalaunch.com/blog/claude-code-scheduling-loop-schedule-cron-recurring-tasks-guide ; https://likeone.ai/blog/claude-code-cron-jobs-guide-2026 ; pricing/model-tier table from the bundled `claude-api` skill (cached 2026-06-24).

---

## 3. Browser control options on Windows — concrete comparison

### 3.1 Playwright with `launch_persistent_context` / `channel: "chrome"` on a real Chrome profile

`chromium.launchPersistentContext(userDataDir, {channel: 'chrome', headless: false})` launches the actual installed Chrome binary against a user-data directory holding real cookies/localStorage — log in once, subsequent runs are already authenticated. **Critical 2026 limitation**: Chrome 136+ actively blocks CDP/automation flags against the **default** `User Data` directory ("pages not loading or the browser exiting" — [neovasolutions.com](https://www.neovasolutions.com/2024/06/13/automating-chrome-with-existing-profiles-using-playwright-and-typescript/), [playwright#5258](https://github.com/microsoft/playwright/issues/5258)). Workaround: copy the real profile to a new, non-default path once (or have the user log into a dedicated Chrome profile there) and point Playwright at that. Session reuse this way is best-in-class — the only way to literally reuse already-authenticated LinkedIn/Workday/Greenhouse/iCIMS sessions with zero login flow. Detectability: real Chrome + `channel: "chrome"` avoids the Chromium-vs-Chrome fingerprint gap and Selenium's `cdc_` markers, but a saved profile "produces a browser that looks like headless/automated Chromium to fingerprinting systems... it doesn't make the browser look human" ([BrowserStack](https://www.browserstack.com/guide/playwright-persistent-context)) — `navigator.webdriver` and other CDP tells remain if a site checks aggressively.

### 3.2 Playwright MCP (`@playwright/mcp`) — accessibility-tree snapshots

The mechanism most relevant to an LLM-driven loop. **What `browser_snapshot` returns**: a YAML-like serialization of the accessibility tree — role, accessible name, state, each tagged with a stable `ref` (namespaced for iframes, e.g. `f1e12`):

```
- heading "Login" [level=1]
- form "Sign in" [ref=e1]
- textbox "Email" [ref=e2]
- textbox "Password" [type=password] [ref=e3]
- checkbox "Remember me" [ref=e4]
- button "Sign In" [ref=e5]
- link "Forgot password?" [ref=e6]
```
([Playwright MCP snapshots docs](https://playwright.dev/mcp/snapshots), via Context7 `/microsoft/playwright-mcp`)

**Why it's token-efficient**: the tree only encodes elements exposed to accessibility APIs, skipping styling/layout/pixel data. A full-page snapshot of a typical application form is a few hundred to low-thousands of tokens; a full-HD screenshot for computer-use costs up to ~4,784 visual tokens (~$0.0135 on Opus-tier pricing) *per screenshot*, needed after nearly every action ([tokencost.app](https://tokencost.app/blog/claude-browser-use-tool-token-cost)). Over a 20-step multi-page application, snapshot-based spend is typically an order of magnitude lower. Refs are also stable and semantically addressable — the model doesn't re-derive click coordinates from an image each step, which matters for small/overlapping targets common in ATS multi-select/typeahead widgets. (One counterpoint benchmark: MCP's tool-schema + snapshot verbosity can run ~114K tokens for a full task vs ~27K via a leaner CLI-style tool — still far below screenshot-driven costs, but not free.)

**How the loop uses refs**: call `browser_snapshot`, see `textbox "Email" [ref=e2]`, call `browser_type({ref: "e2", text: "..."})` or `browser_click({ref: "e5"})`; Playwright MCP tools return an updated snapshot automatically post-action. `browser_fill_form` accepts a batch of `{ref, value}` pairs to fill a whole form in one call. Core tool set: `browser_snapshot`, `browser_click`, `browser_type`, `browser_select_option`, `browser_fill_form`, `browser_press_key`, `browser_hover`, `browser_wait_for`, `browser_navigate`, `browser_tabs`, `browser_file_upload`, `browser_take_screenshot` (fallback), `browser_evaluate`.

**Real-profile / CDP support**: Playwright MCP supports `--executable-path` (real Chrome binary), `--browser chrome` (channel selection), `--user-data-dir <path>` (persistent profile, same default-dir caveat as 3.1), `--cdp-endpoint <endpoint>` (attach to an already-running Chrome instead of launching a new one), and `--extension` (attach to a running Chrome/Edge instance via a Playwright browser extension — no CDP flag needed, and the cleanest workaround for the Chrome 136+ default-profile block, since it attaches to the user's *actual* live tabs rather than a spawned copy). Reliability is high for standard HTML forms; degrades on canvas-rendered widgets, custom date-pickers without proper ARIA roles, or heavily obfuscated component libraries some ATSes use.

### 3.3 Claude native computer-use (screenshot + coordinates)

Declaring the computer-use toolset adds ~4,500-4,590 input tokens up front plus ~735 tokens/tool definition, plus a screenshot every step; a multi-step task commonly runs 50K-200K+ tokens once every screenshot/reasoning turn is counted — roughly **5-20x** the cost of a snapshot-based step ([tokencost.app](https://tokencost.app/blog/claude-browser-use-tool-token-cost), [valueaddvc.com](https://valueaddvc.com/blog/claude-computer-use-the-api-feature-that-lets-ai-control-your-desktop)). Reliability is worse for dense forms — coordinate clicks are brittle to reflow/zoom/async content shifts, with no structural guarantee the clicked element is the intended one; reported ~3 min/form and still not reliably completing it. Still necessary for: canvas-rendered UI (some assessment/coding-challenge embeds), CAPTCHAs/slider puzzles, custom widgets with no accessible roles, and as a cheap final-screenshot human-review gate before submit (even if the acting loop is snapshot-based). If self-hosted against your real Chrome it can reuse the session; Anthropic's hosted computer-use environment does not have the user's real profile and is disqualifying for that reason.

### 3.4 browser-use (Python, github.com/browser-use/browser-use)

An Agent orchestrator running observe -> think -> act; a `DomService` extracts interactive elements with an indexed mapping fed to the LLM as text (reads the full DOM including hidden/off-screen elements, not just accessibility-exposed nodes — sometimes catches more, at the cost of noisier/larger context) ([Fireworks AI](https://fireworks.ai/blog/opensource-browser-agent)). Model-agnostic (works with Claude). Token cost per step is higher than Playwright MCP snapshots but far lower than screenshots. Reported strong on generic web tasks and cited with a low (<5%) "prompt-adjustment rate" versus 15-25% raw-Playwright-selector churn on changing sites, but no job-application-specific reliability data was found. Supports persistent/real-Chrome contexts with the same caveats as 3.1. Windows setup is a bigger commitment (full framework, own config/deps) than adding an MCP server.

### 3.5 Stagehand (github.com/browserbase/stagehand)

Layers `act()` / `extract()` / `observe()` on Playwright — `page.act('click the checkout button')` resolves natural language against the live page via LLM rather than a hand-written selector; `observe()` discovers available actions without executing (cheap dry-run); self-healing when a site's DOM changes ([browserbase.com](https://www.browserbase.com/blog/ai-web-agent-sdk), [Stagehand docs](https://docs.stagehand.dev/v3/first-steps/introduction)). MCP-compatible, works with Claude. Comparisons cite ~89-92% success across Playwright+Claude / Stagehand / Browserbase on generic automation benchmarks and ~$0.05-0.15/task — roughly comparable to the other options, no clear best-in-class specifically for form-filling. Each `act()` call is itself an LLM call re-resolving intent, likely somewhat higher token cost per action than one shared snapshot driving several actions. Browserbase's own hosted infra is cloud-only and would NOT have the user's local Chrome session — must self-host Stagehand locally against the real profile for session reuse.

### 3.6 Selenium + undetected-chromedriver

Legacy in 2026. Selenium injects detectable `cdc_...` markers; "Playwright is generally better for scraping... does not inject cdc_ markers, has built-in stealth capabilities" ([roundproxies.com](https://roundproxies.com/blog/best-patchright-alternatives/)). undetected-chromedriver still defeats basic `navigator.webdriver` checks and simple Cloudflare challenges but is "consistently detected by advanced anti-bot systems" (DataDome/Kasada) since its specific patches are catalogued ([alterlab.io](https://alterlab.io/blog/selenium-bot-detection-and-how-to-fix-it)). Still reached for only where an existing Selenium codebase makes a rewrite costly. No reason to choose it new here — skip it.

### 3.7 Raw CDP against the user's own running Chrome (`--remote-debugging-port`)

**The 2026 blocker**: Chrome 136+ ignores `--remote-debugging-port`/`--remote-debugging-pipe` when launched against the **default** user-data-dir — Google tightened this specifically to stop attackers attaching CDP to a real logged-in profile and exfiltrating session state ([trycua/cua#2916](https://github.com/trycua/cua/issues/2916), [dassi.ai](https://www.dassi.ai/blog/chrome-remote-debugging-port-browser-agents/)). This directly undermines "attach CDP to the user's live default Chrome window" as originally imagined. Practical route: launch Chrome once with a **copied/dedicated, non-default** profile directory and `--remote-debugging-port=9222 --user-data-dir="C:\ChromeAutomationProfile"`, have the user log into all job sites there once, then `playwright.chromium.connect_over_cdp("http://localhost:9222")`. This is functionally the persistent-context approach (3.1) via a different attach path, with the added benefit that the human can watch/interact with the same visible window (useful for HITL review-then-submit). Risk: anything reaching the debug port has full CDP access — bind to `localhost` only, never expose on a network interface, and treat the debug profile directory as sensitive (it accumulates the same cookies as production browsing). Note: Chrome 146+ is reportedly adding a native settings toggle for remote debugging — re-check status closer to implementation.

### Comparison summary

| Approach | Token cost/step | Reliability for forms | Session/cookie reuse | Detectability | Windows setup |
|---|---|---|---|---|---|
| Playwright, real Chrome profile, no LLM | ~0 (no LLM in loop) | High with hand-written selectors; brittle to redesigns | Yes (persistent context) | Real-Chrome fingerprint; profile alone isn't "human-looking" to advanced fingerprinting | Low |
| **Playwright MCP (accessibility-tree)** | Low (~hundreds-few thousand tokens/snapshot) | High for standard HTML forms; degrades on canvas/custom widgets | Yes — best via `--extension` or `--cdp-endpoint` on a dedicated profile | Same as Playwright underneath; `--extension`/CDP to the user's real Chrome is least detectable | Low (`npx @playwright/mcp`) |
| Claude computer-use | Highest (5-20x snapshot cost) | Lowest for precise form fields; better for canvas/CAPTCHA | Yes if self-hosted; no if using Anthropic's hosted env | Coordinate-driven patterns can look more bot-like | Moderate |
| browser-use | Moderate-high (full DOM extraction) | Good generically; unverified for ATS specifically | Yes, same caveats as Playwright | Same as Playwright underneath | Moderate (own framework) |
| Stagehand | Moderate (~$0.05-0.15/task) | Good (~89-92% cited generically); self-healing to DOM changes | Yes if self-hosted (not via Browserbase cloud) | Same as Playwright underneath | Moderate (own framework) |
| Selenium + undetected-chromedriver | ~0 (no LLM) | Same brittleness as raw selectors | Yes via profile flags | Purpose-built evasion but increasingly caught by advanced anti-bot | Low, but legacy |
| Raw CDP to dedicated real-Chrome profile | ~0, or same as MCP if paired with it | High (literal real browser/session) | Best possible — is the real session | Least detectable (not a spawned automation profile) | Low-moderate (one-time profile setup) |

**Recommendation**: **Playwright MCP, attached to a real Chrome install via a dedicated (non-default) profile directory — preferably through `--extension` or `--cdp-endpoint` so it can attach to the user's actual live, already-logged-in Chrome window — driving through accessibility-tree snapshots as the default mode, with Claude computer-use as a narrow fallback for unsnapshot-able widgets (canvas assessments, CAPTCHAs, drag-and-drop) and as a cheap final-screenshot review artifact before submit.** Set up once: create a dedicated Windows Chrome profile (not the user's default `User Data`), have the user log into LinkedIn/Workday/Greenhouse/Lever/iCIMS/email there one time, then run `@playwright/mcp --browser chrome --user-data-dir "<dedicated profile path>"` (or `--cdp-endpoint`/`--extension` to attach to an already-launched instance the human can also watch live). This satisfies the core requirement — reusing existing logged-in sessions across many ATS platforms with zero re-authentication — while staying inside Chrome 136+'s security model and keeping cost/reliability best-in-class for the overwhelmingly common case (standard HTML forms). Skip Selenium/undetected-chromedriver entirely; don't build the core loop on browser-use or Stagehand — both are reasonable but add framework dependencies with no demonstrated job-application-specific edge over Playwright MCP, which already gives the snapshot/ref primitives, real-Chrome-profile support, and first-party Claude/MCP integration.

Sources: https://playwright.dev/mcp/snapshots ; https://github.com/microsoft/playwright-mcp ; https://playwright.dev/mcp/configuration/browser-extension ; https://playwright.dev/docs/api/class-browsertype ; https://www.neovasolutions.com/2024/06/13/automating-chrome-with-existing-profiles-using-playwright-and-typescript/ ; https://github.com/microsoft/playwright/issues/5258 ; https://github.com/microsoft/playwright/issues/24252 ; https://www.browserstack.com/guide/playwright-persistent-context ; https://github.com/trycua/cua/issues/2916 ; https://www.dassi.ai/blog/chrome-remote-debugging-port-browser-agents/ ; https://tokencost.app/blog/claude-browser-use-tool-token-cost ; https://valueaddvc.com/blog/claude-computer-use-the-api-feature-that-lets-ai-control-your-desktop ; https://fireworks.ai/blog/opensource-browser-agent ; https://www.browserbase.com/blog/ai-web-agent-sdk ; https://scrapfly.io/blog/posts/stagehand-vs-browser-use ; https://roundproxies.com/blog/best-patchright-alternatives/ ; https://alterlab.io/blog/selenium-bot-detection-and-how-to-fix-it ; https://www.unbrowse.ai/blog/mcp-browser-servers-compared ; https://dev.to/pointchecknote/browser-automation-with-claude-playwright-mcp-why-accessibility-snapshots-beat-screenshots-2pke

---

## 4. State & idempotency

**Persistence layer:** SQLite, not a JSON file. Single-user, single-machine, but you need transactional writes (crash mid-write must not corrupt state), indexed lookups (dedupe hash, status), and a queryable audit trail across potentially thousands of rows over months. JSON is fine for static profile/config, wrong for a growing, queryable ledger.

**Proposed schema:**

```sql
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,              -- 'linkedin', 'indeed', 'greenhouse', ...
  external_id TEXT,                  -- ATS/job-board's own job id, if available
  url TEXT NOT NULL,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  dedupe_hash TEXT NOT NULL UNIQUE,  -- sha256(normalize(company)+normalize(title)+normalize(url_or_external_id))
  ats_platform TEXT,                 -- 'workday','greenhouse','lever','icims','successfactors','unknown'
  discovered_at TEXT NOT NULL,       -- ISO8601
  raw_posting_json TEXT              -- full scraped posting for later reference
);

CREATE TABLE applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  status TEXT NOT NULL,              -- state machine, see below
  status_updated_at TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  last_error_at TEXT,
  current_step TEXT,                 -- e.g. 'page_2_of_4', 'field:linkedin_url' -- resume point
  form_state_json TEXT,              -- partially-filled field values, for resume after crash
  storage_state_path TEXT,           -- Playwright storageState() JSON snapshot (cookies/localStorage) at last checkpoint
  trace_path TEXT,                   -- path to trace.zip for this attempt
  screenshot_dir TEXT,
  submitted_at TEXT,
  confidence_score REAL,             -- lowest confidence among LLM-answered questions this run
  needs_human_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE answers (
  id INTEGER PRIMARY KEY,
  question_normalized TEXT NOT NULL, -- normalized question text
  question_raw TEXT NOT NULL,
  answer TEXT NOT NULL,
  embedding BLOB,                    -- optional, for semantic match
  source TEXT NOT NULL,              -- 'human' | 'llm_generated' | 'llm_reused'
  application_id INTEGER REFERENCES applications(id),
  created_at TEXT NOT NULL,
  UNIQUE(question_normalized)
);

CREATE INDEX idx_apps_status ON applications(status);
CREATE INDEX idx_jobs_dedupe ON jobs(dedupe_hash);
```

Add a `status_history` table (job_id, from_status, to_status, at, note) if a full audit trail beyond the single `status_updated_at` timestamp is wanted.

**State machine:** `discovered -> filtered_out | queued -> filling -> review -> submitted`, with failure branches `filling -> retrying -> filling` (transient) or `filling -> needs_human` (permanent/ambiguous), and `review -> needs_human` (low confidence or human declines).

**Dedupe:** `dedupe_hash = sha256(normalize(company) + "|" + normalize(title) + "|" + (external_id or normalize(url)))`; normalization = lowercase, strip punctuation/whitespace, strip common suffixes ("Inc.", "LLC"). Prefer the ATS's own `external_id` (Greenhouse/Lever job IDs are stable and canonical) over URL matching, since the same posting is often cross-listed on LinkedIn/Indeed/the company's own site with different URLs. Enforce via `UNIQUE` on `dedupe_hash` plus `INSERT OR IGNORE` — the DB itself refuses a second application row for the same job, which is the simplest possible idempotency guarantee.

**Resumability for multi-page forms:** checkpoint after every page/step transition, not just at the end — persist `current_step`, `form_state_json` (values entered/decided so far, keyed by field id/label), and a Playwright `context.storage_state(path=...)` snapshot at each checkpoint. On restart, an `applications` row stuck in `filling` with a non-null `current_step` re-launches the browser with that `storage_state`, navigates back to the application URL, and either resumes from the ATS's own server-side saved-draft feature (Workday/Greenhouse both auto-save partial applications keyed to the user's account — treat this as the most reliable resume mechanism where available) or replays `form_state_json` against the reopened form.

**Retry/backoff:** exponential backoff with jitter for transient failures (network timeout, 5xx, element-not-yet-rendered) — e.g. 2s/4s/8s capped, up to 3-5 attempts, tracked via `attempt_count`. A circuit breaker per ATS platform (N consecutive failures across different jobs on the same platform -> pause that platform for a cooldown window) avoids hammering an outage or a platform that's started blocking you. Distinguish transient (auto-retry) from permanent (route straight to `needs_human`): CAPTCHA, login/2FA prompt, a required field with no mapped answer, or a below-threshold confidence score are all "permanent for this attempt." This mirrors dead-letter-queue patterns from Celery/BullMQ/Sidekiq (failed jobs after max retries move to a DLQ) — implement the equivalent as `status = 'needs_human'` rows with `last_error` populated, queryable as the dead-letter queue, no separate queue engine needed. A durable-execution engine like Temporal is arguably overkill at single-machine scale — the SQLite ledger plus a checkpointed in-process loop gives most of the durability benefit at a fraction of the operational complexity; reconsider only if distributing across multiple workers.

**How existing tools do it:** AIHawk / Auto_Jobs_Applier_AIHawk ([github.com/AIHawk-FOSS/Auto_Jobs_Applier_AI_Agent](https://github.com/AIHawk-FOSS/Auto_Jobs_Applier_AI_Agent)) drives config through `config.yaml` (title/location/blacklist filters) and `plain_text_resume.yaml` (profile data), with an LLM (OpenAI/Ollama/Gemini-configurable) generating answers per-question live at apply time. No visible persisted answer-bank or SQLite ledger — closer to a stateless per-run script than a durable ledger system. This is a concrete gap this project should close by persisting everything, deduping robustly, and making runs resumable.

---

## 5. Human-in-the-loop

**Batch-review-then-submit** is the strongest default for this domain: run the fill step for a batch of N applications, stop before any submit click, present one review surface (screenshots + extracted field values + LLM-generated free-text answers) for the human to approve/edit/reject each, then submit only the approved batch. This bounds risk while batching the human's attention instead of interrupting per-field.

**Dry-run mode** — "fill but stop before the final submit click" — should be first-class, not a debug hack; it's also the natural point to capture a screenshot/DOM snapshot for review. This maps directly onto the Agent SDK's `plan` permission mode (§2) and onto LangGraph's `interrupt_before` static breakpoint concept.

**LangGraph's interrupt primitives** (via [LangGraph human-in-the-loop docs](https://docs.langchain.com/oss/python/langchain/human-in-the-loop), [Medium walkthrough](https://medium.com/@areebahmed575/langgraphs-interrupt-function-the-simpler-way-to-build-human-in-the-loop-agents-faef98891a92), [DeepWiki](https://deepwiki.com/langchain-ai/langgraph-studio/6.2-interrupts-and-human-in-the-loop)) give two primitives: **static breakpoints** (`interrupt_before`/`interrupt_after` a named node — pauses unconditionally) and **dynamic `interrupt()`** (called *inside* a node, pauses mid-node, returns a payload to the human; whatever the human sends back on `Command(resume=decision)` becomes the interrupt's return value). The canonical use case named in the docs — "review tool calls before execution... prevent dangerous operations... pause before high-stakes actions like sending an email or approving a transaction" — maps directly onto pausing before "click Submit." The decision vocabulary is **approve / edit / reject / respond** (the last for "ask the user a direct question"). Even without adopting LangGraph, this four-way vocabulary is a good model for the submit-gate; a `needs_human`/`review` status row in the ledger (§4) *is* the serialized interrupt state, and "resume" is just an UPDATE moving status back to `filling`/`queued` with the human's decision written into `form_state_json`/`answers`.

**Real product UX precedent**: browser-use's interactive mode ([GitHub issue #3341](https://github.com/browser-use/browser-use/issues/3341)) prompts before executing a staged action set with Approve-and-execute / Reject-and-give-feedback / Skip / Cancel — a concrete per-action or per-page approval-gate UX reference. UiPath's attended vs. unattended RPA distinction is the enterprise-RPA analogue: attended bots run under direct supervision for judgment-heavy tasks, unattended bots run fully autonomously for well-understood repeatable tasks, and low-confidence outputs are routed to a human validation queue rather than blocking the whole run — i.e., confidence-based selective escalation rather than blanket per-step approval ([UiPath docs](https://docs.uipath.com/overview/other/latest/overview/attended-vs-unattended-automation)). General agent-UX pattern catalogs (Cloudflare Agents docs, agentic-patterns.com) converge on: only gate irreversible/costly/externally-visible actions; show the exact proposed action with full context (a rendered diff/screenshot, not a vague "should I proceed?"); support async/timeout-tolerant approval so a long run doesn't block forever on one review; keep an audit trail of what was approved/rejected and why.

**Confidence thresholds:** attach a confidence score to every LLM-generated answer (self-reported, or inferred from ambiguity signals — field label didn't closely match any profile field, or the free-text question doesn't fuzzy/semantically match anything in the bank). Commonly cited banding: **>90% auto-proceed, 60-90% proceed but flag for lightweight review, <60% escalate to human before continuing.** Apply per-question, roll up to `applications.confidence_score` as the minimum across an application so one weak answer routes the whole application to `review`/`needs_human` rather than silently submitting a guess.

**Surfacing "I'm not sure" efficiently:** don't interrupt mid-run per question — accumulate flagged questions across the whole batch and present them together (grouped by similarity) so the human answers each *distinct* uncertain question once; persist that answer to the bank (§6) immediately and apply it to every application currently blocked on it, rather than asking N times for what's really one question worded N ways. Given this is an unattended batch runner (not an interactive chat agent), an end-of-batch digest fits better than LangGraph's synchronous per-node interrupt, which is designed more for interactive agents.

**Default HITL gates (no confidence override):**
1. The final "Submit application" click — always a hard stop, always dry-run by default; show a full diff/screenshot before approval.
2. Any answer to a legally/ethically sensitive question (EEO/demographic, salary expectations, visa/sponsorship status, criminal history) — always surfaced regardless of confidence, since a wrong answer here has outsized consequences and answer-bank reuse would silently propagate it.
3. First-time answer to any new "hard question" not yet in the bank — human confirms/edits once, then it's memorized for reuse without re-asking.

---

## 6. Answer memory (question-bank pattern)

**Pipeline:** normalize -> exact match -> fuzzy match -> embedding/semantic match -> LLM disambiguation -> ask human once, persist.

1. **Normalize**: lowercase, strip punctuation, collapse whitespace, strip trailing "?" and boilerplate ("Please describe...", "(optional)").
2. **Exact match** against `answers.question_normalized` (`UNIQUE` index -> O(1) lookup) — catches the very common case of the same ATS template asking the identical question across postings (e.g. Greenhouse's standard "Are you legally authorized to work in the US?").
3. **Fuzzy match** (rapidfuzz/thefuzz, Levenshtein-family) above a threshold (commonly ~85-90 similarity ratio) — catches near-identical phrasing ("Do you require visa sponsorship?" vs "Will you now or in the future require visa sponsorship?").
4. **Embedding/semantic match** — embed the new question, compare cosine similarity against stored question embeddings (a small local embedding model plus a lightweight sqlite-vec/FAISS/Chroma index is plenty for a single-user local tool — no hosted vector DB needed) above a threshold (commonly ~0.85-0.9 cosine) — catches semantically-identical but lexically different phrasing ("Why do you want to work here?" vs "What draws you to our company?").
5. **LLM disambiguation** as the last automated step before asking a human: given the new question plus the top-K nearest stored questions, ask a cheap model (Haiku 4.5) "is this the same underlying question as any of these, and if so which, else NEW" — catches cases where similarity search returns a plausible-but-wrong near-match (e.g. "years of experience with Python" vs "...with SQL" are lexically/embedding-close but substantively different) that should not be silently reused.
6. **Ask human once, persist** — if nothing matches, pause (batched per §5), get the human's answer, `INSERT` into `answers` keyed by normalized text (store the embedding too), and immediately apply it to the current and any other pending applications with the same/matching question.

**Reuse vs. light tailoring:** for factual fields (visa status, years of experience, salary expectation, willingness to relocate) exact reuse is correct — don't let the LLM "improve" a factual answer. For narrative fields ("why this role/company") the *pattern* (structure, key selling points) should be reused as a template, with the LLM re-tailoring company/role-specific details — treat these as `answer_templates`, still gated by the same matching pipeline recognizing "this is a 'why us'-style question" as a category.

**Recommended starting thresholds (tune empirically):** exact match 100%; fuzzy accept >=90 ratio, review-flag 80-90; embedding accept >=0.88 cosine, review-flag 0.75-0.88; below that, treat as new/ask-human. Log every match decision (which stage matched, score) so thresholds can be recalibrated from real data over time — the same "monthly calibration review, plot accuracy vs. confidence band" discipline used in production confidence-threshold systems generally.

Public prior art specifically documenting this exact pipeline was thin — AIHawk-style tools appear to regenerate answers per-application via a live LLM call each time rather than maintaining a persistent, matched answer bank, which is both a cost and a consistency weakness (the same objective fact could get inconsistently phrased/valued across applications). A purpose-built answer bank with this matching pipeline is a concrete improvement over existing open-source tools.

---

## 7. Observability

**Structured action log** — one JSON-lines file per application attempt (or a table), each entry: `{timestamp, application_id, step_type, tool_name, target_ref_or_selector, input_summary, decision_rationale (if LLM-driven), result, latency_ms}`. This is the primary "what did the agent actually do and why" audit trail, cheap to write, and the natural companion to the DB status transitions in §4 (DB = current/aggregate state, action log = full blow-by-blow history).

**Playwright tracing** (`context.tracing.start(screenshots=True, snapshots=True, sources=True)` ... `context.tracing.stop(path="trace.zip")`) captures, per action, a before/after DOM snapshot, a screenshot, console logs, and network requests — replayable frame-by-frame in the Trace Viewer (`npx playwright show-trace trace.zip`, or trace.playwright.dev) — the single highest-leverage debugging artifact for a failed run, letting you click through the exact click/fill/navigate sequence and inspect the live DOM and network calls after the fact without having watched the run live. Overhead is modest relative to LLM call latency in this pipeline. **Default to tracing every application attempt**, not just failures — storage is cheap and a run that "succeeded" but produced a subtly wrong submission is exactly the case you want a trace for.

**Video recording** (`browser.new_context(record_video_dir=...)`) is heavier (continuous video vs. discrete per-action snapshots) and mostly redundant with a full trace for programmatic debugging — make it opt-in for cases where a human-watchable recording is specifically wanted (e.g., showing someone what the bot did), not the default debugging tool.

**Screenshot-on-failure**: even with full tracing on, also write a final full-page screenshot to a well-known path whenever an application transitions to `failed`/`needs_human`, so a human triaging a backlog can visually scan before opening the trace viewer for the ones that actually need deeper inspection.

**Per-run artifact layout:**
```
runs/<application_id>/
  trace.zip
  action_log.jsonl
  screenshots/
    01_landing.png
    02_page1_filled.png
    ...
    final.png
  storage_state.json      -- for resume
  form_state.json         -- last known field values
```
with the DB (`applications.trace_path`, `.screenshot_dir`) pointing at this folder, so any ledger row is one click away from its full forensic record — mirroring how CI systems (e.g. Playwright's own test reporter) organize per-test artifacts.

---

## Overall architecture recommendation (synthesis)

1. **Runtime**: a standalone Python (or TS) long-running daemon on Windows, launched/kept alive via Task Scheduler, owning the deterministic skeleton (poll -> filter -> dedupe -> route -> ledger writes) and the SQLite ledger. It calls into a Claude Agent SDK `ClaudeSDKClient` (not raw `claude -p` shells, not a hand-rolled Messages loop) for the two escape-hatch node types: free-text/cover-letter generation and unmapped-field/unknown-ATS recovery.
2. **Browser**: Playwright MCP attached to a dedicated (non-default) real Chrome profile via `--extension` or `--cdp-endpoint`, driven primarily by accessibility-tree snapshots; per-ATS deterministic Playwright fillers for the known-template fast path (Workday/Greenhouse/Lever/iCIMS), LLM-driven snapshot reasoning only on structural drift or genuinely novel fields; computer-use reserved for canvas/CAPTCHA edge cases.
3. **Models**: Haiku 4.5 for classification/routing/fuzzy-match arbitration, Sonnet 5 as the default field-mapping/filling workhorse, Opus 5 for cover-letter tailoring and the confidence-gate judgment call. Cache the profile+resume+answer-bank prefix with an `ephemeral` breakpoint, and batch jobs per poll cycle to keep the cache warm.
4. **Guardrails**: a `PreToolUse` hook hard-blocks the submit action independent of model behavior; default mode is always dry-run/fill-then-stop; batch-review-then-submit is the standard human touchpoint, with real-time escalation reserved for genuinely blocking cases (CAPTCHA, 2FA, no mapped answer).
5. **Memory**: a persistent answer bank (SQLite + embeddings) matched via normalize -> exact -> fuzzy -> embedding -> LLM-disambiguation -> human-once, which existing open-source tools (AIHawk) notably lack.
6. **Observability**: full Playwright tracing on every attempt, structured JSONL action logs, screenshot-on-failure, and a per-application artifact folder linked from the ledger — enabling after-the-fact audit of any of potentially thousands of runs.
