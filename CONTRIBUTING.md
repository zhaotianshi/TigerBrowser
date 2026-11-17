# Contributing to TigerBrowser

Read [AGENTS.md](AGENTS.md) and [ci/tribal-rules.yml](ci/tribal-rules.yml) before
making changes. The repository contains the Python launcher, Studio and its Vue
console. Browser releases come from upstream; Firefox build tooling and the
unused TypeScript launcher have been retired.

The [upgrade design](docs/studio-client-server.md) records the target architecture,
implementation order and acceptance requirements. [ROADMAP.md](ROADMAP.md) links
current work. Use an existing issue or create one before starting a new task.

## Pull requests

- Branch from main; never commit or push to main.
- Associate each PR with an issue and follow the PR template.
- Keep one logical change per PR. Migrate depended-on code before deleting it.
- Preserve unrelated changes, workspace data and native credentials.
- Reproduce each bug with a failing test, fix it, and confirm the regression passes.
- Update user documentation in the same PR as implementation changes.
- Explain any new dependency and include its lockfile.

## Verification

Use an isolated Python environment and install this checkout. The locked desktop
and Windows packaging environment requires Python 3.12+; the Python library itself
supports 3.10+.

```bash
python -m pip install -r ci/requirements.txt -c pythonlib/team-ci-requirements.lock -e './pythonlib[geoip,studio,team]'
python scripts/pin-fpgen-model.py
python -m pytest ci/tests -q
npm --prefix frontend/studio ci
npm --prefix frontend/studio test
npm --prefix frontend/studio run build
python -m ci.run_pythonlib
```

Start with affected tests. Capture output to a temporary log, inspect the exit code
and failures with rg, then remove the log. Native runtime checks have their own
[prerequisites](service-tester/README.md); unit tests do not certify native display.
Windows packaging prerequisites and commands are in the root README.

The required cloud check remains **All tests passed**. An unavailable workflow has
not passed; state the limitation and actual local evidence in the PR. See
[ci/README.md](ci/README.md) for gate behaviour.

Source checks retain all four client builds without Actions artifact uploads.
Publish formal installers through the separate source-bound Release workflow;
follow [the release rules](docs/studio-releases.md). Capacity shortages defer
installer publication, not verified source updates.
