# Grounded qualitative proposals

`JHB_GROUNDED_NARRATIVES=1` enables a bounded fallback after existing factual
bindings and deterministic qualitative templates. It is off by default. Each
preparation pass attempts at most three new qualitative drafts and always stops
at portal review. A draft is never user-authored wording or permission to submit.

The local Codex CLI uses existing subscription authentication, stdin inputs,
an ephemeral read-only sandbox, structured output, disabled shell/web/MCP tools,
and a private isolated working directory. CI must inject synthetic execution;
the real process is disabled in CI. No API key provisioning, browser action,
email, credential reading, or profile mutation occurs in this drafting module.
Any reported tool action, invalid schema, outage, or timeout is a handoff.
The process has a 60-second bound and its process group is terminated on timeout.
See [official non-interactive Codex guidance](https://learn.chatgpt.com/docs/non-interactive-mode)
for read-only execution and structured output.

Inputs contain the exact observed prompt/ref, verified official description,
selected `sde`/`ml` resume facts, the selected PDF hash, and whitelisted brief
style preferences. Other resume variants, contact/disclosure details and file
paths are excluded from inference. A missing verified PDF, unknown role, or
unverified job-description identity prevents drafting.

Codex selects relevant exact evidence and concise framing; Python reconstructs
the answer and rejects extra factual prose. An answer may quote at most 25 words
from the official description and uses only selected verified resume excerpts.
This first iteration deliberately uses controlled paraphrase frames, rather
than trusting free-form generated factual claims merely because citations exist.
It handles brief interest, motivation, role-fit and proud-work prompt variants;
it cannot invent failure history, ownership details, personal anecdotes or
screening/legal/visa/compliance answers. Employer requests for non-AI wording
remain explicit candidate-input or deliberately acknowledged blank choices.

Accepted answers retain `proposed=True`, selected resume SHA, source excerpt
hashes and exact supporting quotes. The review inventory and local portal show
a proposed-wording badge. Role-fit considerations appear separately from final
reviewer issues. Every submission still requires the candidate's approval of the
exact packet, independent review and fresh retained-value checks.

Private mode-600 cache files are keyed by normalized prompt/ref, official JD,
selected role/PDF/facts and style. Changed evidence invalidates the cache;
failed drafting backs off for five minutes. Cache content never goes into git.

Tests use synthetic job descriptions, PDFs, model responses and form controls.
These checks validate drafting/portal contracts, not live Codex quality or live
Browser Use behavior. Live rollout remains disabled until separately reviewed.
