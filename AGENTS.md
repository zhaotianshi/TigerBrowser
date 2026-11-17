# AGENTS.md

Instructions for any coding agent working in this repository: Claude Code,
Codex, Cursor, Copilot, Gemini or anything else. `CLAUDE.md` imports this file;
keep shared repository rules here.

---

## Before you touch anything

1. **This repository is public.** The professionalism rules below govern every
   line, commit message, PR title and comment you write.
2. **Read [`ci/tribal-rules.yml`](ci/tribal-rules.yml).** It lists the decisions
   this repo has already made, each with its evidence. Many are checked by
   `pythonlib/tests/test_tribal_rules.py`. Do not reopen one without new evidence.
   When a new decision is argued and settled, add it there with its citation.
3. **Branch off `main`. Never commit or push to `main`.** After the initial import, every change is a pull
   request tied to a GitHub issue ([`CONTRIBUTING.md`](CONTRIBUTING.md)).
4. **Check `git status` before editing.** Preserve unrelated changes and local
   data. Do not reset a dirty checkout, stage unrelated files, or commit or push
   unless asked.

## Before you write a function

Search the relevant implementation first: `pythonlib/camoufox/`, `frontend/studio/src/`, `ci/` and `scripts/`. If something close
exists, extend it rather than forking it. Duplicated logic is a serious defect
here, because the copies drift and a fingerprint built from two drifting copies
is detectable.

## Professionalism

A public repository is a finished product. People judge the project by what
is in it, and they will not ask what a file was for.

- **No garbage.** No scratch files, diagnostic scripts, saved test output,
  logs, debug logging, commented-out code, dead code, placeholder text, stub
  docs, or `TODO`s left as notes to self.
- **Nothing private reaches this repo.** That includes proprietary results,
  private repo names, and per-vector stealth detail. Do not include private diagnostic results or individual stealth vectors in public artifacts.
- Write commits and PR titles for a stranger reading them in a year.
- **Documentation is never stale.** Update the README, `docs/` and the package
  READMEs in the same pull request as the code that changed them, never
  "later". Every snippet must run as written.

## Code

- **Less code.** More code is a cost, not an achievement. Make minimal, general
  changes, and delete dead code when you find it.
- **No band-aids.** Fix the main flow. A hard-coded value or a special case at
  the call site is a bug relocated, not fixed.
- **Fallbacks are a last resort.** They turn a loud failure into a silent wrong
  answer, and in an anti-detect browser a silent wrong answer is a fingerprint.
  Fail loudly instead.
- **Check real data before inventing a value.** Every spoofed value must be
  something a real device reports. Take it from the recorded distributions
  (fpgen, `pythonlib/camoufox/*.json`), not from memory.
- **Read the provider's docs** before writing against a third-party API or a
  Firefox internal. Never infer an endpoint, pref or field from memory.
- **Comments say why, in a sentence or two.** Simple code needs none. A longer
  explanation is a decision: put it in `ci/tribal-rules.yml` or `docs/`.

## Tests

- **Run the failing test, not the whole suite.** CI runs the full pipeline on
  every pull request.
- **Write test output to a log file and grep the file.** Piping a run straight
  into `grep` throws away output you will need. Delete the log afterwards.
- **Every bug gets a regression test, in this order:** reproduce it with a test
  and confirm the test fails, fix the bug, confirm the test passes, then land
  both.
- **No flakes.** An intermittent failure means something is non-deterministic.
  Fix that behaviour. Never retry, loosen a threshold or skip the test to get
  green.
- **A missing prerequisite fails in CI; it never passes as a skip.** A skip
  reads as green, so a job that forgot to install something passes having
  tested nothing.
- **When local and CI disagree, name the mechanism and fix it in the repo.**
  Typical causes are git-ignored inputs, skipped prerequisites and cold-cache
  races. A fresh clone is a sanity check, not a fix.
- **Never write a test just to pass, or code just to pass a test.**

## Security

- **Never commit a credential.** That covers code, config, fixtures, logs and
  commit messages. CI secrets live in GitHub Actions secrets.
- **Least privilege** for workflows and tokens. Grant `permissions:` per job,
  and only what that job needs.
- **Adding a dependency is a decision.** Say why in the pull request, and commit
  the lockfile.

## Working with the maintainers

- Say when a direction is wrong, before starting, and give the reason.
- Review your own diff the way a strict senior reviewer would. You are biased
  toward what you just wrote.
- Explain plainly and briefly.

## Parallel work

- **Separate pull requests:** one agent per task, each in its own git worktree,
  running at the same time.
- **One pull request with independent slow parts:** use subagents in worktrees.
  Merge each part into your branch as it finishes, then delete the worktrees.
- **Shared files or an ordering requirement:** use one agent.

---

# Repository

TigerBrowser manages persistent profiles with a PySide6 desktop, a Python
team service and a Vue console. Follow [the client/server upgrade design](docs/studio-client-server.md)
and its progress table; planned features are not implemented capabilities.

