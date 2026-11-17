# Studio checks

The repository checks the Python launcher, Studio and Vue console using published
browser artifacts. It does not build Firefox, run a second TypeScript launcher,
or publish custom browser releases.

## Gates

| Job | Required evidence |
| --- | --- |
| Static checks | Python syntax and CI/installer self-tests |
| Python and Studio | Verified wheel frontend and `python -m ci.run_pythonlib`, including retained rule checks |
| Vue tests and build | Verified diagnostic probe regeneration, console tests and production build |
| Client package | Real PyInstaller build and installer on Windows, macOS arm64/x86_64 and Ubuntu, frozen runtime check, Windows PE subsystem check and digest record |
| Linux Vue remote runtime | Real systemd credential persistence, failed unlocks and HTTP team-service restarts, KasmVNC sessions, paired viewers, final-tab shutdown/restart with persistent data, Firefox/Chromium decoder checks and the Vue settings and timezone selection flow |
| All tests passed | Full run only: every job above reports success, including four built and verified client packages; skipped jobs fail this gate |

Source checks do not upload or download Actions artifacts. Installers built for
PR verification remain on the temporary runner. Formal installers are published
by the separate **Release Studio clients** workflow directly to GitHub Releases;
see [the release rules](../docs/studio-releases.md). Delivery to a team server is a
manual administrator step described in `docs/studio.md#client-packages`. Inno Setup is
downloaded from its pinned GitHub release and checked against its asset digest.
Per-job permissions are limited to repository reads. Browser downloads use the
job token only for GitHub API access. Release publication is not part of this workflow.

The native job installs Python dependencies and the built project into a dedicated
virtual environment under `/var/tmp` and disables the runner's user site. Its systemd
checks execute that same interpreter as a non-login account, which cannot traverse
the runner's private temporary directory or use its user-site packages. The real
credential and service lifecycle tests require imports to work after the account
switch; the installed package does not depend on access to the source checkout.

## Staged cloud validation

Pull requests always run the complete four-platform gate. Creating or updating
a pull request therefore also starts macOS packaging. To defer macOS, use a
manual **Tests** run with `stage: server-windows` before opening the pull request:

| Manual stage | Client packages | Aggregate check |
| --- | --- | --- |
| `server-windows` (default) | Windows x64 and Ubuntu x64 | `Selected stage passed (server-windows)` |
| `macos` | macOS arm64 and x86_64 | `Selected stage passed (macos)` |
| `full` | All four platforms | `All tests passed` |

Every stage still runs the static, Python, Vue and Linux native checks. A partial
stage cannot satisfy the required `All tests passed` check, and separate partial
runs are not combined into a full result. All runs fail their aggregate check on
any failed, cancelled or skipped prerequisite. Packaging checks are not desktop
installation, update or cross-computer acceptance.

Manual runs require the workflow to exist on the default branch; select the
intended feature branch in **Run workflow**. If GitHub does not expose dispatch
for that branch, report the limitation instead of substituting a PR or claiming
that the staged checks ran. See GitHub's
[workflow dispatch documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch).

Partial, full and PR checks build and verify installers without storing them.
There is no `upload_installers` input. Consumers build the frontend locally using
the same commit and lockfile; native failure diagnostics appear in job logs.
An exhausted artifact allowance cannot block source validation through an upload
step. Keep the account's spending limit unchanged; do not raise the budget to get
a passing build or treat unavailable compute as a successful check.

Each job has a timeout. New runs cancel older runs only for the same event,
branch/PR and stage; a manual partial run cannot cancel a PR's full gate, and the
two partial stages can run independently. Existing verified KasmVNC and npm caches
use their separate cache allowance; they are not release storage.

## Installer publication

After full **Tests** succeeds on the exact `main` commit, dispatch **Release Studio
clients** on `main` with tag `studio-vVERSION` matching the Python package version.
It creates a source-bound draft, uses the same four-platform packager as PR checks,
and uploads installers plus their digest/protocol records directly to that draft.
Publication requires all four builds and a fresh verification of every downloaded
asset. Failed publication leaves a draft, not an advertised partial release.
Only release jobs receive repository write access; source checks remain read-only.
When capacity is unavailable, merge verified source and defer installer publication.

