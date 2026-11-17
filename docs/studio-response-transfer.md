# Passive response observation and original-byte transfer

These operations extend the version-1 automation contract.
Both automation listeners use the same implementation and existing member,
API-key, session, lease and task-page checks. No browser restart, main-world
execution, cookie export or website-specific RPC is required. Studio does not
determine whether a website supplied an original or a preview: the application
must select and validate the appropriate UI action and artifact.

## Capability negotiation

Read `GET /automation/v1/profiles` before creating a task. Protocol version stays
1; additional capability fields are additive and older clients may ignore them:

```json
{
  "protocolVersion": 1,
  "capabilities": {
    "mainWorldEval": true,
    "responseObservation": true,
    "responseBodyRead": true,
    "downloadBinaryRead": true,
    "downloadEventFilter": true,
    "binaryTransferVersion": 1,
    "maxTransferBytes": 134217728,
    "maxChunkBytes": 1048576
  }
}
```

The response also includes `profiles`. Missing or false flags mean unsupported;
never infer support from a Studio version or a website build. Without these
capabilities, keep the existing `expectEvent("download")` → `result` →
`read({maxBytes})` → `delete` path. Do not retry the UI action after a timeout.
`responseBodyRead` describes API availability, not a guarantee that every response
is retained by the selected browser. No routing/interception capability is advertised.

## Register before clicking

All calls below are authenticated JSON `POST /automation/v1/leases/:leaseId/call`.
Registration returns only after the page listeners are installed:

```json
{
  "pageId": "TASK_PAGE_ID",
  "method": "observeResponse",
  "args": [{
    "origin": "https://files.example",
    "method": "GET",
    "urlPrefix": "https://files.example/images/",
    "timeout": 30000
  }]
}
```

Successful registration returns `{value:{$handle:"HANDLE_ID",type:"observation"},url,closed}`.
Schema:

| Field | Rule |
| --- | --- |
| `pageId` | Required; an open task page belonging to this lease |
| `origin` | Required HTTP(S) origin; no credentials, query or fragment; optional trailing slash |
| `method` | Required uppercase ASCII method, 1–16 letters |
| `url` | Optional exact HTTP(S) URL, including query, on that origin |
| `urlPrefix` | Optional literal URL prefix on that origin; mutually exclusive with `url` |
| `timeout` | Milliseconds, positive and at most 300000; default 30000 |

URLs are limited to 8192 characters. Unknown fields are rejected. Origin matching
uses the scheme, hostname and effective port, so a matching hostname prefix cannot
match another host. Matching is literal, without regular expressions or globbing.
The first matching request after registration is selected. Its redirects are
followed by browser request identity, including cross-origin redirects; the filter
applies to the initial request. Unrelated requests, even with the same URL, cannot
replace the selected request. At most 20 redirects are retained. Observers are
page-scoped; background worker requests not reported by Playwright on this page
are outside the contract.

Call the handle with `{pageId,handleId,method:"status",args:[]}` for a snapshot,
or `method:"result"` to wait for its terminal state. Both return the JSON envelope
`{value:observation,url,closed}`. `observation` contains:

| Field | Meaning |
| --- | --- |
| `state` | `waiting`, `requested`, `responded`, `complete`, `attachment`, `failed`, `unavailable`, `redirect_limit`, or `timed_out` |
| `requestId`, `responseId` | Opaque IDs; null until the corresponding event arrives |
| `method` | Selected request's actual method, including changes across redirects; absent before selection |
| `redirectedFrom` | Previous request ID, or null |
| `redirects` | Previous hops, each with request ID, response ID, method and status |
| `status` | HTTP status, or null before a response; an HTTP error can still have a completed body |
| `mimeType` | Response Content-Type without parameters, or null if unknown |
| `declaredBytes` | Content-Length, or null; not proof of actual body length |
| `timings` | `requestMs`, `responseMs`, `completedMs`, when available, relative to registration |

No request headers, cookies, authorization headers, post bodies, response headers
or signed request URLs are returned in this record. Existing call envelopes still
contain the task page URL; treat all API responses as private application data.
Studio adds no network-payload logging. `result` returns HTTP 408 after an
observation timeout; `status` can still report `timed_out` until handle cleanup.
Completion, failure and timeout detach listeners. `delete` cancels an observer
immediately, removes its handle and interrupts pending reads without cancelling
the website's network request.

### Attachments and download fallback

Before the same click, also register the existing `expectEvent` download waiter
when an attachment is possible. With `downloadEventFilter:true`, it accepts an
optional filter on the URL reported by the browser's download event:

