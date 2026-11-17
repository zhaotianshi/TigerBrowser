import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const revision = '475ecfa5356579ef222983c7ce4619a7576a3bce';
const kasmRevision = '17265facc40ab50db5740cdf0d12c61173edafc9';
const repository = 'https://github.com/kasmtech/noVNC.git';
const directory = path.dirname(fileURLToPath(import.meta.url));
const output = path.resolve(directory, '../../frontend/studio/public/kasm-client');
const scratch = mkdtempSync(path.join(tmpdir(), 'studio-kasm-client-'));
const sources = {};
const modifiedSources = {};
const sha256 = (data) => createHash('sha256').update(data).digest('hex');

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2) throw new Error(`Upstream source changed: ${before}`);
  return source.replace(before, after);
}

function adapt(name, source) {
  if (name === 'core/rfb.js') {
    source = replaceOnce(source, 'import initializePrinterRelay from "./output/printer.js";\n', '');
    source = replaceOnce(source, 'import initializeSmartcardRelay from "./output/smartcard.js";\n', '');
    source = replaceOnce(source, '        initializePrinterRelay(this);\n        initializeSmartcardRelay(this);', '');
    source = replaceOnce(source,
      'this._supportsMultiMonitor = (typeof BroadcastChannel !== "undefined" && typeof SharedWorker !== "undefined");',
      'this._supportsMultiMonitor = false;');
    source = replaceOnce(source,
      "if (typeof RTCPeerConnection !== 'undefined' && this._isPrimaryDisplay) {",
      "if (this._useUdp && typeof RTCPeerConnection !== 'undefined' && this._isPrimaryDisplay) {");
    source = replaceOnce(source, '        this._gestures.attach(this._canvas);\n', '');
    const inputBegin = source.indexOf('        // Always grab focus on some kind of click event');
    const inputEnd = source.indexOf('        // WebRTC UDP datachannel inits', inputBegin);
    if (inputBegin < 0 || inputEnd < 0) throw new Error('Upstream input registration changed');
    source = source.slice(0, inputBegin) + source.slice(inputEnd);
    source = replaceOnce(source,
      "window.addEventListener('resize', this._eventHandlers.windowResize);",
      'this._resizeObserver = new ResizeObserver(this._eventHandlers.windowResize);\n' +
      '        this._resizeObserver.observe(this._target);');
    source = replaceOnce(source,
      "window.removeEventListener('resize', this._eventHandlers.windowResize);",
      'this._resizeObserver?.disconnect();');
  }
  if (name === 'core/display.js') {
    source = replaceOnce(source, '    _asyncRenderQPush(rect) {',
      "    _closeRect(rect) {\n" +
      "        if (rect.type === 'video_frame') rect.frame?.close();\n" +
      "        if (rect.type === 'vid' || rect.type === 'bitmap') rect.img?.close();\n" +
      "    }\n\n" +
      '    _asyncRenderQPush(rect) {\n' +
      '        if (!this._renderer) { this._closeRect(rect); return; }');
    source = replaceOnce(source,
      "                    case 'video_frame':\n                        rect.frame?.close();",
      "                    case 'video_frame':\n                    case 'vid':\n                    case 'bitmap':\n                        this._closeRect(rect);");
    source = replaceOnce(source,
      "                        if (droppedRect.type === 'video_frame') {\n                            droppedRect.frame?.close();\n                        }",
      '                        this._closeRect(droppedRect);');
    source = replaceOnce(source,
      "                if (rect.type === 'video_frame')\n                    rect.frame?.close();",
      '                this._closeRect(rect);');
    source = replaceOnce(source,
      '            }\n        }\n    }\n\n    /*\n    Clear the async frame buffer',
      '            } else {\n                this._closeRect(rect);\n            }\n        }\n    }\n\n    /*\n    Clear the async frame buffer');
    const renderBegin = source.indexOf('    _pushAsyncFrame(force=false) {');
    if (renderBegin < 0) throw new Error('Upstream frame renderer changed');
    source = source.slice(0, renderBegin) + replaceOnce(source.slice(renderBegin),
      '                }\n            }\n\n            if (this._renderer?.enableCanvasBuffer)',
      '                }\n                this._closeRect(a);\n            }\n\n            if (this._renderer?.enableCanvasBuffer)');
    source = replaceOnce(source, "import UI from '../app/ui.js';\n", '');
    source = replaceOnce(source, '                UI.registeredWindows.set(screenID, windowId);\n', '');
    source = replaceOnce(source,
      '                    const windowId = UI.registeredWindows.get(screenID);\n' +
      '                    if (windowId) {\n' +
      '                        UI.registeredWindows.delete(screenID);\n' +
      '                        UI.displayWindows.delete(windowId);\n' +
      '                    }\n', '');
    source = replaceOnce(source,
      '        this._broadcastChannel = new BroadcastChannel(`channel_${this.screenID}`);\n' +
      '        if (!this._isPrimaryDisplay) {\n' +
      "            this._broadcastChannel.addEventListener('message', this._handleSecondaryDisplayMessage.bind(this));\n" +
      '        }\n', '');
  }
  if (name === 'core/util/cursor.js') {
    source = replaceOnce(source, 'const needsFallback = !supportsCursorURIs || isTouchDevice;', 'const needsFallback = true;');
  }
  if (name === 'core/decoders/tight.js') {
    source = replaceOnce(source, "import qoiWorker from './qoi/decoder.js?worker';\n", '');
    const begin = source.indexOf('    _enableQOIWorkers() {');
    if (begin < 0 || !source.endsWith('    }\n}\n')) throw new Error('Upstream QOI worker source changed');
    source = source.slice(0, begin) + '    _enableQOIWorkers() {\n        throw new Error("QOI is disabled in the Studio JPEG/WebP client");\n    }\n}\n';
  }
  return source;
}

