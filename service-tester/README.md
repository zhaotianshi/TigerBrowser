# Studio real-runtime checks

## Response observation and byte transfer

`studio_response_transfer_tests.py` uses the installed Camoufox executable,
Node.js 22+, a temporary workspace and a controlled loopback HTTP page. Install
the runtime prerequisites below and set `CAMOUFOX_STUDIO_BINARY` to the actual
executable. It opens its own headed test browser, with humanized mouse motion
disabled in the synthetic profile so event deadlines do not include random input
delays. It never uses a real website, account, proxy or generation service.

```powershell
$testLog = Join-Path $env:TEMP 'studio-response-transfer.log'
python -m pytest service-tester/studio_response_transfer_tests.py -q *> $testLog
$testExit = $LASTEXITCODE
rg 'FAILED|ERROR|passed|failed' $testLog
Remove-Item -LiteralPath $testLog
if ($testExit -ne 0) { throw 'Response transfer runtime validation failed' }
```

The test covers image responses, native attachments, redirect identity, delayed
completion, truncated connections, size limits, cancellation, concurrent pages,
exact length/SHA-256 and parity with the existing Base64 download. It executes the
[shipped Node client](../scripts/studio-artifact-client.mjs) in response, download
and automatic selection modes, including legacy capability discovery. Missing prerequisites fail. This validates the
local headed runtime; Linux private-display downloads and production website
behavior require their own acceptance. See the [API contract](../docs/studio-response-transfer.md).

## Systemd service credentials

`studio_credentials_tests.py` uses real systemd encrypted credentials, D-Bus and
GNOME Keyring on Ubuntu 24.04. Install the locked team prerequisites into an
isolated environment and install this checkout as described below. Then run from
the repository root (the interpreter and checkout must be readable by `nobody`):

```bash
sudo apt-get install --no-install-recommends dbus-daemon gnome-keyring
sudo "$(command -v python)" -m pytest service-tester/studio_credentials_tests.py -q > studio-credentials.log 2>&1
```

The test runs as root only to create transient system services and encrypt their
unlock credentials. The service processes run as the non-login `nobody` account
with a separate temporary home and private runtime directory. It verifies native
credential writes and reads across separate service lifetimes, encrypted storage,
upgrade preservation, wrong-key rejection and subsequent recovery with the original
key. A second test checks preserved installation settings using real systemd
environment-file parsing and argument expansion without starting or initializing
a team workspace. It stops its own transient services and removes its temporary state; no real
workspace or user keyring is opened. `systemd-creds` may initialize the machine's
root-only host encryption key on first use. Missing prerequisites fail, not skip.
Inspect and remove the log. This credential test is not fresh-server installation,
whole-machine reboot, browser execution or cross-host acceptance.

`studio_server_lifecycle_tests.py` additionally initializes a synthetic team using
the real terminal command, starts the actual systemd team entrypoint twice, logs
in over HTTP, saves a proxy password and reads it through the same native service
bus after restart. It checks clean workspace-lock release and absence of plaintext
in SQLite and the keyring. Use the same root invocation and prerequisites, plus
`bubblewrap`; the browser mode is disabled and no real proxy connection is made:

```bash
sudo "$(command -v python)" -m pytest service-tester/studio_server_lifecycle_tests.py -q > studio-service-lifecycle.log 2>&1
```

Inspect and remove the log. The test owns only temporary state and transient units;
it does not install the production service or certify an operating-system reboot.

## Studio runtime regression

The separate opt-in `studio_runtime_tests.py` uses a real Camoufox executable,
a temporary workspace and a loopback TCP team service. It does not require proxies,
antibot test sites or the TypeScript detection bundle. Its HTTP clients explicitly
ignore outbound proxy environment variables; the persistence test supplies an unusable
proxy to verify that local acceptance stays on loopback. From the repository root,
install the locked test prerequisites into an isolated Python environment:

```bash
python -m pip install -r pythonlib/team-ci-requirements.lock
python -m pip install --no-deps -e ./pythonlib
```

Fetch the browser, model and default addons using `python -m camoufox fetch`, or
use an already prepared installation. Set `CAMOUFOX_STUDIO_BINARY` to its actual
executable; missing prerequisites fail rather than skip. Linux example:

