# Browser Use credits for this pipeline

Assessment date: October 4, 2026. Local Chrome through the registered Browser
Use CLI remains the default application executor. Buying Cloud credits does not
change that connection or fund the local Codex subscription.

| Route | Useful for | Current limitation |
| --- | --- | --- |
| Existing local CLI browser | Reusing Google sign-in, local resume PDFs and guarded drafts | Shared browser lane; some hidden tabs pause native input |
| Isolated Cloud browser through the CLI | Independent public source checks; supported CAPTCHA/stealth handling | Separate authentication and session; local PDFs are unavailable remotely |
| Hosted Cloud agent | Tasks that continue when the candidate's laptop is offline | Requires a separate deployment with the booklet, boundaries, uploads and durable handoffs |

Cloud browsers automatically attempt supported CAPTCHAs. Cloudflare bot walls
use stealth/proxy protections instead. Neither path guarantees access. The
[official CAPTCHA guidance](https://docs.browser-use.com/cloud/browser/captcha-handling)
says to wait about ten seconds and inspect again while the solver works.

The [published rates](https://browser-use.com/pricing) are $0.02 per browser-hour,
plus traffic: $5/GB through the default residential proxy or $0.20/GB with direct
traffic/your own proxy. Hosted agents add model costs and a 20% service fee.
Traffic and retries can dominate browser time. No measured cost or speed advantage
is claimed for this application pipeline.

A live CLI sign-in attempt used one new owned local tab and preserved existing
drafts. The Cloud sign-in page rendered blank without a usable sign-in control;
CLI authentication did not complete. The attempt stopped before provisioning a
paid browser, exporting cookies or uploading candidate data. No credits were
spent and no Cloud CAPTCHA solver was validated.

Recommended next use: a bounded fresh Cloud session for a public source that
currently encounters a bot challenge. Keep application filling local until the
remote route proves its source-access benefit. Separate named daemons also need
separate browser lanes for real concurrency. Migrating filling requires remote
PDF staging, persistent review access and the same final-submit guard; simply
changing the daemon name is insufficient. Cookie/profile export is a separate
choice and is not required for a public-source trial.

See the [product-selection guide](https://docs.browser-use.com/cloud/which-product)
and [CLI profile-sync reference](https://github.com/browser-use/browser-harness/blob/main/interaction-skills/profile-sync.md).