try {
  let sourceDirectory = process.argv[2];
  if (!sourceDirectory) {
    sourceDirectory = path.join(scratch, 'upstream');
    execFileSync('git', ['init', '--quiet', sourceDirectory], {stdio: 'inherit'});
    execFileSync('git', ['-C', sourceDirectory, 'fetch', '--quiet', '--depth=1', repository, revision], {stdio: 'inherit'});
  }
  sourceDirectory = path.resolve(sourceDirectory);
  const source = (name) => {
    const data = execFileSync('git', ['-C', sourceDirectory, 'show', `${revision}:${name}`], {maxBuffer: 16 * 1024 * 1024});
    sources[name] = sha256(data);
    return data;
  };
  const bundle = await build({
    stdin: {contents: 'export {default} from "upstream:core/rfb.js";', resolveDir: directory},
    absWorkingDir: directory,
    bundle: true,
    format: 'esm',
    platform: 'browser',
    target: ['es2020'],
    outfile: 'rfb.js',
    sourcemap: 'external',
    sourcesContent: true,
    write: false,
    metafile: true,
    minify: true,
    legalComments: 'none',
    loader: {'.bin': 'dataurl'},
    banner: {js: '/*! KasmVNC 1.5.0 matching noVNC client; MPL-2.0. License: /kasm-client/LICENSE.txt; source: /kasm-client/rfb.js.map; provenance: /kasm-client/PROVENANCE.json. */'},
    plugins: [{
      name: 'pinned-kasm-source',
      setup(builder) {
        builder.onResolve({filter: /^upstream:/}, (args) => ({path: args.path.slice(9), namespace: 'kasm'}));
        builder.onResolve({filter: /^\./, namespace: 'kasm'}, (args) => {
          let name = path.posix.normalize(path.posix.join(path.posix.dirname(args.importer), args.path.replace(/\?url$/, '')));
          if (!path.posix.extname(name)) name += '.js';
          return {path: name, namespace: 'kasm'};
        });
        builder.onResolve({filter: /^pako\//, namespace: 'kasm'}, (args) => ({path: path.join(directory, 'node_modules', args.path)}));
        builder.onLoad({filter: /.*/, namespace: 'kasm'}, (args) => {
          const original = source(args.path);
          if (!args.path.endsWith('.js')) return {contents: original, loader: 'dataurl'};
          const contents = adapt(args.path, original.toString('utf8'));
          if (contents !== original.toString('utf8')) modifiedSources[args.path] = sha256(contents);
          return {contents, loader: 'js'};
        });
      },
    }],
  });
  const bundledInputs = Object.keys(bundle.metafile.inputs);
  if (bundledInputs.some((name) => /app\/ui\.js|output\/(printer|smartcard)|decoders\/qoi/.test(name))) {
    throw new Error('Studio client unexpectedly includes an application or optional relay');
  }
  const assets = {
    'rfb.js': bundle.outputFiles.find((file) => file.path.endsWith('/rfb.js') || file.path.endsWith('\\rfb.js')).contents,
    'rfb.js.map': bundle.outputFiles.find((file) => file.path.endsWith('.map')).contents,
    'LICENSE.txt': source('LICENSE.txt'),
    'MPL-2.0.txt': source('docs/LICENSE.MPL-2.0'),
    'AUTHORS': source('AUTHORS'),
    'pako-LICENSE.txt': readFileSync(path.join(directory, 'node_modules/pako/LICENSE')),
  };
  const provenance = {
    kasmvnc: {version: '1.5.0', revision: kasmRevision, repository: 'https://github.com/kasmtech/KasmVNC'},
    client: {repository, revision, license: 'MPL-2.0'},
    build: {esbuild: '0.25.12', pako: '2.1.0', command: 'npm --prefix scripts/studio-kasm-client ci && npm --prefix scripts/studio-kasm-client run build'},
    adaptations: [
      'Isolate the decoder from application UI initialization and disable multi-monitor registration.',
      'Honor disabled WebRTC before allocating a peer connection.',
      'Do not subscribe to printer or smartcard relays.',
      'Leave DOM input and focus registration to Studio; RFB handles display traffic only.',
      'Observe the embedded display host for client-side scaling changes and disconnect the observer on teardown.',
      'Render the remote cursor with the upstream canvas overlay instead of CSP-blocked data URLs.',
      'Release decoded image frames and bitmaps after rendering, dropping, queue clearing or late delivery after disposal.',
      'Reject QOI worker activation; Studio negotiates JPEG/WebP without workers or WebAssembly.',
    ],
    sources: Object.fromEntries(Object.entries(sources).sort()),
    modifiedSources: Object.fromEntries(Object.entries(modifiedSources).sort()),
    assets: Object.fromEntries(Object.entries(assets).map(([name, data]) => [name, sha256(data)])),
  };
  mkdirSync(output, {recursive: true});
  for (const [name, data] of Object.entries(assets)) writeFileSync(path.join(output, name), data);
  writeFileSync(path.join(output, 'PROVENANCE.json'), JSON.stringify(provenance, null, 2) + '\n');
  console.log(`Built pinned KasmVNC client (${assets['rfb.js'].length} bytes)`);
} finally {
  rmSync(scratch, {recursive: true, force: true});
}
