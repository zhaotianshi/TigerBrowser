# TigerBrowser

Studio manages persistent Camoufox browser profiles for a team. A Python team
service on one server owns the workspace: profiles, proxies, members, grants and
the audit. Members use the same Vue console from a web browser or from the
PySide6 desktop client on Windows, macOS or Linux, with a Chinese interface.
The [client/server design](studio-client-server.md) records the staged upgrade
and which parts still await physical acceptance.

A profile runs in one place at a time. The server runs it headless or, on
Linux, with a [native remote view](studio-team-service.md). A member with the
**本机运行** grant can instead check it out to their own computer through the
desktop client, run it there, and return its data when the browser closes.
There is no standalone local mode: the desktop always connects to a server.
No separate serverless application or installer is planned. Running a browser
locally uses the server's checkout and return workflow. Server workspaces and
client checkout caches must not share a writable data directory or be synchronized
outside that workflow. See [the product scope decision](studio-client-server.md#76-仅提供连接服务器的客户端).
Studio contains no cloud service, billing or automatic profile synchronization.

## Install and run

Set up the server first; [the team service guide](studio-team-service.md)
covers its installation, the first administrator, network scope and HTTPS.
Then install the desktop client from the repository root with Python 3.12 or
newer:

```bash
python -m pip install -r pythonlib/studio-requirements.lock
python -m pip install --no-deps -e './pythonlib[studio]'
python -m camoufox fetch
python -m camoufox studio
```

`fetch` installs the default extension and the verified fpgen model that local
runs need. Browser kernels for checked-out profiles come from the server's
kernel cache, so a client does not download them from GitHub. Startup never
downloads missing prerequisites; an incomplete installation fails with a setup
instruction. Custom `FPGEN_MODEL_URL` models are not accepted.

Use the complete desktop lockfile on every platform: checkout imports the shared
archive-validation code and requires `cryptography` on Windows and macOS as well
as Linux. A development environment containing the team extra can conceal an
incomplete desktop dependency set.

The first start asks for the server address, which must match the server's
`--origin` exactly, for example `http://192.168.1.10:8766`. The client checks
that the address answers as a Studio server before saving it; several addresses
can be kept. The console then loads from that server, and its login cookie
persists per server until it expires. Pages from other origins open in the
system browser instead of the client.

Linux needs a graphical desktop and Qt's system libraries. Local runs with
authenticated proxies need a native credential service: Credential Manager on
Windows, Keychain on macOS, or an unlocked Secret Service such as GNOME Keyring
on Linux. Studio never stores proxy passwords in a plaintext fallback.
Packaged clients are described in [Client packages](#client-packages); Ubuntu Desktop 24.04+ also has a
[source installer and application-list launcher](studio-ubuntu.md).

## Client cache

Each server has its own cache under the per-user data directory, for example
`%LOCALAPPDATA%\TigerBrowser\servers\<workspace-id>\` on Windows. It holds
only checked-out profiles, the device identity, local settings and the local
automation connection file. Only one client process can use a cache at a time,
and it keeps its lock after an unclean exit. See
[workspace locks](studio-preflight.md#工作区互斥与故障恢复) before reopening it.
`python -m camoufox studio --cache-dir <directory>` selects an independent cache.

The desktop's **本机** page shows the local settings, profiles waiting to
download or return, and retired copies. Its settings stay in that server's
cache and are never uploaded: local concurrency and
the [local automation interface](studio-automation.md#desktop-client).

Workspaces created by earlier desktop versions keep their profiles and website
data. Stop that desktop, create the first administrator with
`studio-team-init --existing`, and serve the directory with `studio-team`; see
[the team service guide](studio-team-service.md#安装和首次使用).

In **代理资源**, add one HTTP, HTTPS or SOCKS5 address or import a batch. See [proxy configuration](studio-nodes.md).

## Team members

Members, grants and sessions live in the server's workspace database. The
administrator is created locally on the server with `studio-team-init`; there
is no public registration, default password or remote initialization.

Member names are case-sensitive, byte-exact and never reused, even after a
member is disabled. Passwords are stored as salted scrypt hashes; exports never
contain members, groups, memberships, sessions, grants or audit entries. Five failed logins
for the same name within five minutes lock that name for fifteen minutes; a
successful login clears the counter. Login sessions last twelve hours, are bound
to the member's permission version, and stop applying the moment an
administrator disables the member, resets a password or changes any grant.
Use **修改密码** in the header for your own account; changing it logs out
immediately. The administrator's reset action is available only for other members.

The administrator manages members, per-environment grants and member groups
from **团队管理**, which also shows the redacted management audit.
Non-administrators see only environments with a `view` grant. Viewers cannot
start, stop, edit or delete environments. Operators additionally need `operate`
to start or stop a granted environment on the server, and `checkout`
(**本机运行**) to run it on their own computer. Creating, editing, restoring,
deleting, regenerating identities, Cookie transfer, proxy management, settings,
import/export and the audit require an administrator. Grants cannot exceed
those role limits, and saving grants does not remove grants for archived
environments absent from the editor. Group grants are combined with direct
grants and capped by role; see the [member-group rules](studio-member-groups.md).

Environment editor saves check the configuration revision and report
`revision_conflict` if another edit intervened. Team schema upgrades preserve
existing valid logins and grants; the v1 migration repairs session member
indexes, revokes old logins and adds revisions to legacy profiles without
changing identities or browser data. Login deadlines use Unix time and remain
comparable after a host restart.

Applications use member API keys on the server's separate automation listener,
or the desktop client's loopback interface for profiles running on that
computer. Both follow current member grants and never extend a login; see
[local automation](studio-automation.md). The desktop login controls Studio,
not other programs running as the same OS user; use OS accounts and
filesystem permissions for host-level isolation.

## Running on this computer

Select an environment and click **本机启动**. The client checks it out with
this computer's device identity, downloads only files that differ from its
cache, installs the environment's exact kernel from the server cache after
verifying its SHA-256 digest, and starts the browser with the same engine the
server uses. While it is checked out the server refuses to start, edit, delete
or snapshot it; other members see the holder and device in the list.

Closing the browser or clicking **停止本机** returns the changed files. The
server commits a return atomically and increments the data revision; an
interrupted transfer resumes on the next start or heartbeat. A check-out that
never launched can be abandoned from the **本机** page. If the client exits
while a return is pending, the profile stays **等待签回** until the client runs again.
Redelivering the same check-out retains local changes and any pending commit;
it does not download the server copy over an already running or returning copy.
Normal client exit refuses new work and waits for accepted checkouts and transfers
before releasing the cache lock. Cancelling the caller or closing twice does not
abandon a worker that can still write the cache; exit can therefore wait for an
in-flight network operation to finish or report its timeout.
Profiles sharing a cached proxy keep its credential until the last check-out ends.

An administrator can **强制解锁** a profile whose device is lost. The old
lease token is then rejected. If that client is still running, it closes the
browser and moves its unreturned data to a retired copy under `stale/` in the
cache, which can no longer be returned or started.
If browser cleanup is uncertain, the recovery barrier keeps the files in place;
heartbeat retries cannot move or return a possibly open directory. Revoking a member's
`checkout` grant asks the client to return the profile while still accepting
its data. Fingerprint deduplication uses the server's ledger, so a local run
cannot claim an identity already recorded on the server.

## Client packages

Members normally install a packaged client from the server's **客户端下载**
page instead of a source checkout. Each package contains Python, Qt with
QtWebEngine, Playwright's driver, the verified fpgen model, the default
extension. It contains no browser kernel, client cache,
website data or credentials; kernels come from the server for each checked-out
profile. The packages are unsigned.

| Platform | Package | Installs to |
| --- | --- | --- |
| Windows 10/11 x64 | Inno Setup `.exe` | `%LOCALAPPDATA%\Programs\TigerBrowser`, no administrator rights; SmartScreen warns on first run |
| macOS arm64 or x86_64 | `.dmg` with `TigerBrowser.app` | drag to Applications; open the first time with right-click → Open |
| Ubuntu 24.04 x64 | `.deb` | `/opt/tigerbrowser-client`, system dependencies declared by the package |

The Windows desktop EXE uses the GUI subsystem, so starting it does not
allocate a terminal. The internal `_internal/TigerBrowserRuntime.exe` helper keeps
standard streams for the packaging-time runtime check.

### Updates

After login the client compares its version and protocol range with
`GET /team/v1/client/update`. When the two cannot work together the console
shows only the download page until the client is updated. When the server
offers a newer compatible package for this platform, **客户端下载** offers to
install it. The client refuses while profiles run locally, downloads the
installer, checks its size and SHA-256 against the server's record, and then:

- Windows runs the installer silently, which replaces the program files and
  reopens the client.
- macOS replaces the application bundle after the client exits when its folder
  is writable, keeping the previous bundle if the copy fails; otherwise it opens
  the disk image.
- Ubuntu opens the package in the system software installer.

A client started from a source checkout updates through Git; it never runs an
installer. Over HTTP the record and the installer arrive on the same plaintext
connection, so someone on the network could replace both. That is the accepted
risk of the default HTTP mode; use HTTPS when the network is not trusted.

### Build and publish

Build each package on its own platform with Python 3.12, from the repository root:

On macOS, first install the Xcode command-line tools and `brew install openssl@3 rust`,
then export `OPENSSL_STATIC=1` and `OPENSSL_DIR="$(brew --prefix openssl@3)"` in the
build shell. Intel macOS may build the locked `cryptography` from source; follow
its [static-linking instructions](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos)
so the frozen client does not mix Homebrew and Python OpenSSL ABIs. Use a fresh
environment and clear any previously cached dynamic `cryptography` wheel with
`python -m pip cache remove cryptography` before installing the requirements.

```bash
python -m pip install -r pythonlib/studio-requirements.lock -r pythonlib/studio-build-requirements.lock
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build
python -m pip install --no-deps -e './pythonlib[studio]'
python -m camoufox fetch
python scripts/package-studio.py
```

Windows also needs [Inno Setup 6.7.3](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3),
found on `PATH`, in its default folder or through `--iscc PATH`. Ubuntu needs
`desktop-file-utils`, an unlocked Secret Service for the runtime check, and the
package's own dependencies. Existing output directories are refused; use
`--output <new-directory>` for another build. The builder copies only runtime
distributions and dependency licenses (license directories and conventional
text notices, not similarly named test binaries), runs the frozen client's
`--check-runtime` (QtWebEngine, the WebChannel script, the native credential
backend, the bundled model and runtime paths) and, on Windows, checks both
executable subsystem headers. It writes the installer and a `.json` record
with its version, platform, size, SHA-256 and protocol range. Pull requests and
full CI runs build and verify all four platforms without storing Actions artifacts.
[Staged validation](../ci/README.md#staged-cloud-validation) can run Windows/Ubuntu
before macOS; partial stages also build without uploading installers and
cannot satisfy the full acceptance gate.

Formal installers use the separate **Release Studio clients** workflow and
GitHub Releases. A source-bound draft becomes public only after all four builds
and their downloaded assets pass validation. Source updates can proceed while
installer publication is deferred for capacity or budget reasons. See the
[release rules](studio-releases.md) for exact version tags, full-main validation,
failure handling and server delivery.

To publish, copy the installers with their records into the server's client
directory, then build the manifest the service reads:

```bash
python -m camoufox studio-client-manifest /var/lib/camoufox-studio/clients
```

The command verifies every installer against its record and rejects
duplicates, unknown platforms and path-like names. Start `studio-team` with
`--client-root` pointing at that directory, outside the workspace. A damaged
manifest or a missing or truncated installer makes the update endpoint report
`client_packages_unavailable` instead of offering a partial set.

## Profile workflow

1. Create a Profile with its name, group, tags, notes and target desktop OS.
2. Choose a locale, optional timezone, proxy and startup URLs. Leave the
   timezone blank to use the proxy's exit timezone, or the host timezone when
   no proxy is selected.
3. Start the Profile. The first start generates its identity using the existing
   Camoufox launcher and saves it. Later starts reuse that complete identity.
4. Select several rows for batch start, stop or archive. The configurable
   concurrency limit includes profiles that are still starting or stopping.
5. Stop a Profile before editing it, changing its proxy, resetting its identity,
   or archiving it. Archived profiles retain their browser data and can be restored.

To permanently delete profiles, select one or more rows and click **删除** (Delete).
This also works in the archived view. The confirmation defaults to Cancel and shows
up to five selected names plus the remaining count. **同时删除浏览器数据** is checked by default:
it removes the selected profiles' Cookie, history and website storage directories.
Uncheck it to remove only the profile records and retain their directories for
manual recovery. Shared proxies and credentials remain in the proxy library.

Deletion runs in the background and locks the affected profiles against launching
or editing until it finishes. A failed directory removal retains the profile record
and reports the failure; files already removed cannot be restored. Data removal
rejects profile or ancestor directory links and does not follow links inside a
profile. The store API preserves its metadata-only default; pass
`delete_profile(profile_id, remove_data=True)` only after confirming data removal.

Profiles use separate persistent Firefox directories. Cookies, local storage,
IndexedDB, browser preferences and other profile data remain in those directories
across launches. Closing a browser window updates its status and releases its
running slot. Stopping the team service closes the browsers it owns; quitting
the desktop client closes its local browsers and returns their data. Both wait
for their Playwright drivers to finish.

Studio uses the native Firefox tab bar, including with older Camoufox releases
whose minimal theme treated tabs as window drag regions. Tabs select with one
click; double-clicking a tab does not maximize the window. This is applied at
each launch through `disableTheming`, without modifying the installed browser
or resetting saved identities. After updating Studio, restart the service or
client and relaunch your profiles to apply the change.

**复制** clones configuration into a new Profile. It creates a new identity on
first launch and starts with an empty browser directory. It does not duplicate
cookies or logged-in accounts. Changing OS, locale or advanced fingerprint options
invalidates the saved identity; editing a name, tags, notes or proxy does not. Resetting the identity is
explicit and leaves the existing browser data in place.

A team profile records an exact kernel ID such as
`official/152.0.4-beta.31`; new identities retain that ID as well as the Firefox
version. Launching resolves that build independently of global activation and
ignores the team's old executable-path setting. A missing or ambiguous build
fails explicitly. Historical identities match their recorded Firefox version
only when exactly one installed build qualifies. Changing the new-profile
default does not move existing profiles. Selecting another kernel in the editor
resets identity while preserving website data. This does not guarantee that different devices produce
different canvas hashes; Studio follows Camoufox's recorded device distributions
and does not introduce canvas noise.

## Run without browser windows

Select one or more stopped profiles and click **无头启动** (Headless start).
The override applies only to this session; it does not change the profile's saved
launch preference. The status column shows **无头 · 启动中**, **无头 · 运行中** or
**无头 · 停止中**. Use **停止** to close background sessions and release their slots.
Stopping their owner also closes background browsers and their node processes.

To make headless mode the default for a profile, open **编辑 → 启动行为**, enable
**默认无头启动**, save, then use **启动**. Uncheck it to restore visible launches.
The launch-behavior checkboxes use a green box with a dark check mark when selected.
Click the box or its label, or focus it and press Space, to toggle humanized mouse
movement, image blocking or default headless mode. Saving retains the selections
when creating or reopening a profile.
Stop the profile before switching modes. Startup URLs still load, and both modes
share the profile's persistent cookies, site storage, saved identity and proxy binding.
Cookie import/export remains available while a headless session runs.

The implementation uses Playwright's
[persistent-context headless option](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context-option-headless).
It controls browser windows only. A running status means the browser is ready,
not that every startup page or website feature succeeded. Launch failures appear
in the profile's status; stop and launch with windows when interactive
inspection is needed.

## Advanced fingerprint controls

The Profile editor's advanced tab exposes these effective browser settings:

| Control | Choices |
| --- | --- |
| Resolution | Saved generated screen, native display, or constrained custom width and height |
| Fonts | Saved generated font set or native fonts |
| WebGL information | Saved recorded device, native GPU, or a recorded vendor/renderer pair |
| WebGPU | Disabled, or native behavior together with native WebGL |
| AudioContext, speech voices, media devices | Saved generated values or native behavior |
| Hardware concurrency | Automatic or an explicit logical processor count |
| Do Not Track | On (`DNT: 1` / `"1"`) or off (no header / `"unspecified"`) |
| WebRTC | Disabled, native behavior, or replace visible addresses with the checked proxy exit |

Defaults generate a random configuration once per new Profile and keep it on
subsequent starts. Clones and explicit identity resets generate new combinations.
The workspace stores a SHA-256 digest of each generated device configuration,
excluding routing data, paths and application metadata. An atomic database claim
rejects combinations already seen in that workspace, including after profiles
are deleted or reset. Generation tries at most eight candidates before reporting
that the selected constraints need changing. Existing identities are registered
when upgrading; they are preserved even if two old profiles already match.

This is local configuration deduplication, not a promise of a globally unique
web-observable fingerprint. CPU counts, resolutions, GPU models, canvas images
and other individual values can repeat. The default CPU count follows the host's
capacity; other generated values use the existing recorded distributions. No
random machine name or UUID is used to manufacture a device difference.

Custom WebGL requires a pair recorded by fpgen for the selected OS; parameters,
extensions and shader precisions come from that record together. Unrecorded
pairs and inconsistent manual combinations fail visibly. Custom resolution is
checked after generation, so a failed constraint cannot silently use another
screen size. Native fonts, WebGL, voices and media devices require the Profile OS
to match the host. These settings can be imported on any OS, but startup checks
the actual host. Enabling native WebGPU does not guarantee a usable adapter;
hardware, drivers, the browser build and secure-context rules still apply.

Canvas, WebGL pixels and ClientRects retain the browser's actual rendering.
There are no fake controls for MAC addresses, system hostnames, deviceMemory,
TLS randomization or complete port-scan protection. See the
[parameter capability matrix](studio-capabilities.md) for the full comparison
with commercial browser settings and the relevant kernel sources.

## Proxy library

Proxies can be reused by several profiles. The library supports HTTP, HTTPS and
SOCKS5 addresses, labels, usernames and optional passwords. See [proxy configuration](studio-nodes.md).
Each startup checks the
selected proxy before opening the browser. A failed check aborts the startup.
Studio never replaces a failed proxy with a direct connection.

Authenticated SOCKS5 uses a temporary SOCKS5 listener on `127.0.0.1`, because
Playwright cannot pass SOCKS5 credentials itself. The listener forwards every
destination through the selected authenticated upstream, preserving remote DNS,
and closes all tunnels when the browser stops. Username and password must each
contain 1–255 UTF-8 bytes. Other local processes can use this ephemeral listener
while the session is running; it is not exposed on external network interfaces.
HTTP and HTTPS authentication go directly through Playwright.

Bulk import accepts one entry per line:

```text
http://127.0.0.1:8080
socks5://127.0.0.1:1080
proxy.example:8080
proxy.example:8080:username:password
http://username:password@proxy.example:8080
```

These illustrate syntax; replace them with reachable proxy endpoints. In URL
form, percent-encode special characters in credentials. Import validates the
entire batch before writing. An in-use proxy cannot be deleted.

Proxy checks reuse Camoufox's `proxy_exit_geo`: it queries ip-api.com through the
proxy, reports exit IP, timezone and request latency, and requires a successful
response. It is a reachability/exit-location check, not a proxy reputation score
or a guarantee that every destination is accessible. A provider outage or rate
limit blocks startup until the check works. The existing endpoint uses HTTP,
so an exit proxy can tamper with the location result. An explicit timezone
overrides the returned timezone. Locale is user-selected, not inferred from IP.

TigerBrowser disables WebRTC by default. For HTTP and SOCKS5 proxies, advanced options can enable native WebRTC or
replace its visible addresses with the checked proxy exit. Replacement requires
a bound, working proxy and refreshes its address each launch without changing
the saved device identity. It does not provide an additional UDP/TURN tunnel or
guarantee call connectivity. Native mode can expose host network information.
Proxy routing is supplied directly to Playwright; no page-world fingerprint
injection is added by Studio.

## Cookies and exports

While a Profile is running, import or export its Cookie JSON from the profile
actions. Import accepts a Playwright cookie array or the `cookies` member of a
Playwright `storage_state` document. It adds cookies and does not replace local
storage. Files larger than 10 MB are rejected. Cookie exports contain login
secrets and should be handled as private files.
After import, Studio reads back the cookies and reports any rejection, such as
an invalid SameSite/Secure combination. Accepted cookies remain in the browser
if other entries were rejected; an import is not an atomic transaction.

Configuration export includes profile metadata and proxy settings. It deliberately
excludes proxy passwords,
generated identities, cookies, history and browser
directories. Import creates new IDs and remaps proxy bindings, leaving existing
profiles intact. Re-enter proxy passwords on the destination computer. This is a
configuration transfer, not a full session backup.

Back up a server workspace with [encrypted stopped snapshots](studio-backup.md);
snapshots are refused while any profile is checked out. Passwords remain in the
OS credential service and are not part of a snapshot; a restored workspace on
another computer or account requires re-entering credentials.
Browser data is protected by the operating system's user permissions; Studio
does not encrypt Firefox profile databases or Cookie exports.

## Storage and implementation

The server workspace is the directory passed to `studio-team --data-dir`; client
caches use the same layout. Its SQLite database stores profile configuration, generated device
data, proxy metadata, settings and an activity log. Browser files live under
`profiles/<UUID>/`. UUIDs and import schemas are validated, and foreign keys
prevent dangling proxy references.

The desktop client hosts the server's Vue console in QtWebEngine. Its local
bridge sends commands to one background asyncio loop in a `QThread`, and
`BrowserEngine` owns independent `AsyncCamoufox` context managers on the server
and the client alike. Generation, synchronization and network probes run outside
the GUI thread. Shared first-use fingerprint
generation is serialized; browser processes run concurrently. Launch failure and
normal shutdown both tear down their drivers. Activity entries use fixed action
codes, never exception payloads or proxy credentials.

The implementation reuses Camoufox's existing `launch_options`, configuration
validation and environment encoding. It persists only generated browser options
and device configuration, never a snapshot of the host process environment.
Proxy routing, timezone and user-controlled behavior are applied per launch.

The underlying API contracts are documented in
[Playwright's persistent-context API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context),
[Cookie API](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-cookies),
[Qt's thread documentation](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)
and [keyring's native backend documentation](https://keyring.readthedocs.io/en/latest/).

## Verification and release scope

The optional [automation API](studio-automation.md) provides authenticated
profile creation, startup and access to newly created task tabs in running
environments, through member API keys on the server or the desktop client's
loopback interface. Creation requires administrator access. Both are disabled by
default. The protocol and resource limits are documented separately and do not
require browser kernel changes.

Run the focused persistence and lifecycle tests from the repository root:

```bash
python -m pytest pythonlib/tests -q -k studio
```

The repository Python gate remains `python -m ci.run_pythonlib`. The unit tests
use temporary workspaces and injected browser/credential adapters. They check
transaction rollback, secret redaction, stable identities, proxy failures,
parallel lifecycle and Cookie transfers without a real desktop or proxy account.
Desktop client tests require the `studio` extra and use Qt's offscreen platform;
missing Qt is a failure, not a skipped test. Check-out tests run a real local
HTTP team service. Proxy bridge tests use actual local
TCP connections and verify authentication, remote DNS, relay and cleanup.

The initial Windows validation passed the Python gate (558 passed, seven
pre-existing platform skips), real-browser cookie/storage persistence and profile
isolation, HTTP and SOCKS5 authentication against local test proxies, and a native
Credential Manager round trip. Real commercial proxy services were not tested.
Advanced settings were also checked against the installed 152.0.4-beta.31 kernel:
1920×1080 and 12 logical processors reached the page, DNT agreed between the JS
getter and HTTP header in both states, restart kept the identity, and native
modes launched after all corresponding overrides were removed. WebGPU was
exposed on a trustworthy loopback page only when enabled. This release predates
the newer media label/group patches: it does not implement the generated
`mediaDevices:*Labels` / `*Groups` keys, so full synthetic device labels need a
a compatible official kernel; this repository no longer builds Firefox.
The advanced-controls Python gate passed 630 tests with seven existing platform
skips. A separate check generated 12 distinct configurations, reloaded all 12
without changes, created a distinct clone, and confirmed that reset and database
reopen did not allow a historical configuration to be claimed again.
The separate Linux-oriented `ci/tests` suite has five existing Windows path
failures, also reproduced on the unchanged baseline: manifest keys use native
backslashes, and the release test harness inserts Windows paths into Bash without
quoting. Those tests still require the Linux CI environment. These results
predate the desktop client.

The desktop client's loading, pop-up windows, downloads, clipboard and local runs
on Windows, macOS and Ubuntu still await physical acceptance, tracked in the
[upgrade design](studio-client-server.md#12-实施任务与验收). Automatic updates and
macOS/Linux packaging are implemented but still require release and installation
validation. Installer signing and macOS notarization remain unimplemented.
Cross-platform Qt code does not
substitute for testing packaged applications on each supported OS.