```bash
export CAMOUFOX_STUDIO_BINARY=/opt/camoufox/camoufox
python -m pytest service-tester/studio_runtime_tests.py -q > studio-runtime.log 2>&1
```

PowerShell example:

```powershell
$env:CAMOUFOX_STUDIO_BINARY='D:\Browsers\Camoufox\camoufox.exe'
python -m pytest service-tester/studio_runtime_tests.py -q *> studio-runtime.log
```

Replace the example binary path with the installed executable. Inspect the log
before deleting it; do not commit logs or temporary workspaces. The regression
checks two independent authenticated clients sharing one run, permission revocation
without restarting the browser, persistent cookies/localStorage/IndexedDB and identity
across restart, then encrypted stopped backup, quarantine and exclusive same-host
activation retiring the exact source. It never enables main-world evaluation or
changes a real workspace. A second test drives the actual web console through login,
application restart on the same loopback address, reconnected status and a successful
group-creation request with the refreshed CSRF. It also checks logout, page errors
and 375px horizontal overflow. Its temporary screenshot requires visual inspection;
automated layout checks do not certify CJK font coverage or accessibility.
The host/timezone choice case preserves a disconnected server interface and a saved
IANA alias, displays the recorded proxy timezone, searches canonical choices and
saves automatic selection as an empty value. It checks the actual Vue form at
375px and emits desktop/mobile screenshots for visual inspection.
Native remote frames, input control, file isolation,
rendering consistency and physical cross-host recovery are outside this test's scope.
The stopped server-handover regression also uses two independent host registration
directories with a real browser. It prepares an encrypted destination request,
retires the source, activates the target and checks the same site data, identity,
member login and grants. This simulates host replacement rather than testing two
physical servers. Run that case alone with:

```bash
python -m pytest service-tester/studio_runtime_tests.py::test_stopped_server_handover_preserves_site_data_and_member_access -q > studio-migration.log 2>&1
```

Inspect and remove the log afterwards. The operational steps, compatible artifacts,
receipt-bound activation and remaining limits are in [server migration](../docs/studio-migration.md).

## Checkout and API-key runtime regression

With the real browser, model and addons prepared as above, checkout acceptance
also requires a working native credential backend in the test user's session.
`CAMOUFOX_STUDIO_BINARY` must identify one registered, exact installed kernel.
No real workspace or existing credential is used:

```bash
python -m pytest service-tester/studio_checkout_tests.py -q > studio-checkout.log 2>&1
```

Four cases exercise actual local browser runs, cookie/localStorage/IndexedDB and
identity persistence across return, exclusive ownership, grant revocation,
force-release and stale-token rejection, and pending return after a real TCP
service outage. They use separate temporary server and client caches on one host,
not a graphical desktop shell or two physical computers.

The Node.js example in [the integration guide](../docs/studio-integration.zh-CN.md)
is exercised unchanged against the real server API and browser by:

```bash
python -m pytest service-tester/studio_api_key_tests.py -q > studio-api-key.log 2>&1
```

Install Node.js 22.15+ and leave loopback port 8767 available, or set
`CAMOUFOX_STUDIO_AUTOMATION_PORT` to a free port. This checks borrowing/releasing
an existing browser and immediate API-key revocation. A second case uses the
member's real server login to start through the client's loopback API, runs the
same Node sample, closes the local browser, waits for return and removes the
discovery file when local automation is withdrawn. It needs the checkout
credential prerequisites above. Only the example website's
response is synthetic; it is not an external-site or cross-host connectivity test.
Inspect and remove both logs afterwards.

## Exact-build runtime regression

`studio_kernel_tests.py` uses the same prepared browser and temporary workspace,
but explicitly downloads one additional official host package. It verifies the
published size and SHA-256, installs into a disposable kernel cache, and copies
the existing kernel there without activating or changing the user's cache:

```bash
export CAMOUFOX_STUDIO_SECOND_KERNEL=official/152.0.4-beta.30
python -m pytest service-tester/studio_kernel_tests.py -q > studio-kernels.log 2>&1
```