```json
{
  "pageId": "TASK_PAGE_ID",
  "method": "expectEvent",
  "args": ["download", {
    "timeout": 30000,
    "urlFilter": {"origin": "https://files.example", "urlPrefix": "https://files.example/images/"}
  }]
}
```

The filter follows the same URL schema, without `method`; the download API does
not expose the request method. It matches the download event URL, which can differ
from the initial URL after redirects. The unfiltered legacy waiter remains
compatible and selects the first download on that page. Applications doing multiple
actions on one page must supply sufficiently precise filters or serialize actions.
Distinct pages have independent waiters.

An exposed `Content-Disposition: attachment` response produces `attachment`, not
a purported readable body. Firefox may instead report request failure when it
hands an attachment to the download subsystem; `failed` alone cannot distinguish
that handoff from a network error. Use the already registered download waiter to
establish whether a download actually occurred. A body may also be unavailable
after an otherwise completed response. These cases do not trigger another click,
fetch, generation or server-side replay. Delete the unused waiter after selecting
an artifact. Website-specific original/preview binding remains the caller's job.

## Binary chunks

`readBinary` works on a completed observation or an existing download handle.
Always include its owning page ID, even though legacy download methods allow an
omitted page ID:

```json
{
  "pageId": "TASK_PAGE_ID",
  "handleId": "HANDLE_ID",
  "method": "readBinary",
  "args": [{"offset": 0, "chunkBytes": 1048576, "maxBytes": 134217728, "timeout": 30000}]
}
```

Each successful call returns HTTP 200 `application/octet-stream`, a precise
`Content-Length`, `Cache-Control: no-store`, and a JSON `X-Studio-Transfer` header.
It returns no JSON envelope and performs no Base64 encoding. On errors, the
normal JSON error response remains in use. Options are bounded integers:
`offset >= 0`, `1 <= chunkBytes <= 1048576`, `1 <= maxBytes <= 134217728`.
`timeout` has the observation timeout rules. Defaults are offset 0, 1 MiB chunks,
128 MiB total and 30 seconds per call. `maxBytes` limits the whole artifact,
not the current chunk.

The metadata schema is:

```json
{
  "transferVersion": 1,
  "offset": 0,
  "readBytes": 3,
  "totalBytes": 6,
  "mimeType": "image/png",
  "complete": false,
  "sha256": null,
  "chunkSha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  "timings": {"handleWaitMs": 12.3, "sourceReadMs": 4.5, "chunkReadMs": 0.7}
}
```

Read sequentially: send `offset + readBytes` for the next call on the same handle.
Concurrent reads are rejected, as are stale offsets. `complete:true` appears only
when the cumulative byte count equals `totalBytes`; that chunk also contains the
SHA-256 of the entire transfer. Each chunk has its own digest. Verify offset,
lengths, chunk hash, final length and final hash before publishing a local file.
Digest verification detects truncation/corruption; it is not transport encryption
or independent website authenticity. Existing HTTP/TLS deployment rules remain.

Downloads wait for Playwright's completed artifact, then read at most one chunk
from disk per call. No path is exposed. Download MIME is `null` because the
Download API does not report it; Studio does not guess from a filename. Response
reads wait for `requestfinished` and use the retained browser response; they never
export authentication or issue another request. The returned entity-body bytes
are those exposed by Playwright, after HTTP content decoding, not compressed wire
bytes. Images are never rendered, resized, screenshotted or re-encoded.

**Memory boundary:** Playwright's public `response.body()` returns a whole buffer,
not a bounded stream. Studio rejects an oversized declared length before reading,
checks actual length after reading, and checks unencoded body lengths for
completeness. Unknown lengths and compressed bodies can only be checked after
the driver returns the body. Consequently `maxBytes` bounds accepted/returned
bytes, not the browser/driver's peak buffering of an untrusted response. The bridge
reserves the requested maximum before loading each response, with a 256 MiB total
reservation limit; release handles promptly. This is not a hard process-memory
limit. Prefer native download transfer for large artifacts. Do not interpret
an unexposed, truncated or oversized body as a successfully obtained original.

`delete` releases buffers/listeners and cancels/deletes the corresponding temporary
download, if any. Read failures after transfer admission discard the handle.
Invalid options, busy and offset errors leave it available. Download and chooser
handles expire at registration timeout plus 300 seconds; observation handles
expire 600 seconds after registration, regardless of heartbeats. The five-second
sweeper releases unused resources; calls reject an already expired handle.
Each lease still allows eight pages and 32 total handles. Closing a page, releasing
or expiring its lease, losing the session, revocation or disconnecting a pending
HTTP call preserves the existing cleanup and authorization boundaries. A disconnected
pending call releases the entire automation lease, including its task tabs.

