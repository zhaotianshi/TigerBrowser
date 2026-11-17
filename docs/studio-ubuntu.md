# Ubuntu Desktop

This source installation targets Ubuntu Desktop 24.04 or newer with Python 3.12+
on x64 or arm64. Availability also depends on the locked Python wheels and a
compatible upstream Camoufox browser asset for the machine's architecture.
It installs the desktop client that connects to a Studio team server; it does not
build Firefox or create a Linux portable binary.

## Install

Keep the checkout in a directory owned by your desktop user. Run the installer as
that user, without putting `sudo` before Python:

```bash
cd /path/to/TigerBrowser
python3 scripts/install-studio-ubuntu.py
```

The installer invokes `sudo` only for system packages. It installs Qt's X11/XCB
and OpenGL runtime libraries, XWayland, a D-Bus user session, GNOME Keyring, Chinese
fonts and Python's virtual-environment support. The pinned Playwright version's
`install-deps firefox` supplies its Firefox dependency list, including Ubuntu's
version-specific library package names.

Application dependencies come from `pythonlib/studio-requirements.lock` and are
installed into `<checkout>/.venv-studio`; only binary wheels are accepted. The
local `pythonlib[studio]` package is installed in editable mode. The existing
`camoufox fetch` command installs the browser, extension and verified fingerprint
model. Fresh installations use the official browser source; configured download
channels or pinned versions follow the existing downloader's rules.
An unavailable wheel or browser asset fails installation; it does not substitute a
different browser or compile an unpinned dependency.

For a machine whose system dependencies are already installed:

```bash
python3 scripts/install-studio-ubuntu.py --skip-system-deps
```

This option skips both `apt-get` and Playwright's system dependency installation.
Downloads require access to PyPI and the upstream GitHub releases. Installation
does not need a Google login, proxy password or access to an existing browser profile.

## Launch and data

Choose **TigerBrowser** in Ubuntu's application list, or run:

```bash
bash /path/to/TigerBrowser/start-studio.sh
```

The script uses its own checkout and `.venv-studio`, independent of the terminal's
working directory. It opens the desktop client, which asks for the team server's
address on first start; see [the Studio guide](studio.md#install-and-run). Use a
graphical desktop session with `DISPLAY` or `WAYLAND_DISPLAY`; an SSH shell
without a display cannot show Studio.

After `desktop-file-validate` accepts the generated entry, it is written to
`${XDG_DATA_HOME:-$HOME/.local/share}/applications/tigerbrowser.desktop`.
It quotes the checkout path, including spaces, and passes it as an argument
without evaluating a shell command. It does not place private credentials in the
entry. Reinstalling updates this user's TigerBrowser entry to the selected
checkout; move the checkout only while Studio is closed, then rerun the installer.
If a desktop launch fails, run `bash start-studio.sh` in a terminal in your desktop
session to see the Qt or dependency error.

Checked-out profiles, the device identity and local settings stay in the client
cache under `~/.local/share/TigerBrowser/servers/`, one directory per server.
Local runs with authenticated proxies require an unlocked native Secret Service,
normally GNOME Keyring in a logged-in Ubuntu desktop session. The locked Python
dependencies already include `keyring`, `secretstorage` and `jeepney`. No
plaintext fallback is installed. A locked keyring, missing session D-Bus, or a
root/SSH session may prevent a local run even when the application window opens.

Local applications use the client's loopback automation interface for profiles
running on this computer. Enable it on the **本机** page, which shows the
connection file's actual path; both applications must run in the same user
desktop session. See [local automation](studio-automation.md#desktop-client).

## Updating and removing the launcher

Close Studio and its browser windows before updating the checkout and rerunning
the installer. The installer reuses `.venv-studio` and leaves the client cache and
native credentials intact. Profiles that were not returned stay pending and are
returned the next time the client runs.

Remove the single `tigerbrowser.desktop` entry from the applications directory
above to remove the application-list shortcut. Keep the client cache while any
profile is still checked out to this computer.

## Validation status

Installer planning, error propagation, desktop-entry escaping, and the Bash
launcher's syntax and working-directory handling are tested. Existing tests cover
subprocess handling and credential refusal rules.

On 2026-10-03, native Ubuntu Desktop 24.04.4 x64 with GNOME X11, Python 3.12.3,
Qt 6.11.2 and official Camoufox 152.0.4-beta.31 passed application launch,
native keyring availability checks, profile launch and local automation tests
with the earlier standalone desktop. An isolated real-browser fixture completed
106 HTTP image requests, including 100 sequential requests using one task tab and
two concurrent requests using two task tabs. Releasing the lease preserved the
manual tab. This does not verify the current desktop client, arm64, Wayland, or
check-out runs on Ubuntu, which remain part of physical acceptance.

The upstream extension download was unavailable from the tested host; deployment
used an already downloaded copy of the same official uBlock Origin release.
Installation on a fresh machine still requires access to the runtime assets.

## Chinese glyphs in the bundled browser

System `fonts-noto-cjk` was present on the tested host, but the browser's isolated
Linux font directory lacked the CJK family names advertised by its Linux identity.
Chinese pages showed missing-glyph boxes. Installing system fonts alone did not
fix that browser cache.

For the tested browser's legacy `fonts/linux` layout, copying these four files
from `/usr/share/fonts/opentype/noto/` into that browser's `fonts/linux/` directory
restored simplified and traditional Chinese rendering:

- `NotoSansCJK-Regular.ttc`
- `NotoSansCJK-Bold.ttc`
- `NotoSerifCJK-Regular.ttc`
- `NotoSerifCJK-Bold.ttc`

Close the affected browser before changing its font files, then restart it.
Keep the correction within the Linux font directory; do not mix font sets from
other operating systems or reset profile identities. This was a browser-cache
resource correction; the source installer currently installs the system fonts.
Replacing the browser kernel may require repeating the correction for its font
layout.

Dependency references: [Qt Linux runtime requirements](https://doc.qt.io/qt-6/linux-requirements.html),
[Playwright system dependencies](https://playwright.dev/python/docs/browsers#install-system-dependencies),
[keyring Linux requirements](https://keyring.readthedocs.io/en/latest/#linux), and
[desktop Exec quoting](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html).