Choose an available exact build different from `CAMOUFOX_STUDIO_BINARY`; allow
disk space for its archive and two extracted kernels. Set
`CAMOUFOX_STUDIO_KERNEL_TEMP` to an existing temporary-volume directory if needed.
The test starts both builds, inspects actual executable paths and verifies that
changing the team's default affects only newly created profiles. Existing
identities and website state survive a restart using their original exact build.
Inspect and remove the log. Passing two builds of one Firefox version does not
certify different Firefox versions, every fingerprint patch, or cross-host use.

## Studio native regression

`studio_native_tests.py` is an opt-in Linux execution-host test. Use the same locked
Python prerequisites, prepared Camoufox binary and temporary-workspace convention
as above. Install the private display tools and a separate stock Firefox web client:

```bash
sudo apt-get install xauth xdotool bubblewrap xclip fontconfig
python scripts/install-studio-kasmvnc.py --install-system-deps --prefix "$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
export CAMOUFOX_KASMVNC_TOOLS="$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
python -m playwright install firefox
export CAMOUFOX_STUDIO_BINARY=/opt/camoufox/camoufox
python -m pytest service-tester/studio_native_tests.py -q > studio-native.log 2>&1
```

Long-text and clipboard regression can be run on its own:

```bash
python -m pytest service-tester/studio_native_tests.py -k native_long_text_and_explicit_clipboard_export -q > studio-clipboard.log 2>&1
```

This check sends a long Unicode value through the authenticated WebSocket, verifies
the native browser receives the complete text, and reads a fresh native selection
copy and the environment clipboard. Use the execution service's approved display-tool
PATH when the host restricts bubblewrap through AppArmor.

Persistent-input regression uses the same real Linux prerequisites:

```bash
python -m pytest service-tester/studio_native_tests.py -k 'native_input_reuses_worker or two_native_environments' -q > studio-input.log 2>&1
```

It sends 100 ordered pointer moves through one worker, reads actual X11 key/button
state before and after controller disconnect, checks clean worker shutdown, and
kills either the display or input worker to verify that the other environment stays
usable. Inspect the exit code and log, then delete the log. These are functional
checks; they do not establish a latency or frame-rate improvement. The portable
`test_studio_native.py`, `test_studio_native_input.py` and `test_studio_control.py`
unit tests cover pipe ordering and failure handling without certifying XTest or a
real browser.

Final-tab shutdown and restart can be checked independently:

```bash
python -m pytest service-tester/studio_native_tests.py -k last_native_tab_close -q > studio-last-tab.log 2>&1
```

This keeps authenticated control and display connections open while closing the
browser's last native tab. It checks worker/display teardown, a stopped durable
run, continued service availability, and a new run with unchanged identity,
cookies, localStorage and IndexedDB. Inspect and remove the log afterwards.

Replace the binary path with the actual installation. Run as a non-root service
account with user namespaces enabled; missing prerequisites fail. Inspect the log
and temporary screenshot before deleting them. The target is the actual Camoufox
headed browser; stock Firefox is only the independent console client, never an
execution fallback. Checks cover real KasmVNC display streams, native clicks/keys, Unicode
paste, admin handover with held-key release and stale-input rejection, host-file
isolation, pending-handshake capacity, CSRF, read-only watching, frame/input revocation,
Chinese upload/download names, unsupported IME events without disconnecting the
WebSocket, separate authenticated windows, full-viewport fullscreen geometry, original-size
rendering, floating tools, restoration of narrow native windows to the saved display,
read-only remote-entry metadata validated against the actual running native session,
retained browser lifetime after closing the window, console reopen, 375px and landscape
layouts. Font checks use the
browser's actual Fontconfig configuration and require the declared Chinese family and
Han glyph coverage in a matching bundle. This is a loopback
two-client test, not a two-physical-computer HTTPS deployment or a GPU/font fingerprint
certification. Remote execution is Linux-only; Windows remains a client.

