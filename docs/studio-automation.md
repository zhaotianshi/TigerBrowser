# Studio automation API

For a Chinese integration guide with interface selection, endpoint and error
tables, and a Node.js client with independent heartbeats, see
[TigerBrowser 自动化对接文档](studio-integration.zh-CN.md).

The team service accepts member-owned API keys on a separate, opt-in listener.
The desktop client serves the same protocol on loopback for environments it runs
locally. Both require a member; the token-only interface for workspaces without
members was removed together with `studio-host`.
For deployment, connection files and caller configuration, see the
[same-host and cross-computer guide](studio-automation-connections.md).

## Team service

Enable the separate listener on an existing team workspace:

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace --listen 0.0.0.0 --origin http://192.168.10.6:8766 --browser-mode remote --automation-listen
```

The automation port defaults to 8767 and can be set with `--automation-port`.
It shares the web listener address, TLS certificate and direct-peer network
restrictions. An enabled browser execution mode is required. Headless services
use `--browser-mode headless`; callers request `headless:true`.

Administrators and operators create credentials in the Vue **API 密钥** page.
The one-time connection file retains protocol version 1, baseURL and token.
Only a SHA-256 digest is stored. Keys can restrict expiry, profile scope and
source IP/CIDR. Administrators manage all keys; operators manage their own.
A scoped administrator key cannot create profiles outside its fixed scope.

Every request uses MemberStore to resolve current member grants. Grant changes
take effect without restarting the service. Password resets and member
disablement revoke key sessions; enabling a member again never revives them.
A key can access only its own leases, task pages and handles. Revocation closes
owned task pages and releases leases while leaving browser runs and manual tabs
alive. Durable starts retain team capacity and native execution checks.

Network mode does not write a shared workspace discovery credential. The key
and downloaded file remain valid across service restarts until revoked or
expired, including explicit crash recovery. Recovery revokes ordinary browser
logins but preserves API-key grants, scope, expiry and existing revocation state.
Leases, pages and handles do not survive a service restart; reacquire them using
the same valid key. Query-string
credentials and Origin headers are rejected, and Host must match the configured
endpoint. HTTP transmits credentials in plaintext; use HTTPS when confidentiality
is required. HTTPS callers must validate the certificate normally.

Native Linux launches place accepted Playwright downloads in the profile's
existing `Downloads` bind, where the browser and driver see the same files.
The browser's private `/tmp` cannot be used as a driver-visible download path.
Download transfer reads the original bytes; explicit release or cleanup deletes its accepted artifact;
it does not resize or re-encode the file.

## Desktop client

The desktop client that connects to a team server offers the same protocol for
environments run on the member's computer. Enable **本机 → 本地自动化接口**; the
setting is stored only in that server's local cache. The connection file is
`automation.json` inside the cache, and the page shows its actual path.

The interface uses the member's current client login. Listing shows the
environments that member can see with the **本机运行** (`checkout`) grant.
Starting one checks it out to this computer and launches it locally, or continues
a check-out the computer already holds. An environment running on the server or
checked out to another device returns HTTP 409 `profile_checked_out` or
`profile_busy`. Creating an environment forwards to the server and requires an
administrator. Leases, task tabs and heartbeats are unchanged; the interface
never stops a browser, and closing it in the client returns its data to the
server. Logout or login expiry withdraws the interface and removes discovery.
Automation heartbeats never extend that login; the client renews its view of
the member's grants from the server every few seconds and fails closed when the
server cannot confirm them. Callers that used a workspace's `automation.json`
from an earlier desktop version must switch to the file shown on the **本机** page.

## Task tabs

### Optional idle browser memory maintenance

Team discovery advertises `capabilities.browserMemoryMaintenance:true` only when
the durable team stop/start callback is installed. Desktop clients do not
advertise this capability. It does not change protocol version 1 or authorize a
caller to reclaim an environment with tasks or manual tabs.

`GET /leases/:id/resources` returns
`{version:1,sessionId,rssBytes,processCount,idle}` under the owning lease's current
grant. `sessionId` identifies one engine session; it changes when that session is
replaced. RSS covers the exact Studio-descendant browser for that profile path
and its children. Shared resident pages may be counted more than once. Missing,
ambiguous or incomplete process measurements return `rssBytes:null` and
`processCount:null`, never a fabricated zero. A memory sample is not a host-memory
quota or proof that a task is safe to interrupt.

`POST /leases/:id/recycle` accepts exactly `{sessionId:"the observed session"}`.
The server requires the same live session, the sole owned lease, no task pages,
handles, active calls or cleanup, exactly one blank anchor tab, and no native
controller. Team maintenance, recovery and durable run ownership gates still
apply. The server fences new leases and configuration/start/stop races, repeats
the idle check at stop dispatch, normally stops the exact durable run, waits for
clean shutdown, and normally starts the same profile and running mode. Saved
identity, proxy, site data and the session's main-world requirement are retained.
It never signals an arbitrary process or forces a failed teardown to pass.

Success returns `{recycled:true,previousSessionId,sessionId}` with a different
replacement ID and invalidates the old lease. Acquire a new lease afterward.
Pause the old lease's heartbeat while awaiting this bounded operation; its TTL
does not expire during the retained recycle task, while authorization and
revocation still apply. A definite HTTP 409 `profile_busy` rejection leaves the
old run and lease usable. Stop/start uncertainty or a lost response must be
reviewed, not automatically retried. Disconnecting a request does not abandon
the retained operation or start another copy.

The callback waits at most 180 seconds. If cancellation or that deadline occurs
while an accepted durable command is still running, its profile reservation
remains until both that command and the bridge fence settle. Normal management
cannot start a replacement through the unfinished cleanup boundary.

The bridge can explicitly start a selected environment using its saved proxy,
fingerprint and browser data. It does not connect to existing tabs.
A client acquires one exclusive lease for a running
environment and creates its own tabs there. Those tabs share the environment's
cookies, storage and proxy. Closing a client tab, releasing a lease, disabling
the interface or missing heartbeats closes only the client's tabs. Studio
continues to own the browser. Closing the browser invalidates its lease.
Closing the final native tab ends that run. Once its resource cleanup finishes,
the same environment can start again with its saved identity and site data;
callers acquire a new lease and new task pages. Late viewer disconnects cannot
stop the replacement run or turn a clean exit into a recovery requirement.

Task pages open as native tabs in an existing environment window, rather than
using Juggler's `context.new_page()` operation, which creates a separate window.
The bridge opens a uniquely marked blank popup with no window features, matches
only that new page, waits for its initial document, detaches its opener and removes
the marker with `history.replaceState` to return a clean `about:blank` tab. It does
not issue a second navigation, which can interrupt Firefox's popup initialization.
Existing pages are not navigated or adopted. Concurrent clients' page requests
cannot claim unrelated popups, and releasing the lease closes only its tabs.
The environment uses Firefox's standard setting to open new pages in tabs.

## Discovery and authentication

While enabled, the client cache's `automation.json` contains:

```json
{"protocolVersion":1,"baseURL":"http://127.0.0.1:49152","token":"generated-at-startup","scope":"team-member","member":"operator"}
```

Read the actual file instead of copying this example. The operating system
chooses the port and Studio generates a fresh token on every interface start.
The file is removed on shutdown or disable; after a process crash it can remain
stale until the next launch. Reread discovery when reconnecting. Treat the file
as a credential and keep the client cache limited to trusted local users.

Requests use `Authorization: Bearer <token>`, a matching `Host` header and, for
bodies, `Content-Type: application/json`. The desktop listener binds only to `127.0.0.1`,
rejects every browser `Origin` header, accepts no cross-origin requests and
closes each HTTP connection after its response. It does not expose its token,
proxy credentials, local download paths or raw Playwright errors in responses.
The team listener uses the downloaded file's configured HTTP/HTTPS root address,
not the web-management port or a substituted loopback address. Both listeners
serve application clients and reject browser Origin headers. HTTPS clients must
verify certificates. The desktop token changes on interface restart; a server API
key does not. HTTP 401 `session_expired` on the team listener means the key or
member authorization is no longer valid, not that every deployment rotates keys.

## Leases and pages

All paths below begin with `/automation/v1`.

| Method and path | Request | Response |
| --- | --- | --- |
| `GET /profiles` | No body | `{protocolVersion:1,capabilities:{...},profiles:[{id,name,state,headless,mainWorldEval,leased}]}` |
| `POST /profiles` | `{name,...profile configuration}` | `{profile:{id,name,state,headless,mainWorldEval}}` |
| `POST /profiles/:id/start` | `{headless?:boolean,mainWorldEval?:boolean,restartIfNeeded?:boolean}` | `{profile:{id,name,state,headless,mainWorldEval}}` |
| `POST /leases` | `{profileId,clientName?}` | `{leaseId,profile:{id,name,state,headless,mainWorldEval}}` |
| `GET /leases/:id/resources` | No body | `{version:1,sessionId,rssBytes,processCount,idle}` |
| `POST /leases/:id/recycle` | `{sessionId}`; requires `browserMemoryMaintenance` capability | `{recycled:true,previousSessionId,sessionId}` |
| `POST /leases/:id/heartbeat` | `{}` | `{}` |
| `DELETE /leases/:id` | No body | `{}` |
| `POST /leases/:id/pages` | `{}` | `{pageId,url}` |
| `POST /leases/:id/call` | `{pageId?,handleId?,locator?,method,args:[]}` | `{value,url?,closed?}`, or binary bytes for `readBinary` |

A cancelled heartbeat request leaves its lease and pages intact. A later
heartbeat may renew it; normal TTL expiry still releases the abandoned lease.
Disconnecting a page operation retains the existing cancellation and owned-page
cleanup behavior.

### Creating a profile

`POST /profiles` creates a saved environment using the same `ProfileStore`
validation and defaults as Studio and the team API. A minimal request is:

```json
{"name":"Automation profile","headless":true}
```

Supported fields are `name`, `group_name`, `tags`, `notes`, `os`, `locale`,
`timezone`, `proxy_id`, `start_urls`, `headless`, `humanize`, `block_images`,
`archived`, `fingerprint` and `kernel`; their types and fingerprint policies follow the
[team configuration contract](studio-team-api.md#环境配置输入). Only `name` is
required. Unknown fields, caller-supplied IDs or identities, and missing proxy
references return HTTP 400 `invalid_request`, without creating a profile.

The response contains a fresh environment ID and `state:"stopped"`. Creation
does not start a browser, acquire a lease, copy another environment's login or
generate a fingerprint; the normal first launch generates and saves its identity.
Use the returned ID in `/profiles/:id/start`, then acquire a lease. The saved
`headless` preference does not change the start endpoint's default: send
`headless:true` explicitly to request a headless session.

Team member bridges require an administrator for creation; operators receive
HTTP 403 `admin_required`. Team service bridges reuse the team's maintenance gate
and return HTTP 409 `service_busy` while maintenance owns it. Desktop bridges use
the current login's administrator authorization and forward creation to the server.
Scoped administrator API keys receive HTTP 403 `access_denied`. Every successful creation request creates a new
ID, even with an identical name and body. A disconnect may happen after the
database commit; inspect `/profiles` before submitting another creation request.
Older protocol-version-1 builds may return HTTP 404 `not_found` for this added
endpoint; update and restart Studio before using profile creation.

### Starting and borrowing

Starting is idempotent: an existing session with the requested mode is reused,
and simultaneous requests wait for the same launch. Omit `headless` or send
`false` for a visible browser; send `true` for background headless operation.
Both modes use the same persistent profile, saved proxy and fingerprint without
changing its saved mode or clearing data. An existing session with a different
mode returns `profile_mode_mismatch`; the client must ask the user to stop it in
Studio before switching. By default, the bridge never restarts a running environment.
The request waits up to 120 seconds and reports sanitized launch errors.
Archived/missing profiles, stopping sessions and the normal Studio concurrency
limit are rejected.
Cancelling this request or releasing a lease does not close the environment.
Starting does not acquire a lease; clients must still use `POST /leases`.

### Opt-in main-world execution

Clients needing page-realm JavaScript first check that `GET /profiles` returns
`capabilities.mainWorldEval === true`, then start the selected profile with
`{"headless":true,"mainWorldEval":true}` (choose `headless` for the intended mode).
This capability describes the API, not every installed browser kernel. Older
version-1 servers may omit it; do not send the new field or assume support there.
Normal UI automation should omit `mainWorldEval`.

`mainWorldEval` accepts only JSON booleans; `null`, numbers, strings and arbitrary
configuration passthrough are rejected with HTTP 400 `invalid_request`. `true`
requires main-world execution for this launch. `false` and omission impose no
requirement and preserve the existing launch behavior, including a legacy saved
`allowMainWorld:true`; they do not disable an already enabled session.
The shared engine applies `allowMainWorld` only to its copied launch configuration,
never to the saved identity. It checks the selected environment's actual executable
and its `properties.json` (including macOS bundle resources), not the globally
active kernel. A missing, unreadable or non-boolean capability declaration fails
with HTTP 409 `main_world_unsupported` before identity generation or browser launch.
No kernel switch, identity reset or external maintenance script is performed.

The `profile.mainWorldEval` field in lists, start and lease responses is the
effective running session configuration: `true` or `false` after browser startup,
`null` when stopped or still preparing. It is not a saved preference or a claim
that a stopped browser has the feature disabled. Clients should verify it again
on the acquired lease because a browser can stop between start and borrowing.
An already enabled session is reused, including by callers omitting the field.
If a live session did not enable it, a request requiring `true` returns HTTP 409
`profile_main_world_mismatch`. Concurrent requests share the launch and each
checks its own requirement after that session is ready; an incompatible request
cannot silently succeed. Neither error stops a running browser, task or manual tab.
Ask the operator to finish existing work and stop only that environment before
requesting a fresh launch. Never restart the whole service to toggle this capability.

The team listener additionally advertises `capabilities.mainWorldRestart:true`.
After draining its work and releasing its lease, an application may explicitly
send `{"mainWorldEval":true,"restartIfNeeded":true}` with the same `headless`
mode. Both fields must be booleans and `restartIfNeeded:true` requires
`mainWorldEval:true`. An already enabled session is reused. A running session
without main-world execution is reserved against new automation leases, normally
stopped through its exact durable run ID, and started through the existing team
launch path. Any remaining lease returns `profile_leased`; a competing upgrade
or lease attempt returns `profile_busy`. Authorization, maintenance, checkout
and cleanup barriers remain enforced. A cancelled caller does not abandon the
reserved restart. Failure does not retry with weaker settings or switch kernels.
Clients must not request this operation for unfinished or uncertain work.
It closes the target browser window and its manual tabs; saved identity, site
data and proxy configuration are retained, while unsaved page contents may be lost.
Acquire a new lease and probe actual main-world execution before accepting work.
The desktop checkout bridge does not advertise this extension. Omission or false
keeps the original non-restarting contract on both listeners.

Both server API-key and desktop checkout interfaces use this contract without
changing authorization, durable run ownership, checkout exclusivity, proxy routing
or workspace/recovery barriers. Native attestation remains an optional diagnostic.
Installing this implementation still needs the normal planned application upgrade;
afterwards enabling the capability is per launch, not a service maintenance task.

### Lease lifecycle and limits

`headless` describes the actual running session, including a one-time launch
override. Send a heartbeat every 20–30 seconds, independently of long browser
calls. Leases expire after 90 seconds without a heartbeat; expired resources
are swept every five seconds. A second lease for the same profile receives HTTP
409. Each lease supports eight open pages and 32 total event/observation handles.
Manually closing a task tab removes its handles and returns `page_closed` to
subsequent calls; other tabs and the lease remain available.

These automation leases are distinct from desktop check-out leases. Automation
leases expire after missed heartbeats; a check-out never expires automatically
and must be returned or explicitly released. A server start refuses a checked-out
environment. See the [check-out contract](studio-team-api.md#86-签出租约本机运行).

Request bodies are limited to 321 MiB, including Base64 encoding. Each request
has a 330-second operation limit, so clients should use a slightly longer HTTP
timeout. Upload body receipt and response transmission each have 30-second
deadlines. HTTP errors use `{error:{code,message,name}}`; operation timeouts use
`name:"TimeoutError"`. A failed transport during a call releases that lease.
An idle client crash is detected by heartbeat expiry. At most 256 simultaneous
HTTP connections are accepted; excess connections receive HTTP 503 `service_busy`.

## Browser operations

Page methods are `goto`, `waitForLoadState`, `evaluate`, `title`, `content`,
`close`, `keyboard.press`, `mouse.click`, `expectEvent` and `observeResponse`. `goto` returns `null`. JSON calls return
the latest URL and closed state when a page ID is present. With no page or handle
ID, only context `cookies` and `addCookies` are permitted. These cookie methods
explicitly access the leased environment's shared cookies.

Arguments follow the corresponding JavaScript Playwright signatures. An options
object follows positional arguments, for example:

```json
{"pageId":"returned-page-id","method":"goto","args":["https://example.com",{"waitUntil":"domcontentloaded","timeout":30000}]}
```

`mouse.click` takes `[x,y,optionalOptions]` in main-frame viewport CSS pixels.
Coordinates must be finite numbers. Options are `button`, `clickCount`, `delay`
and the bridge's `timeout`; `delay` is the interval between down and up in
milliseconds. `timeout` defaults to 30000 milliseconds and must be positive and
at most 330000. A timeout attempts to release the held button and never retries
the click; clients must still check the page outcome. See the
[Playwright Mouse reference](https://playwright.dev/python/docs/api/class-mouse).

Locators are arrays of `{method,args}` steps. Builders are `locator`, `getByRole`,
`getByText`, `getByLabel`, `getByPlaceholder`, `getByTestId`, `filter`, `first`,
`last`, `nth`, `and` and `or`. Nest a locator as `{"$locator":[...]}`; encode a
regular expression as `{"$regex":"pattern","flags":"i"}` (`i`, `m`, `s`, `u`
are accepted; Python regular-expression syntax applies).

Locator methods are `click`, `dblclick`, `fill`, `press`, `hover`,
`scrollIntoViewIfNeeded`, `waitFor`, `isVisible`, `isEnabled`, `isChecked`, `count`,
`innerText`, `textContent`, `allTextContents`, `getAttribute`, `inputValue`,
`evaluate`, `evaluateAll`, `setInputFiles`, `selectOption`, `check` and `uncheck`.
The bridge translates supported method and option names, not arbitrary Python
attributes. Unsupported methods fail explicitly.

`evaluate` and `evaluateAll` take `[functionSourceString, optionalData]`. Data
keys are preserved without locator decoding or option-name translation.
Camoufox's default isolated evaluation world remains unchanged. See the
[Page](https://playwright.dev/python/docs/api/class-page) and
[Locator](https://playwright.dev/python/docs/api/class-locator) API references
for operation semantics.

After acquiring a lease whose `profile.mainWorldEval` is `true`, explicitly prefix
page `evaluate` source with `mw:`, for example `args:["mw:document.title"]`.
Ordinary expressions still use the isolated world. Main-world code is observable
by the page and returns JSON-serializable data, not element handles; see the
[upstream main-world contract](https://camoufox.com/python/main-world-eval/).

## Upload and download events

Upload files contain bytes, never paths on the Studio machine:

```json
{"$file":{"name":"sample.txt","mimeType":"text/plain","base64":"aGk="}}
```

`setInputFiles` and file chooser `setFiles` accept a file payload or an array of
up to eight files, with at most 30 MiB of decoded bytes per file. An empty array
clears the selection. Filenames must not contain directory components.

To catch a chooser or download reliably:

1. Call page `expectEvent` with `["download",{"timeout":30000}]` or
   `["filechooser",{"timeout":30000}]`.
2. Wait for the response `{value:{$handle:"id",type:"event"},...}`. At this point
   the listener is registered.
3. Call handle `result` with `[]` concurrently with the click that triggers it.
4. `result` returns a download handle with `suggestedFilename`, or a chooser
   handle with `multiple`.

Chooser `setFiles` accepts `[filePayloads,options]` and consumes its handle.
Download `read` accepts `[{maxBytes:134217728}]` and waits for completion before
returning `{base64:"..."}`. The hard limit is 128 MiB; the client writes the bytes
to its own destination. Call `delete` with `[]` to remove the temporary download
and consume the handle. Failed reads discard their handles and temporary files.
`failure` with `[]` returns `null` or a sanitized failure string. Lease release
cleans remaining listeners and download artifacts. Event timeout defaults to
30 seconds and must be greater than zero and no greater than 300 seconds.

### Passive observations and binary transfer

Discover `responseObservation`, `responseBodyRead`, `downloadBinaryRead`,
`downloadEventFilter` and `binaryTransferVersion` in `GET /profiles`.
`observeResponse` selects one page request by method and URL/origin filter,
follows its redirect chain, and reports completion without interception or replay.
Register a native download waiter before the same click when an attachment is
possible. Response-body availability is browser-dependent; a download handoff is
not proof that `response.body()` is readable.

Observation and download handles accept `readBinary` with mandatory owning
`pageId`, sequential offset, whole-artifact size limit, chunk size and timeout.
HTTP 200 contains binary bytes plus length, completion and SHA-256 metadata in
`X-Studio-Transfer`. `delete` also cancels pending event/observation handles.
Handles have bounded retention even while a lease receives heartbeats.
The existing Base64 `read` shape remains unchanged. See the complete
[schemas, errors, memory limits, timing and runnable Node example](studio-response-transfer.md).

An interrupted heartbeat request does not release the lease or close its task
tabs. The existing deadline still expires unless a later heartbeat succeeds.
Disconnecting an active page operation retains its normal cancellation and
lease cleanup behavior; this exception applies only to heartbeat renewal.

## Minimal Python client

Save this as `studio_client.py`, start an environment in Studio, then run
`python studio_client.py /path/to/automation.json PROFILE_ID`, using the downloaded
server file or the file shown on the desktop's **本机** page.
The example supports both listeners, uses only the Python standard library and releases its task tab in
a `finally` block. Longer clients must add the independent heartbeat above.

```python
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

connection = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
base = urlsplit(connection['baseURL'])
if (connection.get('protocolVersion') != 1 or base.scheme not in ('http', 'https')
        or not base.hostname or base.username or base.password or base.query or base.fragment
        or base.path not in ('', '/') or not isinstance(connection.get('token'), str)
        or len(connection['token']) < 32):
    raise ValueError('Invalid connection file or unsupported protocol')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

http = build_opener(NoRedirect())

def call(method, path, body=None):
    request = Request(
        connection['baseURL'].rstrip('/') + '/automation/v1' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + connection['token'],
                 'Content-Type': 'application/json'},
        method=method,
    )
    with http.open(request, timeout=340) as response:
        return json.load(response)

lease = call('POST', '/leases', {'profileId': sys.argv[2]})['leaseId']
path = '/leases/' + lease
try:
    page = call('POST', path + '/pages', {})['pageId']
    call('POST', path + '/call', {'pageId': page, 'method': 'goto',
                                 'args': ['https://example.com']})
    print(call('POST', path + '/call', {'pageId': page, 'method': 'title', 'args': []})['value'])
finally:
    call('DELETE', path)
```
