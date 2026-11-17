# Studio KasmVNC runtime

Studio's sole remote display transport uses a separately built Linux KasmVNC server.
Remote service startup requires this verified component. Building the
display server does not rebuild Firefox or change saved browser identities.

## Build and install

Use Ubuntu 24.04 x86_64 with Python 3.12 or newer. Reserve at least 2 GiB for
source/build files in addition to system build dependencies. The default two
build workers leave capacity for an existing Studio service.

From this checkout:

```bash
python3 scripts/install-studio-kasmvnc.py \
  --install-system-deps \
  --prefix "$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
```

The system dependency step uses `sudo apt-get` when run as a regular user. Omit
`--install-system-deps` when the listed build packages are already installed.
The build requires `libxtst-dev` for XTest and `mesa-common-dev` for
`GL/internal/dri_interface.h`, as well as the runtime X11/GL libraries. A
preconfigured desktop can otherwise hide these missing development headers
from a cold server installation.
Downloads honor the standard `http_proxy` and `https_proxy` environment variables.
Every archive must match the SHA256 pinned in `scripts/data/studio-kasmvnc.json`;
a changed download or corrupt cache fails the build.

libyuv is downloaded from the lemenkov GitHub mirror at the exact upstream
commit recorded in the pins. Gitiles dynamically stamps tar entries, so its
archive bytes change between downloads even when source files are identical.
Before pinning the mirror archive, all 159 file contents and executable flags
were compared with the official Google archive; the upstream commit/tree and
the mirror archive's SHA256 are recorded. Other dependencies keep their existing
source URLs and hashes. CI rejects volatile Gitiles archive URLs.

Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor. If the
production namespace command fails with `bwrap: setting up uid map: Permission
denied`, an administrator must configure the bubblewrap profile before starting
remote execution. The updated `apparmor-profiles` package provides
`/usr/share/apparmor/extra-profiles/bwrap-userns-restrict`. Review any existing
profiles attached to `/usr/bin/bwrap` before loading it; do not overwrite another
application's policy. On a fresh host without an existing attachment:

