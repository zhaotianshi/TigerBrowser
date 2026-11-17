<div align="center">

# Camoufox Python Interface

#### Lightweight wrapper around the Playwright API to help launch Camoufox.

</div>

> [!NOTE]
> All the latest documentation is available [here](https://camoufox.com/python).

---

## What is this?

This Python library wraps around Playwright's API to help automatically generate & inject unique device characteristics (OS, CPU info, navigator, fonts, headers, screen dimensions, viewport size, WebGL, addons, etc.) into Camoufox.

It uses [fpgen](https://github.com/scrapfly/fingerprint-generator) under the hood to generate fingerprints that mimic the statistical distribution of device characteristics in real-world traffic.

In addition, it will also calculate your target geolocation, timezone, and locale to avoid proxy protection ([see demo](https://i.imgur.com/UhSHfaV.png)).

---

## Installation

First, install the `camoufox` package:

```bash
pip install -U camoufox[geoip]
```

The `geoip` parameter is optional, but heavily recommended if you are using proxies. It will download an extra dataset to determine the user's longitude, latitude, timezone, country, & locale.

The dataset is [GeoIP All-in-One](https://github.com/daijro/geoip-all-in-one), which merges several IP databases and is rebuilt weekly. Camoufox fetches the newest build and refreshes it once it is a week old.

Next, download the Camoufox browser:

```bash
camoufox fetch
```

`fetch` also installs fpgen's model, pinned by sha256, into fpgen's package directory. Otherwise the first generated fingerprint installs it. Run `fetch` as that directory's owner if the browser will run as another user, e.g. while building a Docker image.

To uninstall, run `camoufox remove`.

Both `Camoufox` and `AsyncCamoufox` treat Playwright's `TargetClosedError` during
context-manager exit as an already-closed browser. They still stop and reap the
local Playwright driver; other browser-close errors and driver teardown failures
propagate. An already-closed target does not bypass Studio's native-resource or
workspace recovery checks.

---

# Installing multiple Camoufox versions & from other repos

## TigerBrowser

**TigerBrowser** manages persistent profiles, a proxy library and team access
on top of this launcher. A Qt-free team service on one server owns the
workspace; members use the same Vue console from a web browser or from the
PySide6 desktop client on Windows, macOS or Linux. The desktop always connects
to a server: there is no standalone local workspace or separate serverless installer.
Local browser execution uses checkout and return; server workspaces and client
caches must not share a writable data directory. The standalone Python
`Camoufox` and `AsyncCamoufox` launcher APIs remain supported.

Set up a server workspace with the Qt-free `team` extra or
`team-requirements.lock`, then create its first administrator locally:

```bash
# From the repository root:
python -m pip install -r pythonlib/team-requirements.lock
python -m pip install --no-deps -e ./pythonlib
python -m camoufox studio-team-init --data-dir ./my-team-workspace --name admin --display Administrator
python -m camoufox studio-team --data-dir ./my-team-workspace
```

The terminal prompts for a hidden, confirmed password. A workspace without
members from an earlier desktop version keeps its profiles when initialized
with `--existing`. Browser execution is disabled by default; see
[the team service guide](../docs/studio-team-service.md) for headless and
Linux native remote modes, network scope, HTTPS, check-outs and the kernel cache.
Closing the remote viewer leaves the browser running. Closing the browser's last
native tab stops its run; after resource cleanup it can restart with the same
identity and saved site data, without a late viewer disconnect fencing the service.

The Ubuntu 24.04 server installer also provisions a private Secret Service session
for its non-login systemd account. A host-encrypted systemd credential unlocks the
persistent native keyring; a failed unlock or read/write probe prevents startup.
Upgrades preserve both the keyring and its unlock credential. See the
[service credential operations](../docs/studio-team-service.md#服务账号凭据) for
existing-keyring adoption and host-key/backup boundaries. Full fresh-server
deployment acceptance remains separate from the credential runtime regression.

Install the desktop client with the `studio` extra, which adds Qt with
QtWebEngine, native OS credential storage, IANA timezone data, psutil and the
shared archive-validation encryption dependency on every desktop platform:

```bash
python -m pip install -r pythonlib/studio-requirements.lock
python -m pip install --no-deps -e './pythonlib[studio]'
python -m camoufox fetch
python -m camoufox studio
```

The client asks for the server's exact `--origin`, loads its console and keeps
one cache per server in the per-user data directory. Members with the
**本机运行** grant can check a profile out, run it on their own computer with the
same engine, and return its changed files when the browser closes.

Profiles generate a recorded-device identity once and reuse it; the server's
history prevents generated combinations from repeating. Each profile pins an
exact kernel ID independently of global activation. Each start checks the
selected HTTP, HTTPS or SOCKS5 proxy. A failed proxy check stops the launch.

Applications borrow running profiles through the
[automation API](../docs/studio-automation.md): member API keys on the server's
separate `--automation-listen` port, or the desktop client's loopback interface
for profiles running on that computer. Both follow live member grants. Server
API keys survive deployment and explicit crash recovery; automation leases,
pages and handles must be reacquired. See the [Chinese integration guide](../docs/studio-integration.zh-CN.md)
for connection-file selection and an executable Node.js example.

Both automation listeners advertise `capabilities.mainWorldEval` and accept a
strict boolean `mainWorldEval` on profile start. Opt-in applies only to that launch,
after checking the profile's actual kernel schema; it never edits the saved identity
or restarts a running browser by default. The team listener can additionally advertise
`mainWorldRestart` for an explicit `restartIfNeeded:true` request after work is drained
and leases are released; normal durable stop/start and authorization checks still apply.
Start, list and lease responses report the effective
session value. See [main-world execution](../docs/studio-automation.md#opt-in-main-world-execution).

Both listeners also expose page-scoped passive response observations and bounded
binary chunks for response bodies and native downloads, with final length/SHA-256
verification and the existing Base64 download API retained. Discover capability
flags before use; register listeners before clicking and never replay a website
action on timeout. See the [transfer contract and runnable Node client](../docs/studio-response-transfer.md).

PR checks build and verify all four desktop platforms without Actions artifact
storage. Formal installers are delivered through source-bound GitHub Releases;
source updates may proceed while publication is deferred. See the
[release rules](../docs/studio-releases.md).

`camoufox studio-check --mode headless` reports static host prerequisites as
JSON without changing the browser selection or a workspace; see
[preflight and workspace locks](../docs/studio-preflight.md).
[Encrypted stopped snapshots](../docs/studio-backup.md) and
[stopped server migration](../docs/studio-migration.md) cover backup and moving
a server. [The Studio guide](../docs/studio.md) describes the workflow and data
model, the [capability matrix](../docs/studio-capabilities.md) explains
Firefox-specific limits, and the [client/server design](../docs/studio-client-server.md)
tracks what still awaits physical acceptance.

---

## CLI Manager

#### Demonstration

https://github.com/user-attachments/assets/992b1830-6b21-4024-9165-728854df1473

<details>
<summary>See help message</summary>

```
$ python -m camoufox --help

 Usage: python -m camoufox [OPTIONS] COMMAND [ARGS]...

╭─ Options ─────────────────────────────────────────────────────────────────────────────╮
│ --help  Show this message and exit.                                                   │
╰───────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ────────────────────────────────────────────────────────────────────────────╮
│ active    Print the current active version                                            │
│ fetch     Install the active version, or a specific version                           │
│ list      List Camoufox versions                                                      │
│ path      Print the install directory path                                            │
│ remove    Remove downloaded data. By default, this removes everything.                │
│           Pass --select to pick a browser version to remove.                          │
│ server    Launch a Playwright server                                                  │
│ set       Set the active Camoufox version to use & fetch.                             │
│           By default, this opens an interactive selector for versions and settings.   │
│           You can also pass a specifier to activate directly:                         │
│           Pin version:                                                                │
│               camoufox set official/stable/134.0.2-beta.20                            │
│           Automatically find latest in a channel source:                              │
│               camoufox set official/stable                                            │
│ sync      Sync available versions from remote repositories                            │
│ test      Open the Playwright inspector                                               │
│ version   Display version, package, browser, and storage info                         │
╰───────────────────────────────────────────────────────────────────────────────────────╯
```

</details>

### `sync`

Pull a list of release assets from GitHub.

```bash
> camoufox sync
Syncing repositories...
  Official... 24 versions
  CoryKing... 2 versions

Synced 26 versions from 2 repos.
```

<hr width=50>

### Which browser build is used

Each camoufox release is paired with the one browser build it was built and tested with. By default, `camoufox fetch` installs exactly that build and every launch uses it. That holds even when other builds are installed, and even when the paired build is a prerelease (a prerelease of this package pairs with a prerelease browser). Upgrading the package therefore never runs a browser it was not tested with. Run `camoufox fetch` after upgrading to install the new pairing.

Choosing a channel or a build with `camoufox set` overrides the pairing. The choice is kept, and a launch warns that the build differs from the paired one. `camoufox set --release` goes back to the paired build.

A development checkout (installed from the repository, not from PyPI) is paired with nothing and follows its channel, `official/stable` by default.

<hr width=50>

### `set`

Choose a version channel or pin a specific version, overriding the paired build. Can also be called with a specifier to activate directly.

Interactive selector:

```bash
> camoufox set
```

You can also pass a specifier to pin a specific version or choose a channel to follow directly. Following `official/stable` pulls the latest stable version from the official repo on `camoufox fetch`:

```bash
> camoufox set official/stable
```

Follow latest prerelease version from the official repo, if applicable:

```bash
> camoufox set official/prerelease
```

Pin a specific version:

```bash
> camoufox set official/stable/134.0.2-beta.20
```

Go back to the build this release is paired with:

```bash
> camoufox set --release
```

<hr width=50>

### `active`

Prints the current active version string:

```bash
> camoufox active  # A released package uses its paired build by default
official/prerelease/156.0.1-beta.33 (1a2b3c4d) (paired with this release)
```

```bash
> camoufox set coryking/stable/142.0.1-fork.26
Pinned: coryking/stable/142.0.1-fork.26
Run 'camoufox fetch' to install.

> camoufox active  # A specific version is pinned
coryking/stable/142.0.1-fork.26 (not installed)
```

<hr width=50>

### `fetch`

Install the browser build this release is paired with, or, after `camoufox set`, the latest version from the chosen channel. This will also automatically sync repository assets.

```bash
> camoufox fetch  # Install the paired build (or the latest in the chosen channel)
```

To download the latest from a different channel, or pin a version:

```bash
> camoufox set coryking/stable
> camoufox fetch  # Will download the latest release from CoryKing's repo for now on
```

Or pass in the identifier to download directly without activating it:

```bash
> camoufox fetch official/stable/135.0-beta.25   # Install a specific version
```

<hr width=50>

### `list`

List installed or all available Camoufox versions as a tree.

```bash
> camoufox list          # show installed versions
> camoufox list all      # show all available versions from synced repos
> camoufox list --path   # show full install paths
```

<hr width=50>

### `remove`

By default, removes the entire camoufox data directory.

```bash
> camoufox remove
> camoufox remove -y  # skip confirmation prompt
```

Remove a specific version:

```bash
> camoufox remove official/stable/134.0.2-beta.20
```

Interactively select a version to remove:

```bash
> camoufox remove --select
```

<hr width=50>

### `version`

Display the Python package version, active browser version, channel, and update status.

```bash
> camoufox version
Python Packages
  Camoufox                    v0.5.7
  fpgen                       v1.3.0
  Playwright                  v1.62.0
Browser
  Active                      official/stable/152.0.4-beta.31
  Current browser             v152.0.4-beta.31
  Installed                   Yes
  Latest in official/stable?  Yes
  Last Sync                   2026-03-07 00:23
GeoIP
  Database                    GeoIP AIO by daijro
  Updated                     2026-03-07 00:24
Storage
  Install path                /home/name/.cache/camoufox
  Browser(s) directory size   1.2 GB
  GeoIP database size         116.4 MB
  Config file                 /home/name/.cache/camoufox/config.json
  Repo cache                  /home/name/.cache/camoufox/repo_cache.json
```

<hr width=50>

### `path`

Print the install directory path.

```bash
> camoufox path
/home/name/.cache/camoufox
```

<hr width=50>

### `test`

Open Camoufox with the Playwright inspector for debugging.

```bash
> camoufox test
> camoufox test https://example.com
```

<hr width=50>

### `server`

Launch a remote Playwright server.

```bash
> camoufox server
```

---

## Usage

All of the latest stable documentation is available at [camoufox.com/python](https://camoufox.com/python).