The console screenshot test matches Firefox's native headless screen to its emulated
viewport. Viewport emulation alone leaves the compositor's fullscreen bounds unchanged
and can add misleading black padding to captures. The native screen is set through
[Mozilla's documented headless display variables](https://firefox-source-docs.mozilla.org/testing/geckodriver/Testing.html).

## KasmVNC runtime and console regression

Install the [pinned Studio runtime](../docs/studio-kasmvnc-runtime.md), prepare the
same real Camoufox, model, addons, system tools and stock Firefox client as above,
install the Chromium test client, and build the existing Vue console. The stock
v1.5.0 binary is not accepted as the
production runtime: the encoding cache, private IPC and unused UDP listener fixes
are checked by source markers and executable hashes.

```bash
export CAMOUFOX_KASMVNC_TOOLS="$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
python -m playwright install chromium
npm --prefix frontend/studio run build
python -m pytest service-tester/studio_kasmvnc_tests.py service-tester/studio_kasm_gateway_tests.py service-tester/studio_kasm_client_tests.py -q > studio-kasmvnc.log 2>&1
```

Inspect the exit code and failures in the temporary log, then delete it. Missing
prerequisites fail; these tests do not download anything. All workspaces are
synthetic and separate from real profiles. Run the pytest process within the
same private PID boundary used for native acceptance when verifying descendant
cleanup; never take ownership of a running user's workspace.

The protocol checks reuse the production display and input lifecycle. A synthetic
writer account belongs only to the tests and challenges the server's global
read-only flags. Tests cover authentication, owner-only API access, the production
upstream handshake, rejected input/clipboard/resize/configuration messages,
independent credentials and clipboards, and display/input failure containment.

Console checks open the packaged Vue page in stock Firefox, decode real KasmVNC Camoufox displays, send Chinese text, close
and reconnect in read-only mode. They check two concurrent viewers and preserve
cookies, local storage, IndexedDB and identity across three normal KasmVNC starts.
The sustained connection regression runs in both Firefox and Chromium. Each
client exercises the Vue console
with 60 seconds of native pointer motion and 45 seconds of idle viewing, then
verify a newly painted frame. This covers KasmVNC's missing WebSocket Pong
response beyond the default Python client's 20-second Ping and 20-second timeout.
The client-specific tests also exercise JPEG and
WebP pixels, decoded-frame/bitmap release in both browsers, cursor rendering,
strict CSP, disabled WebRTC/input/resize, source
provenance and disconnect during initialization. This is functional validation;
performance and target-host capacity require their own measured workload.

For a repeatable local measurement using that Vue console:

```bash
python service-tester/studio_display_benchmark.py --seconds 30 > /tmp/studio-display-benchmark.json
```

The benchmark creates its own temporary workspace and process boundary. It samples
actual decoded pixels and binary display bytes for a static page and a 25 Hz
synthetic color change, using the sole KasmVNC transport and the same saved identity.
It reports effective painted FPS and p95 update intervals, without a pass threshold.
This synthetic loopback comparison does not measure input latency, scrolling quality,
weak networks, physical clients or long-run resource capacity. Inspect the result
before deleting or exporting it; run on an otherwise idle host for a useful comparison.

## Studio boundary and optional measurement regression

`studio_boundary_tests.py` checks real kernel lifetime containment and explicit
recovery, without capturing a host desktop. On Linux it also kills the actual
native manager, recovers durable run ownership, and reopens committed cookies
without replacing the saved identity. Use the prepared binary and Linux native
dependencies above. On Windows select only the kernel case:

```powershell
python -m pytest service-tester/studio_boundary_tests.py::test_crashed_manager_contains_detached_descendants_and_recovers -q *> studio-boundary.log
```

Missing prerequisites fail; keep test workspaces separate from real browser data.

`studio_attestation_tests.py` runs two native starts and four content-realm reads,
then checks that a changed historical measurement does not block a restart. Its Linux
pytest process must itself run as the same private PID-namespace init as the host:

```bash
python -c 'import subprocess,sys; from camoufox.studio.team.boundary import linux_boundary; scope=linux_boundary([sys.executable,"-m","pytest","service-tester/studio_attestation_tests.py","-q"]); command=scope.__enter__(); result=subprocess.run(command); scope.__exit__(None,None,None); sys.exit(result.returncode)' > studio-attestation.log 2>&1
```

Inspect and remove logs afterwards. These tests measure same-host stability,
not all stealth checks or two physical computers. Deployment steps and the explicit
HTTP transport, private-peer switch and IP/CIDR allowlist are in
[native acceptance](../docs/studio-native-acceptance.md).