```bash
sudo apt-get install apparmor-profiles
sudo test ! -e /etc/apparmor.d/bwrap-userns-restrict
sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

This is the distribution's scoped policy: bubblewrap may create its namespace,
and its children remain restricted. The installer does not change host policy or
disable the system-wide namespace restriction. CI loads this policy on its fresh
runner and executes the production namespace command before building or restoring
the component. See [Ubuntu's namespace policy](https://documentation.ubuntu.com/release-notes/23.10/).

The destination must be a new directory. A failed build retains its `build.log`
in the cache directory shown in the error. Successful builds remove intermediate
objects, while keeping verified download archives and corresponding source.
The installer never replaces an existing runtime or restarts a Studio service.

Set this environment variable in the existing team service configuration before
starting that service:

```bash
export CAMOUFOX_KASMVNC_TOOLS="$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
```

The directory contains `Xkasmvnc`, `kasmvncpasswd`, and
`studio-kasmvnc-runtime.json`. Studio checks the supported revision markers and
both executable SHA256 digests before remote service startup. Missing or invalid
runtime components fail startup; there is no PNG selection or automatic fallback. KasmVNC's own public web listener,
desktop launcher, and host clipboard integration are not used by Studio.
Studio pins a pidfd for the display namespace's PID 1 at startup. Stopping a
session signals that exact process and waits for kernel namespace teardown before
reaping the bwrap monitor and removing the private display files. The persistence
runtime test checks that repeated start/stop cycles leave no display descendants.
The additional `kasmvncconfig` utility supports real server-configuration
regression tests; the production service does not invoke it.

Startup failures report `kasmvnc_startup_failed` with the failing stage, an OS
error number when available, and the observed child exit status. Stages distinguish
relay timeout/exit, server creation, display readiness, listener validation and
private gateway creation. Unknown status is not a successful exit. The existing
readiness pipe carries only fixed failure codes and numeric fields; raw subprocess
output, credentials, command lines and environment values are not exposed. Startup
still fails closed and retains the existing time limits and cleanup requirements.

## Source provenance and cache correction

The server base is upstream KasmVNC v1.5.0, revision
`17265facc40ab50db5740cdf0d12c61173edafc9`, with Xorg 21.1.12. The installer
copies only `common/rfb/EncCache.h`, `common/rfb/EncCache.cxx`, and the standalone
cache regression tests from revision
`a05a0984f55d4aa4de15b001f581c5015c177d78` of
[upstream pull request 372](https://github.com/kasmtech/KasmVNC/pull/372).
That proposal is not an upstream release; this build is identified separately
as `1.5.0+studio.2` in Studio's runtime manifest.

The `studio.2` loader correction opens the four FFmpeg libraries by the ABI-major
SONAMEs from their build headers. The system dynamic linker resolves these names
through its normal search and cache, without recursively enumerating `/usr/lib`
and `/usr/lib64`. The old scan made display readiness depend on unrelated directory
contents, traversal order and cold metadata caches. No alternate ABI is tried.
The installer runs the actual FFmpeg loader with directory enumeration denied,
checks the requested ABI names and tests a missing library without a fallback.
See the [dynamic loader contract](https://man7.org/linux/man-pages/man3/dlopen.3.html)
and [pinned upstream loader](https://github.com/kasmtech/KasmVNC/blob/17265facc40ab50db5740cdf0d12c61173edafc9/common/rfb/ffmpeg.cxx).

Existing `studio.1` installations must build `studio.2` into a new directory and
update `CAMOUFOX_KASMVNC_TOOLS` before deploying this source version. The old
runtime is rejected, not silently reused or replaced. The new patch has its own
`ffmpeg_loader_fix_sha256` manifest field and is retained with the source archives.
Saved browser identities and workspaces are unchanged.

[Upstream issue 371](https://github.com/kasmtech/KasmVNC/issues/371) documents
invalid map ordering, stale replacement lengths, and lost encoded-buffer
ownership when multiple viewers reuse rectangles. The proposed correction uses
lexicographic ordering, associates lengths with values, and releases replaced
and remaining buffers. The installer runs the four actual cache tests under
AddressSanitizer and LeakSanitizer before compiling the complete server.
These focused tests establish cache behavior; deployment capacity still depends
on the actual browser workload and concurrent viewers.

## Private display IPC and networking

The additional source patch `scripts/data/studio-kasmvnc-private-runtime.patch`
is SHA256-bound in the runtime manifest. It moves KasmVNC's internal
`.KasmVNCSock<PID>` listener into `/tmp/.KasmVNCSock<PID>`. Studio already gives
each display its own private `/tmp` mount. Two display PID namespaces can reuse
the same process number without colliding in the host's abstract socket namespace.
The server and its WebSocket worker share one C/C++ address helper; installer
tests exercise real filesystem binding, client transfer, and preservation of an
existing file when binding fails. No stale-file removal or host socket scanning
is added. Normal run cleanup removes only that run's private temporary directory.

The display retains the host network namespace so Xorg's `-displayfd` can
atomically claim an unused global X display, without sharing another environment’s display.
This matters on Ubuntu 24.04: its libX11/libxcb still prefers abstract sockets
for `unix/:N` and does not accept the absolute filesystem display syntax available
on newer distributions. Browser and worker mounts expose only their own display
socket and random X authority.

KasmVNC's internal HTTP/WebSocket listener binds only `127.0.0.1` on a
kernel-allocated port. Fresh per-run Basic Auth capabilities stay outside browser
mounts. Browser-facing traffic uses Studio's authenticated gateway, with RFB
input, clipboard synchronization and resizing disabled at the display server.
The pinned server's [WebSocket decoder](https://github.com/kasmtech/KasmVNC/blob/17265facc40ab50db5740cdf0d12c61173edafc9/common/network/websocket.c)
ignores non-data frames, including Ping, without sending Pong. Studio disables
protocol Ping only on this private Unix-socket upstream; otherwise the Python
client's default heartbeat disconnects a healthy display after its unanswered
Ping. Browser-facing WebSocket heartbeats, bounded writes, paired control
cleanup and native worker monitoring remain enabled. Silent static screens
are valid and do not trigger a display inactivity timeout.

The gateway combines available RFB bytes in a buffer bounded to 64 KiB before
authorizing and forwarding each batch. WebSocket message boundaries do not define
RFB packet boundaries. This preserves every byte while avoiding repeated database
queries for each tiny server frame, which can stall the private relay under
continuous pointer motion. The five-second write bounds and upstream size/rate
limits remain in force; a slow viewer still applies backpressure.

The matching client explicitly releases decoded `VideoFrame` and `ImageBitmap`
resources after rendering or discarding rectangles, including queue clearing
and late decode completion after display disposal. The adapted source and hashes
are shipped in the client's provenance and source map. Both browser families
have a regression that counts decoded and closed image resources.

The patch removes the unused UDP/WebRTC listener startup entirely: upstream
otherwise binds UDP to all interfaces even when its HTTP interface is loopback.
It also explicitly rejects RFB requests to upgrade to the disabled UDP transport,
closing only the requesting viewer. A read-only viewer cannot invoke the
uninitialized WebRTC host and terminate the display. Studio uses TCP/RFB streaming.

Codec/dependency snapshots are pinned individually in the same JSON file.
The installed `sources/` directory retains the original source archives,
installer, and lock data; licenses remain inside those archives, and the server's
GPL license is also installed beside the tools. Preserve this directory when
redistributing the runtime. Compiler and source provenance are recorded in the
runtime manifest. Binary builds are identified by their digests; the installer
does not claim bit-for-bit reproducibility across different Ubuntu toolchains.
