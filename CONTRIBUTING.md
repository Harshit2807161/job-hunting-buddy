# Development workflow

Keep `main` release-ready. Create a focused `feat/`, `fix/`, or `docs/` branch and
open a pull request with the problem, resulting behavior, and validation performed.
Use small conventional commits; never force-push shared branches. Tag milestones.

Install Python 3.12 and `python -m pip install -e '.[dev,discovery]'`.
Run `python -m pytest -q` before opening a pull request. Browser features must have
local fixture coverage and must never submit a real application during tests.

Keep resumes, answer booklets, screenshots, browser sessions, application records,
and credentials in ignored local paths. Commit sanitized examples only. Before
pushing, inspect `git diff --cached` and `git ls-files` for private data.

Document scope and acceptance criteria before implementing the next phase. Record
material architectural decisions, keep an explicit human submission boundary, and
identify any unverified behavior in the pull request.
