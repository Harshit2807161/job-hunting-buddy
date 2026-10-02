# Browser Use setup and live validation

Browser Use is installed as a local coding-agent tool, following
https://docs.browser-use.com/open-source/browser-use-cli:

```sh
uv tool install --python 3.12 --upgrade --force 'browser-use @ git+https://github.com/browser-use/browser-use.git'
browser-use skill install --target codex --no-install
browser-use skill install --path skills/browser-use/SKILL.md --no-install
```

The installed source revision is
`302d8fcb245a7a63fb7531a4734c9ce3c7792779`. The Browser Use Python library is
version `0.13.10`; its CLI delegates to Browser Harness `0.1.13`, so
`browser-use --version` reports the harness version. The project optional
`applications` dependencies also include Browser Use. The login-shell PATH
includes `~/.local/bin`.

The official skill is installed at `~/.codex/skills/browser-use/SKILL.md`, with a
repository copy at `skills/browser-use/SKILL.md`. Local browser control uses CDP
and does not require an OpenAI API key or a Browser Use Cloud account.

For the existing Chrome session, enable **Allow remote debugging for this
browser instance** at `chrome://inspect/#remote-debugging`. This is the manual
first step documented in
https://github.com/browser-use/browser-harness/blob/main/install.md. If macOS
then shows an Allow sheet, keep the browser command running and use
`browser-use mac-approve` once.

```sh
browser-use <<'PY'
print(current_tab())
print(page_info())
PY
```

An isolated Chrome instance can also be started with the Browser Use
`BrowserSession` library. Point the CLI at its exact endpoint with `BU_CDP_WS`
or `BU_CDP_URL`; this was the route used for the live check. Browser Use can copy
Chrome profiles into temporary directories, so verify the resolved profile path
before using candidate data. Persistent candidate sessions must stay private.

### Explicit local endpoint

If Chrome shows **Server running at: 127.0.0.1:PORT**, remote debugging is enabled
even when automatic discovery reports that `DevToolsActivePort` is missing.
Use the endpoint shown by Chrome instead of repeating the setup step:

```sh
BU_CDP_URL=http://127.0.0.1:PORT BH_HOME="$PWD/private/browser-use-harness" browser-use <<'PY'
print(page_info())
PY
```

Replace `PORT` with Chrome's current port. The endpoint at port `52018` was
verified through `/json/version` and an actual Browser Use `page_info()` call.
The local endpoint and harness directory are saved in the ignored project
`.env` for application commands, which load that file. Shell calls to the CLI
still need these environment variables. The existing private default daemon is
reused; no per-job local daemon is needed.

## Live-site result, October 1, 2026

Using Browser Use's actual CLI and CDP helpers against live Glassdoor:

- The software-engineer search page loaded in a headed, isolated local Chrome.
- The agent located **Easy Apply only** in the accessibility tree and clicked it
  with CDP mouse input. The URL acquired `applicationType=1`, and results updated.
- The agent opened the real authentication modal. It offered **Continue with
  Google** and **Continue with Apple or email**.
- No CAPTCHA was presented during these actions. CAPTCHA solving is unverified.
- No live account was created and no application was filled or submitted.

Private evidence is in `private/browser-use-live/validation.json` and
`private/browser-use-live/authentication.png`. The exact task recording is under
`private/browser-use-harness/agent-workspace/recordings/glassdoor-live-access`.
These are ignored local artifacts; they must not be committed or published.

This verifies live navigation, rendering, accessibility inspection and clicks.
It does not establish end-to-end application compatibility. The existing
application worker still uses Playwright and requires migration and further live
validation. Synthetic fixture results are recorded separately.