This repository uses published browser artifacts. It contains no Firefox source,
browser patches or browser build pipeline. Browser behaviour and the supported
config schema come from the installed artifact, including its `properties.json`.
The standalone Python `Camoufox` and `AsyncCamoufox` APIs remain supported.

| Path | Purpose |
| --- | --- |
| `pythonlib/camoufox/` | Python launcher, identity data and verified model installation |
| `pythonlib/camoufox/studio/` | Shared engine, store, desktop, automation, backup and migration |
| `pythonlib/camoufox/studio/team/` | Team API, durable runs, native gateways and existing console |
| `frontend/studio/` | Vue console, console tests and canonical diagnostic collector source |
| `assets/studio/` | Desktop icons |
| `scripts/` | Studio packaging/installers, decoder build and Python data maintenance |
| `ci/` | Static/Python gate helpers, self-tests and settled decisions |
| `service-tester/` | Explicit real-browser Studio runtime checks |
| `docs/` | Usage, contracts, deployment and upgrade design |

## Development

Run from the root in an isolated Python environment. The Python library supports
Python 3.10+; the locked desktop environment and Windows packager require 3.12+.

```bash
python -m pip install -r pythonlib/studio-requirements.lock
python -m pip install --no-deps -e './pythonlib[studio]'
python -m camoufox fetch
python -m camoufox studio
```

`fetch` installs the official browser, default addons and pinned model. Normal
startup does not download missing prerequisites. `studio` opens the desktop client,
which always connects to a team server; the Qt management windows, `--local` and
`studio-host` are removed. The team service requires the packaged Vue build;
`--web-root` and the old console are removed.

```bash
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build
```

Build before starting a source team service, running its tests or packaging
Python. Vite writes `pythonlib/camoufox/studio/team/web/` (ignored by Git); Poetry
and PyInstaller include the same files. See `frontend/studio/README.md`.

## Studio boundaries

Remote display uses only verified Linux KasmVNC. PNG capture, Windows dedicated
session streaming, backend selection and `--windows-user` are retired.

- **One engine.** Desktop, local automation and team execution reuse
  `studio/engine.py` (`prepare_launch`, `BrowserEngine`).
- **One member resolver.** Reuse `studio/access.py` (`MemberStore`) and durable
  run ownership. Control expiry does not prove the browser stopped.
- **Preserve ownership and data.** `studio-data/`, server workspaces and client
  caches contain user data. Use temporary test workspaces. Never remove locks or recovery barriers, reset identities to
  force launch, or activate a migrated copy before source retirement.
- **Protect secrets and routes.** Use native credential storage. Credentials,
  discovery capabilities, workspace data and backup keys never enter logs or
  packages. A selected route failure never falls back to direct access.
- **Precise remote claims.** Management, headless execution and native display
  have separate gates. Preserve exact Host/Origin, CSRF and revocation checks.
  HTTP and network allowlists do not encrypt traffic. Native acceptance needs
  real-runtime evidence, independently of mocked tests and static preflight.

Read references for the affected area:

| Task | References |
| --- | --- |
| Desktop, identity, routing | `docs/studio.md`, `docs/studio-capabilities.md`, `docs/studio-nodes.md` |
| Automation | `docs/studio-automation.md`, `docs/studio-integration.zh-CN.md` |
| Preflight and locks | `docs/studio-preflight.md` |
| Team and consoles | `docs/studio-team-api.md`, `docs/studio-team-service.md`, `frontend/studio/README.md` |
| Native and recovery | `docs/studio-native-acceptance.md`, `docs/studio-remote-view.md` |
| Backup and migration | `docs/studio-backup.md`, `docs/studio-migration.md` |

## Tests and packaging

Start with the affected file. Capture output in a temporary log, inspect the exit
status and failures with `rg`, then remove the log. Full local gates:

```bash
python -m pip install -r ci/requirements.txt -c pythonlib/team-ci-requirements.lock -e './pythonlib[geoip,studio,team]'
python scripts/pin-fpgen-model.py
python -m pytest ci/tests -q
npm --prefix frontend/studio ci
npm --prefix frontend/studio test
npm --prefix frontend/studio run build
python -m ci.run_pythonlib
```

Regenerate `studio/team/probe.js` after changing its canonical collector with
`npm --prefix frontend/studio run build:probe`. Commit source and generated probe
together; Python tests verify the canonical LF source digest.

Client packaging uses `scripts/package-studio.py` on each target platform with the
locked prerequisites in `docs/studio.md#client-packages`; CI builds all four
without Actions artifact storage. Formal delivery follows `docs/studio-releases.md`:
each official GitHub Release lists the merged PRs for that version and includes all
four client installers. Source updates and installer publication are separate.
Capacity shortages defer publication rather than block verified source or authorize
spending changes.
`service-tester/studio_*_tests.py` uses actual browsers and native resources;
follow its README and platform prerequisites. Missing prerequisites fail.

The required GitHub check remains **All tests passed**. Unavailable cloud CI has
not passed: report local evidence and cloud limitations separately. User
instructions may authorize continuing with local verification.