## Run locally

Use Python 3.12+ and Node.js 22 in an isolated environment. From the repository root:

On Ubuntu, install the native virtual-display and Qt libraries first:

```bash
sudo apt-get update
sudo apt-get install -y xvfb xauth libegl1 libgl1 libopengl0
```

Then install the locked application dependencies and run the gates:

```bash
python -m pip install -r ci/requirements.txt -c pythonlib/team-ci-requirements.lock -e './pythonlib[geoip,studio,team]'
python scripts/pin-fpgen-model.py
python -m pytest ci/tests -q
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build:probe
npm --prefix frontend/studio test
npm --prefix frontend/studio run build
python -m ci.run_pythonlib
python -m pip wheel --no-deps ./pythonlib --wheel-dir .ci-work/wheels
python -m ci.verify_wheel .ci-work/wheels
```

Capture each command's output in a temporary log, check its exit status and search
the saved log for failures, then delete the log. Start with the affected file while
developing. Follow `docs/studio.md#client-packages` for client packaging and
[service-tester/README.md](../service-tester/README.md) for real native runtime checks.

### WSLg display sockets

On WSL with WSLg, `/tmp/.X11-unix` can be a read-only host mount. The real Xvfb
tests then fail before acquiring a display; this is not the normal Ubuntu CI
filesystem. Check `findmnt /tmp/.X11-unix`. Do not remount or remove WSLg's directory.
With `bubblewrap` installed and permitted by the host, give only the test process
an independent socket directory:

```bash
validation_dir=$(mktemp -d)
mkdir "$validation_dir/x11"
chmod 1777 "$validation_dir/x11"
bwrap --unshare-user --unshare-pid --die-with-parent --bind / / --proc /proc \
  --dev-bind /dev /dev --bind "$validation_dir/x11" /tmp/.X11-unix -- \
  "$(command -v python)" -m ci.run_pythonlib > "$validation_dir/pythonlib.log" 2>&1
validation_exit=$?
rg 'failed|passed|ERROR|FAIL' "$validation_dir/pythonlib.log"
printf 'Exit status: %s; inspect temporary evidence in %s\n' "$validation_exit" "$validation_dir"
```

Inspect and remove that temporary directory afterwards. This changes no global
mount or namespace policy, does not skip display tests, and is unnecessary on
a normal writable Ubuntu X11 socket directory.

## Python evidence

`run_pythonlib` writes JUnit and a gate record under `.ci-work/`. `CI_WORK_DIR` and
`CI_RESULTS_DIR` may select other temporary locations. A nonzero pytest process,
missing test records, failures or errors cannot become a passing gate. The process
exit status matters even if all individual test records report success.

`ci/_pytest.py`, `ci/_util.py` and `ci/results.py` retain reusable runner/evidence
plumbing. `pythonlib/tests/test_tribal_rules.py` checks the retained rules in
`ci/tribal-rules.yml`. Browser-patch-only rules left with the retired build system.

The Vue job tests the sole frontend. Python, native and packaging jobs build that
same source with the shared frontend action, including the hidden Vite manifest.
`ci.verify_wheel` checks
the resources extracted from the built wheel through the production validator.
Linux native checks build the pinned KasmVNC component on Ubuntu 24.04 and use
actual browsers; missing display tools, decoders or browser assets fail the job.
It also starts isolated non-login systemd services to check the server's native
keyring across restarts, reject a wrong unlock key and preserve existing secrets.
It also initializes a temporary team through the real CLI, saves a proxy password
over HTTP and reopens that workspace through the actual systemd entrypoint.
Only the test harness needs root; credential daemons run under the non-login account.
The native job first executes the production namespace command to expose host
policy failures directly. It caches only a successfully built KasmVNC runtime,
keyed by Ubuntu target, architecture, installer and all pinned sources/patches.
System dependencies are installed on every run and restored binaries must pass
the production version and digest checks before the actual session tests.

Cloud CI that cannot start has not passed. Report available local checks and that
limitation separately; do not create a substitute successful status. A user may
authorize proceeding with local verification while cloud execution is unavailable.
