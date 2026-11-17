// Generic UI-triggered artifact client; website-specific original selection belongs to the caller.
import { createHash } from 'node:crypto';
import { readFile, open, mkdtemp, link, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';

const [connectionFile, profileId, jobFile, output, mode = 'headed'] = process.argv.slice(2);
if (!connectionFile || !profileId || !jobFile || !output || !['headed', 'headless'].includes(mode)) {
  throw new Error('Usage: node studio-artifact-client.mjs CONNECTION PROFILE_ID JOB_JSON OUTPUT [headed|headless]');
}
const connection = JSON.parse(await readFile(connectionFile, 'utf8'));
const base = new URL(connection.baseURL);
if (connection.protocolVersion !== 1 || !['http:', 'https:'].includes(base.protocol) ||
    base.username || base.password || base.search || base.hash || base.pathname !== '/' ||
    typeof connection.token !== 'string' || connection.token.length < 32) {
  throw new Error('Invalid Studio connection file');
}
const job = JSON.parse(await readFile(jobFile, 'utf8'));
if (typeof job.pageURL !== 'string' || typeof job.selector !== 'string' ||
    !['auto', 'download', 'response'].includes(job.mode ?? 'auto')) {
  throw new Error('Job requires pageURL, selector and an optional auto/download/response mode');
}
const maxBytes = job.maxBytes ?? 128 * 1024 * 1024;
if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > 128 * 1024 * 1024) {
  throw new Error('Invalid maxBytes');
}
const operations = new AbortController();
async function request(method, path, body, { cleanup = false, timeout = 330000 } = {}) {
  const response = await fetch(base.origin + '/automation/v1' + path, {
    method, redirect: 'error',
    signal: cleanup ? AbortSignal.timeout(timeout) : AbortSignal.any([operations.signal, AbortSignal.timeout(timeout)]),
    headers: { Authorization: `Bearer ${connection.token}`,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const result = await response.json();
    throw Object.assign(new Error(result.error?.code || `HTTP ${response.status}`), { code: result.error?.code });
  }
  return response;
}
async function json(method, path, body, options) {
  return (await request(method, path, body, options)).json();
}
const listing = await json('GET', '/profiles');
if (listing.protocolVersion !== 1) throw new Error('Unsupported Studio protocol');
const caps = listing.capabilities ?? {};
const binary = caps.binaryTransferVersion === 1 && caps.downloadBinaryRead === true;
const observation = binary && caps.responseObservation === true && caps.responseBodyRead === true;
if (job.mode === 'response' && !observation) throw new Error('responseObservation unavailable; use UI download mode');
await json('POST', `/profiles/${encodeURIComponent(profileId)}/start`, { headless: mode === 'headless' });
const lease = await json('POST', '/leases', { profileId, clientName: 'artifact-client' });
const leasePath = '/leases/' + encodeURIComponent(lease.leaseId);
let heartbeatTask;
const heartbeat = setInterval(() => {
  if (!heartbeatTask) {
    heartbeatTask = json('POST', leasePath + '/heartbeat', {}, { timeout: 5000 })
      .catch(error => operations.abort(error)).finally(() => { heartbeatTask = undefined; });
  }
}, 20000);
let temporary;
try {
  const { pageId } = await json('POST', leasePath + '/pages', {});
  const callBody = (method, args = [], handleId, locator) => ({ pageId, method, args,
    ...(handleId ? { handleId } : {}), ...(locator ? { locator } : {}) });
  const call = async (method, args, handleId, locator) =>
    (await json('POST', leasePath + '/call', callBody(method, args, handleId, locator))).value;
  const readChunk = async (handle, offset) => {
    const started = performance.now();
    const response = await request('POST', leasePath + '/call', callBody('readBinary',
      [{ offset, maxBytes, chunkBytes: Math.min(caps.maxChunkBytes, 1024 * 1024), timeout: 300000 }], handle));
    const metadata = JSON.parse(response.headers.get('x-studio-transfer'));
    const declared = Number(response.headers.get('content-length'));
    if (!Number.isSafeInteger(declared) || declared < 0 || declared > 1024 * 1024) throw new Error('Invalid chunk size');
    const bytes = Buffer.from(await response.arrayBuffer());
    if (metadata?.transferVersion !== 1 || metadata.offset !== offset || metadata.readBytes !== bytes.length ||
        bytes.length !== declared || !Number.isSafeInteger(metadata.totalBytes) || metadata.totalBytes > maxBytes ||
        metadata.totalBytes < offset + bytes.length || typeof metadata.complete !== 'boolean' ||
        metadata.complete !== (offset + bytes.length === metadata.totalBytes) ||
        createHash('sha256').update(bytes).digest('hex') !== metadata.chunkSha256) {
      throw new Error('Chunk integrity check failed');
    }
    return { bytes, metadata, receiveMs: performance.now() - started };
  };
  await call('goto', [job.pageURL, { waitUntil: 'domcontentloaded' }]);
  const handles = [];
  if (observation && job.mode !== 'download') {
    handles.push({ kind: 'response', id: (await call('observeResponse', [{ ...job.request, timeout: 300000 }])).$handle });
  }
  if (job.mode !== 'response') {
    const options = { timeout: 300000 };
    if (caps.downloadEventFilter === true && job.downloadFilter) options.urlFilter = job.downloadFilter;
    handles.push({ kind: 'download', id: (await call('expectEvent', ['download', options])).$handle });
  }
  // Both acknowledgements precede the one UI action; fallback never clicks again.
  const actionStarted = performance.now();
  await call('click', [{ timeout: 30000 }], undefined, [{ method: 'locator', args: [job.selector] }]);
  const candidates = handles.map(async handle => {
    const event = await call('result', [], handle.id);
    if (handle.kind === 'response' && (event.state !== 'complete' || event.status < 200 || event.status >= 300)) {
      throw new Error('response_body_unavailable');
    }
    if (binary) return { ...handle, event, first: await readChunk(handle.id, 0) };
    const result = await call('read', [{ maxBytes }], handle.id);
    if (typeof result.base64 !== 'string' || result.base64.length > Math.ceil(maxBytes / 3) * 4) {
      throw new Error('Invalid legacy download size');
    }
    const bytes = Buffer.from(result.base64, 'base64');
    if (bytes.length > maxBytes || bytes.toString('base64') !== result.base64) throw new Error('Invalid legacy download');
    return { ...handle, bytes };
  });
  const selected = await Promise.any(candidates);
  for (const handle of handles.filter(handle => handle.id !== selected.id)) {
    try { await call('delete', [], handle.id); }
    catch (error) { if (!['handle_not_found', 'handle_expired'].includes(error.code)) throw error; }
  }
  await Promise.allSettled(candidates);
  const destination = resolve(output);
  temporary = await mkdtemp(join(dirname(destination), '.studio-artifact-'));
  const partial = join(temporary, 'bytes');
  const file = await open(partial, 'wx', 0o600);
  let offset = 0;
  const digest = createHash('sha256');
  const timings = { actionToSourceReadyMs: performance.now() - actionStarted, clientReceiveMs: 0,
    clientWriteMs: 0, observation: selected.event?.timings ?? null };
  try {
    if (!binary) {
      const writeStarted = performance.now();
      await file.writeFile(selected.bytes);
      timings.clientWriteMs += performance.now() - writeStarted;
      offset = selected.bytes.length;
      digest.update(selected.bytes);
    } else {
      let chunk = selected.first;
      const totalBytes = chunk.metadata.totalBytes;
      for (;;) {
        if (chunk.metadata.totalBytes !== totalBytes || (!chunk.metadata.complete && chunk.bytes.length === 0)) {
          throw new Error('Incomplete byte transfer');
        }
        const writeStarted = performance.now();
        await file.writeFile(chunk.bytes);
        timings.clientWriteMs += performance.now() - writeStarted;
        digest.update(chunk.bytes);
        offset += chunk.bytes.length;
        timings.clientReceiveMs += chunk.receiveMs;
        if (chunk.metadata.complete) {
          if (offset !== totalBytes || digest.copy().digest('hex') !== chunk.metadata.sha256) throw new Error('Final integrity check failed');
          timings.source = chunk.metadata.timings;
          break;
        }
        chunk = await readChunk(selected.id, offset);
      }
    }
    if (operations.signal.aborted) throw new Error('Studio authorization or connection lost');
    const syncStarted = performance.now();
    await file.sync();
    timings.clientSyncMs = performance.now() - syncStarted;
  } finally {
    await file.close();
  }
  await call('delete', [], selected.id);
  // Publish only verified bytes and refuse to overwrite an existing destination.
  await link(partial, destination);
  console.log(JSON.stringify({ source: selected.kind, bytes: offset, sha256: digest.digest('hex'), timings }));
} finally {
  clearInterval(heartbeat);
  operations.abort();
  await heartbeatTask;
  try { await json('DELETE', leasePath, undefined, { cleanup: true, timeout: 10000 }); }
  finally { if (temporary) await rm(temporary, { recursive: true, force: true }); }
}