## Errors and timing

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid options, missing page ID or unsupported operation |
| 401 / 403 | Existing authentication/access codes | Credential, member or transport authorization failed |
| 404 | `lease_not_found`, `page_not_found`, `handle_not_found` | Not owned by this caller/page, released, or already swept |
| 408 | `operation_timeout` | Observation or read timed out; no automatic retry of a website action |
| 409 | `profile_not_running` | The leased browser session no longer matches |
| 409 | `response_body_unavailable` | Attachment, failed/unavailable response or driver body unavailable; inspect the existing download waiter |
| 409 | `resource_limit`, `transfer_busy`, `transfer_offset` | Capacity, concurrent read or incorrect next offset |
| 410 | `page_closed`, `lease_expired`, `handle_expired` | Resource lifetime ended |
| 413 | `image_size_limit` | Whole-artifact size exceeds the chosen limit; retained for legacy compatibility |
| 502 | `body_incomplete`, `download_failed` | Incomplete body or failed browser download |

Observation `responseMs` is receipt of the browser's response-headers event, a
first-response proxy rather than a wire-level first-byte measurement.
`completedMs` records its completion/failure event, or header time for an attachment.
Transfer `handleWaitMs` measures waiting for an observation, `downloadWaitMs`
measures waiting for the browser's completed download path, `sourceReadMs` measures
retrieval of the full response buffer, and `chunkReadMs` measures the chunk read.
Fields apply only to their corresponding source. Binary transfer has no Base64
encoding phase. Measure HTTP request/receive and disk-write durations on the
client separately; those include transport and scheduling and are not pure network
time. These measurements do not promise to eliminate website preparation or
browser download time.

## Runnable Node client

[scripts/studio-artifact-client.mjs](../scripts/studio-artifact-client.mjs) needs
Node.js 22+ and no npm dependencies. It discovers capabilities, starts or reuses
the selected mode, creates its own leased page, maintains independent heartbeats,
registers waiters before one UI click, verifies chunks and publishes only the
completed file. It refuses to overwrite an existing output. It does not enable
main-world execution or stop an existing browser.

Create a private job JSON file using the actual page, selector and artifact matcher:

```json
{
  "pageURL": "https://files.example/export",
  "selector": "#download-original",
  "mode": "auto",
  "maxBytes": 134217728,
  "request": {"origin": "https://files.example", "method": "GET", "urlPrefix": "https://files.example/images/"},
  "downloadFilter": {"origin": "https://files.example", "urlPrefix": "https://files.example/images/"}
}
```

The domains and selector illustrate the schema; replace them with your site's
actual values. Keep the connection and job files outside the repository. Run from
the repository root, supplying real paths and the selected profile ID:

```powershell
node ./scripts/studio-artifact-client.mjs CONNECTION_JSON PROFILE_ID JOB_JSON OUTPUT_FILE headed
```

`mode:"auto"` races successful response-body retrieval against the already
registered native download. On older servers it uses the legacy download path.
`download` requires the native UI download; `response` requires the response
capabilities and does not silently switch sources. The response candidate requires
HTTP 2xx. Both still depend on a correct application-level artifact matcher;
neither proves that a website returned full resolution. A timeout aborts the task,
without a second click. Console output contains only source type, byte count,
digest and timings. No prompts, URLs, image contents or credentials are printed.

## Verification scope

`pythonlib/tests/test_studio_response_transfer.py` covers authenticated binary HTTP,
page/lease/key/session boundaries, filtering, cancellation, disconnect, expiry,
limits, unavailable bodies, digests and Base64 compatibility. The opt-in
`service-tester/studio_response_transfer_tests.py` uses a temporary workspace,
real headed Camoufox and a controlled loopback HTTP server. It verifies images,
attachments, redirects, delayed and truncated responses, concurrent pages,
cancellation, limits and the shipped Node client against the same bytes, including
its fallback when the server advertises only legacy capabilities.
It uses deterministic input by disabling optional humanized mouse motion only
in its synthetic profile. Missing browser/Node prerequisites fail. See
[runtime setup](../service-tester/README.md#response-observation-and-byte-transfer).
This test does not certify Linux KasmVNC downloads, a production proxy, a particular
website or original-image recognition; production acceptance remains separate.

The implementation follows the public Playwright contracts for
[request events and redirects](https://playwright.dev/python/docs/api/class-request),
[response bodies](https://playwright.dev/python/docs/api/class-response#response-body)
and [downloads](https://playwright.dev/python/docs/downloads).
