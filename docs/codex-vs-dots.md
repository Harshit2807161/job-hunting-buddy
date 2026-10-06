# Codex versus Dots for this project

Assessed 2026-10-01. Recommendation: **local Codex CLI planner plus Browser Use
CLI for v1; optional Dots coordination later**. This is a requirements
comparison, not a performance benchmark. No comparative live application run has
been performed, and Dots account eligibility has not been checked.

| Requirement | Codex CLI worker | OpenAI Dots | Choice for v1 |
|---|---|---|---|
| Phase 1 cron integration | Invoke `codex exec` from the local worker; validate structured output | Assigned recurring work and supported connected-service events exist; no direct local queue/webhook contract verified | Codex |
| Resume and local cover-letter skill | Read local files in the worker's allowed scope | Local skills require a connected computer | Codex |
| Reliable final review boundary | Enforce in browser executor and test with fixture submission counters | Custom rules, task review, and action approvals; rules can make mistakes | Codex plus code guard |
| Continuous work when laptop is off | Local scheduler cannot run | Cloud computer can continue while personal devices are off | Dots |
| Session reuse | Existing local Chrome session through Browser Use CLI | Separate cloud sessions; private sign-in and takeover; local fallback possible | Local worker for v1 |
| Notifications and conversational oversight | Implement candidate email/local review queue | Messaging and task coordination are built in | Dots optional |
| Secret handling | OS keyring; never give password to planner | Private browser sign-in; optional saved passwords; confirmation required to reuse saved login | Both have a workable path |
| SDLC and reproducibility | Versioned skills, schemas, deterministic executor, CI fixtures | Can create/continue Work and Codex tasks; use the same repository for reproducible execution | Codex |
| Cost and access | Existing local CLI says logged in with ChatGPT; subject to plan limits | Account/market availability and limits must be verified; do not assume access | Existing Codex |

Codex supports non-interactive invocation and JSON-schema output; it reuses saved
CLI authentication. That is sufficient for the local planner. Do not upload its
authentication file or run subscription-authenticated jobs in public CI.
[Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Authentication](https://learn.chatgpt.com/docs/auth).

Dots can maintain responsibilities, schedules, and supported event monitoring.
Its proactive research alone does not mutate apps or control a browser; assigned
tasks have their own permissions. A scheduler-to-Dot API for this poller's SQLite
queue was not established in the reviewed documentation.
[Tasks and memory](https://learn.chatgpt.com/docs/dots/tasks-and-memory).

Its cloud machine persists separately from the candidate's browser. A connected
local computer must be online with the ChatGPT app open; local access is a separate
connection. Cloud-blocked sites may require a separate local task.
[Computers and apps](https://learn.chatgpt.com/docs/dots/computers-and-apps).

Dots custom rules complement existing permissions and automatic review. They are
instructions the agent tries to follow, and do not override built-in requirements.
For this project's explicit pre-submit stop, a tested executor guard is preferable
to relying solely on instructions; that preference is our engineering judgment.
[Controls](https://learn.chatgpt.com/docs/dots/controls).

## Later hybrid

Keep this local queue/executor as the system of record. Connect the laptop and
project to a Dot, ask it to monitor application packets and missing answers, and
let it coordinate a local task when available. Verify any event trigger before
replacing cron. Retain the same no-submit executor, provenance rules, and private
credential store. Neither choice is proven here to solve Greenhouse CAPTCHA unattended.
