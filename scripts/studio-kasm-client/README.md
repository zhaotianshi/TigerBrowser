# Studio KasmVNC client

This build packages the KasmVNC 1.5.0 matching noVNC library into one ESM asset
used by the Vue console. It does not build the upstream application.
The pinned source revision, source and output SHA-256 digests, build dependency
versions and adaptations are recorded in `frontend/studio/public/kasm-client/PROVENANCE.json`.

From the repository root, with Node.js and Git installed:

```sh
npm --prefix scripts/studio-kasm-client ci
npm --prefix scripts/studio-kasm-client run build
```

The build fetches the exact upstream commit. An existing upstream Git repository
may be supplied with `npm --prefix scripts/studio-kasm-client run build -- /path/to/noVNC`;
files are read from that commit, so working-tree changes cannot enter the artifact.
No Node.js runtime or build step is needed on the deployed Python service.

Serve `/kasm-client.js`, `/kasm-client/rfb.js` and the accompanying license and
provenance files from the packaged Vue build. Rebuild `frontend/studio` after
regenerating this decoder. The adapter's `createKasmDisplay(target, socket)` requires an open
display WebSocket. Await it before sending the controller's display-ready JSON.
Its return value has an idempotent `disconnect()` method. It forwards binary
frames to upstream RFB while leaving JSON messages and caller socket handlers intact.

The adapter uses view-only JPEG/WebP rendering, with viewport scaling and fixed
server dimensions. Native input, clipboard export, file transfers and control
leases remain Studio operations. Upstream application initialization, printer and
smartcard relays, multi-monitor registration and WebRTC allocation are excluded or
disabled. QOI activation fails explicitly; this build needs no worker, WebAssembly,
inline script, or relaxed script CSP.

DOM input and focus listeners are excluded so the library does not intercept the
console's native-input controls. Remote cursors use the upstream canvas overlay
to keep the existing `img-src 'self' blob:` policy without data-URL exceptions.
An observer tracks the embedded display host's size, including fit-mode changes
that do not resize the browser window; teardown disconnects that observer.
Decoded image frames and bitmaps are explicitly closed after rendering or
discarding them, including queue clearing and delivery after disposal. Closing
an `ImageDecoder` alone does not release the `VideoFrame` returned by `decode()`.

The upstream core is MPL-2.0 and pako is MIT/Zlib licensed. The distribution keeps
the original notices and full license texts alongside the generated artifact.
`/kasm-client/rfb.js.map` contains the exact adapted module sources and pako source
in its standard `sourcesContent` field, so recipients receive the corresponding
source form together with the executable artifact. The map is not loaded during
normal viewing.
