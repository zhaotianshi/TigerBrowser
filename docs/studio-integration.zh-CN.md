# TigerBrowser 自动化对接文档

本文面向需要使用 Studio 持久化环境的 Python、Node.js、Go、Java 等调用程序，
同时覆盖服务器同机、跨电脑调用和桌面客户端本机运行。核对日期：2026-10-23。
协议版本为 `1`，HTTP 路径前缀为 `/automation/v1`。英文协议见
[Automation API](studio-automation.md)，实现见
[`automation.py`](../pythonlib/camoufox/studio/automation.py)。

## 1. 选择调用方式

| 调用方式 | 适用场景 | 调用方拿到的对象或协议 |
| --- | --- | --- |
| Python `Camoufox` / `AsyncCamoufox` | 程序自行启动和关闭浏览器 | Playwright `Browser`；持久化模式返回 `BrowserContext` |
| Studio `/automation/v1` | 借用 Studio 保存的环境、登录和代理 | 本项目定义的 HTTP + JSON 协议，本文详细说明 |
| Studio 团队管理 API | 成员登录、授权、环境管理和远程操作 | 独立的 [团队 API](studio-team-api.md)，鉴权和生命周期不同 |

`/automation/v1` 是这个 Studio 衍生项目已有的项目级接口规范。它使用标准的
HTTP、JSON 和 Bearer 鉴权，但端点、租约和消息格式由本项目定义；它不是上游
Camoufox 的通用 REST 标准，也不是 Playwright 原生连接协议。

因此，任何支持 HTTP 的语言都可以按本文对接。客户端可封装出 `newPage()`、
`getByRole()`、`click()` 等 Playwright 风格方法，实际可用能力以第 7 节的列表为准。
项目没有在这个接口中提供完整 Playwright 对象、原生 WebSocket endpoint 或 CDP。
Playwright 的 [`connect()`](https://playwright.dev/docs/api/class-browsertype#browser-type-connect)
需要独立的 Playwright 浏览器服务；
[`connectOverCDP()`](https://playwright.dev/docs/api/class-browsertype#browser-type-connect-over-cdp)
仅支持 Chromium 系浏览器。

如果只是自行启动浏览器，安装当前仓库的 Python 包并准备好内核后，可以直接使用
下面的 SDK 示例。它创建自己的浏览器，不借用 Studio 中已打开的环境：

```python
import asyncio
from camoufox.async_api import AsyncCamoufox

async def main():
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        print(await page.title())

asyncio.run(main())
```

## 2. 启用服务

先按 [Studio 安装说明](studio.md#install-and-run)安装本项目、锁定依赖和兼容内核。
上游 PyPI 包的相同包名或版本号不能证明安装了这个 Studio 扩展。
在 Studio 中准备好环境、代理和需要复用的网站登录。

| 环境运行位置 | 启用方式 |
| --- | --- |
| 团队服务器（同机或跨电脑） | 服务器启用 `--automation-listen`，成员在 Vue“API 密钥”下载连接文件 |
| 成员电脑本机（桌面客户端签出运行） | 桌面客户端“本机 → 本地自动化接口”打开开关，按当前登录成员授权 |

两种入口都要求成员身份，使用同一协议。服务器入口的启用条件和示例见
[成员 API 密钥](studio-automation.md#team-service)，跨电脑配置见
[连接说明](studio-automation-connections.md)。客户端入口只监听成员电脑的
`127.0.0.1`，列出该成员有“本机运行”授权的环境；启动即签出到本机运行，
被服务器运行或其他设备签出时返回 HTTP 409，见
[桌面客户端接口](studio-automation.md#desktop-client)。

| 对接项 | 服务器 API 密钥入口 | 桌面客户端本机入口 |
| --- | --- | --- |
| 浏览器运行位置 | 团队服务器 | 持有签出租约的成员电脑 |
| 连接文件来源 | Vue“API 密钥”创建后下载 `automation.json` | “本机”页显示的缓存文件路径 |
| 地址 | 配置的 HTTP/HTTPS 地址，默认自动化端口 8767 | `http://127.0.0.1:随机端口` |
| 凭据期限 | 保留至到期、主动吊销、成员停用或密码重置 | 接口重启会换 Token；同时依赖当前成员登录 |
| 服务重启或显式崩溃恢复 | 原有效密钥继续使用；旧借用、页面及句柄不可复用 | 重新读取连接文件；必要时在客户端重新登录 |

从旧版本迁移时，服务器调用方应改用下载的成员密钥文件；原工作区的单 Token
发现文件不能作为服务器凭据。已有成员密钥不需要因部署更换。

调用方可通过第 5.1 节的接口显式创建新环境，再启动和借用；创建需要管理员。
没有管理窗口不代表浏览器无头；无头运行需要在启动环境时显式发送 `{"headless":true}`。
客户端自动化心跳不会延长成员登录期限；退出客户端或登录到期会关闭本机接口。
服务器密钥不依赖网页是否保持登录；授权变动即时影响可用环境，不要求更换密钥。
无成员工作区的单 Token 接口、`studio --local` 与 `studio-host` 已删除。

## 3. 发现地址与鉴权

### 可选的空闲浏览器内存治理

只有发现结果明确包含 `capabilities.browserMemoryMaintenance:true` 才调用此能力。
`GET /automation/v1/leases/:id/resources` 返回
`{version:1,sessionId,rssBytes,processCount,idle}`。RSS 是当前准确环境浏览器及子进程
的驻留内存之和，共享页可能重复计算；无法确认进程时两项内存计数都为 `null`，
不能当作 0。`sessionId` 对应一个运行实例，替换运行实例后会改变。

调用 `POST /automation/v1/leases/:id/recycle`，请求体严格为 `{sessionId:"刚读取的实例"}`。
服务端只接受原运行实例、唯一自有租约、没有任务页/句柄/执行中调用、只有一个空白
锚点页且没有人工控制的环境。正常停止准确的持久运行，等待清理完成后按原运行模式
和主世界要求启动，保留登录数据、身份和代理。维护、权限和清理失败保护均保留，
不强杀进程、不清人工页，也不重新生成业务任务。

成功返回 `{recycled:true,previousSessionId,sessionId}`，旧租约失效，调用方重新借用。
等待期间暂停旧租约心跳；服务端只在有界回收任务执行期间暂停该租约 TTL 到期，
不延长成员/API 密钥授权。明确的 HTTP 409 `profile_busy` 拒绝保留原租约和浏览器；
响应丢失或停止/启动状态不明时不得自动重复回收。此功能需要配套服务版本，
客户端显示低内存或已经释放连接不代表浏览器进程已重开。

客户端入口启用后在本机缓存中生成 `automation.json`，实际路径显示在“本机”页，格式如下：

```json
{
  "protocolVersion": 1,
  "baseURL": "http://127.0.0.1:49152",
  "token": "generated-at-startup",
  "scope": "team-member",
  "member": "operator"
}
```

这是格式示意。调用时读取实际文件；端口由操作系统分配，Token 在每次接口启动时
重新生成。`member` 是授权所用的成员名。服务器入口的连接文件只含协议版本、地址和密钥。

客户端应检查 `protocolVersion === 1`、Token 是有效字符串，以及 `baseURL` 是
不含用户名、密码、查询参数和片段的 HTTP/HTTPS 根地址。本机入口还要求带端口的
`http://127.0.0.1`；服务器入口使用下载文件中的实际主机和端口，不能强制改成回环地址
或管理网页的 8766 端口。HTTPS 正常验证证书，不能关闭验证。
允许忽略额外发现字段，不要将整个连接文件发给业务网站。
本机入口正常关闭会删除文件，崩溃可能留下旧文件；文件存在不代表接口仍可连接。
服务器下载文件不会因服务重启重写；保存到受信任目录，不加入 Git。

每次请求包含：

```http
Authorization: Bearer <automation.json 中的 token>
Content-Type: application/json
```

`Content-Type` 只在有 JSON 请求体时必须提供。HTTP 库应从 URL 自动生成匹配的
`Host` 和请求体 `Content-Length`。接口拒绝 `Origin` 请求头及
`Transfer-Encoding`，不能在网页前端用浏览器 `fetch` 直接调用，也不能使用分块上传。
禁止自动跟随重定向。接口每次响应后关闭 HTTP 连接，客户端可以按普通 HTTP 请求使用。

用 PowerShell 读取下载的服务器连接文件，或“本机”页显示的文件，并列出环境：

```powershell
$connectionFile = Read-Host 'automation.json 绝对路径'
$connection = Get-Content -LiteralPath $connectionFile -Raw | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($connection.token)" }
Invoke-RestMethod -Method Get -Uri "$($connection.baseURL)/automation/v1/profiles" -Headers $headers
```

## 4. 可运行的 Node.js 客户端

需要 Node.js 22.15 或更新版本，不需要安装 npm 依赖。保存为 `studio-client.mjs`。
程序可读取上述任一入口的连接文件、列出并启动指定环境、借用环境、创建任务标签页、打开示例网站、
读取标题，最后释放借用。心跳独立于页面操作；心跳失败会中断正在进行的客户端请求。
以下示例不会接管已有人工标签页。

```javascript
import { readFile } from 'node:fs/promises';

const [connectionFile, profileId, mode = 'headed'] = process.argv.slice(2);
if (!connectionFile || !profileId || !['headed', 'headless'].includes(mode)) {
  throw new Error('用法：node studio-client.mjs CONNECTION_FILE PROFILE_ID [headed|headless]');
}
const connection = JSON.parse(await readFile(connectionFile, 'utf8'));
const base = new URL(connection.baseURL);
if (connection.protocolVersion !== 1 || !['http:', 'https:'].includes(base.protocol) ||
    !base.hostname || base.username ||
    base.password || base.search || base.hash || base.pathname !== '/' ||
    typeof connection.token !== 'string' || connection.token.length < 32) {
  throw new Error('连接文件无效或协议版本不兼容');
}
if (connection.scope === 'team-member' &&
    (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port)) {
  throw new Error('桌面客户端本机接口必须使用文件中的回环地址');
}

async function request(method, route, body, { timeoutMs = 340000, signal } = {}) {
  const timeout = AbortSignal.timeout(timeoutMs);
  const response = await fetch(base.origin + '/automation/v1' + route, {
    method,
    redirect: 'error',
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    headers: {
      Authorization: `Bearer ${connection.token}`,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = result.error?.code;
    error.name = result.error?.name || 'Error';
    throw error;
  }
  return result;
}

const listing = await request('GET', '/profiles', undefined, { timeoutMs: 10000 });
if (listing.protocolVersion !== 1 || !Array.isArray(listing.profiles)) {
  throw new Error('服务协议版本不兼容');
}
console.table(listing.profiles);
if (!listing.profiles.some(profile => profile.id === profileId)) {
  throw new Error('所选环境不在可见环境列表中');
}
await request('POST', `/profiles/${encodeURIComponent(profileId)}/start`,
  { headless: mode === 'headless' }, { timeoutMs: 125000 });
const lease = await request('POST', '/leases',
  { profileId, clientName: 'studio-client' }, { timeoutMs: 10000 });
const leasePath = '/leases/' + encodeURIComponent(lease.leaseId);
const operations = new AbortController();
let heartbeatTask = null;
const timer = setInterval(() => {
  if (heartbeatTask) return;
  heartbeatTask = request('POST', leasePath + '/heartbeat', {}, { timeoutMs: 5000 })
    .catch(error => operations.abort(error))
    .finally(() => { heartbeatTask = null; });
}, 20000);

try {
  if (lease.profile?.id !== profileId || lease.profile?.headless !== (mode === 'headless')) {
    throw new Error('返回的环境或运行模式与请求不一致');
  }
  const { pageId } = await request('POST', leasePath + '/pages', {},
    { signal: operations.signal });
  const call = (method, args = []) => request('POST', leasePath + '/call',
    { pageId, method, args }, { signal: operations.signal });
  await call('goto', ['https://example.com', { waitUntil: 'domcontentloaded', timeout: 30000 }]);
  console.log((await call('title')).value);
} finally {
  clearInterval(timer);
  operations.abort();
  await heartbeatTask;
  await request('DELETE', leasePath, undefined, { timeoutMs: 10000 });
}
```

先用第 3 节的列表取得真实环境 ID，再运行；第一个参数是实际连接文件路径，第二个参数必须替换成列表中的 `id`。

```powershell
node ./studio-client.mjs ./automation.json PROFILE_ID
```

在最后追加 `headless` 可请求无头模式。默认请求有窗口模式。已有环境以另一种模式
运行时会明确失败；先由环境管理者停止环境，再按需要的模式启动。
客户端释放租约不会停止刚启动的环境，需要在 Studio 或团队管理入口中停止。

## 5. HTTP 端点

下面所有路径都需要加 `/automation/v1`，成功响应为 HTTP 200；除 `readBinary` 返回二进制外，其余返回 JSON。
路径中的 `:id` 是相应的环境 ID 或租约 ID。

| 方法与路径 | 请求体 | 成功响应 |
| --- | --- | --- |
| `GET /profiles` | 无 | `{protocolVersion:1,capabilities:{...},profiles:[{id,name,state,headless,mainWorldEval,leased}]}` |
| `POST /profiles` | `{name,...环境配置}` | `{profile:{id,name,state,headless,mainWorldEval}}` |
| `POST /profiles/:id/start` | `{headless?:boolean,mainWorldEval?:boolean,restartIfNeeded?:boolean}` | `{profile:{id,name,state,headless,mainWorldEval}}` |
| `POST /leases` | `{profileId,clientName?}` | `{leaseId,profile:{id,name,state,headless,mainWorldEval}}` |
| `POST /leases/:id/heartbeat` | `{}` | `{}` |
| `DELETE /leases/:id` | 无 | `{}` |
| `POST /leases/:id/pages` | `{}` | `{pageId,url}` |
| `POST /leases/:id/call` | `{pageId?,handleId?,locator?,method,args:[]}` | `{value,url?,closed?}`；`readBinary` 则返回二进制 |

环境列表的 `state` 为 `stopped`、`starting`、`running` 或 `stopping`。
运行中环境的 `headless` 表示本次会话的实际模式；停止时表示其保存的默认模式。
`leased` 表示是否已经被一个客户端借用。
`clientName` 可省略，最长 128 个字符，仅用于标识调用程序，不是鉴权或权限字段。

启动请求省略 `headless` 时按 `false` 处理。相同模式的运行会话会被复用，
不会清空数据或更换身份；并发启动请求等待同一次启动。启动与租约是两个操作，
启动成功后仍需申请租约。
本文接口没有配置修改、环境停止或任意已有标签页接管端点。

### 被动网络观察与原字节读取

`GET /profiles` 新增 `responseObservation`、`responseBodyRead`、`downloadBinaryRead`、
`downloadEventFilter` 和 `binaryTransferVersion:1` 能力。旧服务器缺少字段时继续使用已有
UI 原生下载和 Base64 `read`，不要按版本号猜测。`observeResponse` 在当前任务页登记一次性
请求观察，按方法、origin 和精确 URL/前缀筛选并跟踪重定向；先等登记成功，再点击一次。
需要兼容附件时在同一次点击前也登记 download waiter，观察失败不再次点击或生成。

响应观察和下载句柄可通过 `readBinary` 顺序读取最多 1 MiB 的块，整件最多 128 MiB。
响应头 `X-Studio-Transfer` 提供总长度、实际读取长度、MIME（未知则 null）、完成标记、
块 SHA-256 和最终 SHA-256。所有新读取必须携带所属 `pageId`。客户端校验完整后才发布文件；
`delete`、页面关闭、租约释放及句柄到期负责清理。现有主世界、成员鉴权和运行模式要求不变。

Playwright 响应体仍需完整缓冲，分块降低的是 Studio 向客户端传输的开销，不能省掉网站
准备和浏览器下载时间；大文件优先使用原生下载。协议不会判断预览图是否为原图。
完整 schema、错误码、时序边界及可运行脚本见[响应与传输协议](studio-response-transfer.md)。

### 5.1 创建新环境

发送 `POST /automation/v1/profiles`，最小请求只需要环境名称：

```json
{"name":"自动化环境"}
```

同样可以设置保存的启动偏好、语言、分组、标签和指纹策略。例如：

```json
{
  "name": "采集环境",
  "group_name": "自动化",
  "tags": ["crawler"],
  "locale": "zh-CN",
  "headless": true,
  "start_urls": ["https://example.com"],
  "fingerprint": {"screen_mode": "identity"}
}
```

成功返回 HTTP 200。下面的 ID 是格式示意，调用时使用实际响应的 ID：

```json
{
  "profile": {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "采集环境",
    "state": "stopped",
    "headless": true,
    "mainWorldEval": null
  }
}
```

| 配置字段 | 类型与规则 |
| --- | --- |
| `name` | 必填有效字符串，最多 128 字符 |
| `group_name`、`notes` | 可选字符串，分别最多 128、10000 字符 |
| `tags` | 不重复的字符串数组，最多 50 项，每项最多 64 字符 |
| `os` | `windows` / `linux` / `macos`，省略时跟随执行主机系统 |
| `locale` | 有效语言标签，默认 `en-US` |
| `timezone` | 空字符串表示自动确定，或有效 IANA 时区名称 |
| `proxy_id` | 已有代理 UUID 或 `null`；省略时为 `null`，不能传代理密码或任意代理地址 |
| `kernel` | 精确内核标识 `repository/release`，来自团队内核清单；省略时使用服务器的新环境默认内核 |
| `start_urls` | 最多 50 项；接受 `about:blank` 或不含内嵌凭据的 HTTP/HTTPS 地址 |
| `headless`、`humanize`、`block_images`、`archived` | 严格布尔值，默认分别为 `false`、`true`、`false`、`false` |
| `fingerprint` | 指纹策略对象，字段与取值见[环境配置输入](studio-team-api.md#环境配置输入) |

创建复用 Studio 的校验与默认值：未指定屏幕策略时为 `custom`、1920 × 1080，
显式策略优先。不能自行提供 `id`、固定 `identity`、Profile 路径或额外字段。
无效字段、错误类型、未知代理 ID 均返回 HTTP 400 `invalid_request`，不会写入新环境。
创建不会导入其他环境的登录信息；新环境的网站登录需要另行完成。

从仓库根目录运行的 PowerShell 创建示例：

```powershell
$connectionFile = Read-Host 'automation.json 绝对路径'
$connection = Get-Content -LiteralPath $connectionFile -Raw | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($connection.token)" }
$body = @{ name = '自动化环境'; locale = 'zh-CN'; headless = $true } | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri "$($connection.baseURL)/automation/v1/profiles" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$created.profile
```

环境创建后保持停止状态，不产生租约。按返回的 `profile.id` 调用启动接口，再申请租约。
固定身份仍由已有引擎在首次启动时生成并保存。即使创建时保存了 `headless:true`，
启动接口仍需要显式传 `{"headless":true}`；省略会请求有窗口模式。

创建要求管理员；操作员返回 HTTP 403 `admin_required`。服务器 API 密钥还必须
未限定环境范围，否则返回 HTTP 403 `access_denied`；服务维护期间返回 HTTP 409 `service_busy`。
网络团队管理也已有 `POST /team/v1/profiles`，但使用团队登录、Origin 与 CSRF 鉴权，
响应为环境元数据，不能混用两种接口的 Token 或响应格式。

本地创建端点属于 `/automation/v1` 的新增能力。旧版 Studio 即使报告协议版本 1，
也可能返回 HTTP 404 `not_found`，需要更新并重启服务后使用。

创建不是幂等操作：重复提交相同名称和配置也会创建不同 ID。请求中断可能发生在
数据库已经提交之后，不能自动重试；先查看环境列表，核对是否已经创建。

### 5.2 按需启用主世界执行

仅需要页面主世界 JavaScript 的 RPC 任务申请此能力，普通 UI 任务省略该字段：

1. 读取 `GET /profiles`，检查顶层 `capabilities.mainWorldEval === true`。
   协议仍为 `protocolVersion:1`；旧版可能没有 capabilities，不能仅凭版本号推定支持。
2. 对选定环境发送 `POST /profiles/:id/start`，例如
   `{"headless":true,"mainWorldEval":true}`。`headless` 仍按实际需要选择。
3. 确认启动响应的 `profile.mainWorldEval === true`，申请租约后再次检查租约中的同一字段。
   启动与借用之间环境可能被用户关闭；不能把上一次会话的状态当作本次状态。
4. 在任务页面使用显式 `mw:` 前缀，例如页面 `evaluate` 的
   `args:["mw:document.title"]`。没有前缀的求值仍在隔离世界执行。

`mainWorldEval` 只接受 JSON 严格布尔值，字符串、数值和 `null` 均返回 400
`invalid_request`，也不接受 `config`、`identity` 等任意启动配置透传。
`true` 表示本次启动必须开启；`false` 与省略一样表示“不要求开启”，不是关闭命令。
已有历史身份若保存了 `allowMainWorld:true`，缺省仍保留该行为；不需要手工修改身份。

列表、启动和租约响应中的 `profile.mainWorldEval` 表示实际会话的有效配置：
浏览器成功启动后为 `true` 或 `false`，已停止或尚在准备时为 `null`。停止时的 `null`
不是声称已关闭能力，也不是用户偏好。已开启的运行会话可直接复用，UI 缺省请求同样可用。

顶层 capabilities 仅表示启动接口接受此字段，不保证每个环境的内核支持它。
共享启动引擎检查该环境实际内核旁的 `properties.json`（macOS 使用应用资源目录），
要求 `allowMainWorld` 声明为布尔类型；缺失、无法读取或类型不符均返回 409
`main_world_unsupported`，不换内核、不生成或重置身份、不启动浏览器。
支持时只修改本次启动的深复制配置，不写回永久 identity，不改代理、Cookie 或权限。

已运行但未开启的环境收到 `true` 请求时返回 409 `profile_main_world_mismatch`。
并发请求等待同一次启动完成后各自校验需求，不会把未满足需求的会话当作成功。
缺省请求保留该行为，由管理者停止**这一个环境**再按需启动；不能杀进程、重启整个服务或清除工作区锁。

团队自动化接口额外声明 `capabilities.mainWorldRestart:true` 时，集成程序可在停止派单、
排空自身任务并释放租约后，显式请求 `{"mainWorldEval":true,"restartIfNeeded":true}`，
`headless` 必须与当前会话一致。已开启能力的窗口直接复用；未开启时通过原持久运行 ID
正常停止并重新启动同一环境，重新申请租约并核对能力。重开期间拒绝新的租约和竞争升级，
现存其他租约返回 `profile_leased`。请求中途取消不会丢弃正在执行的重开操作，调用方先查
实际会话再继续，不盲目重试。权限、维护、签出独占和清理屏障继续生效，不能用于未结束或
状态不明的任务。窗口及人工标签会关闭，已保存身份、代理和网站资料保留，未保存页面内容
可能丢失。`restartIfNeeded` 必须是布尔值，true 必须配合 `mainWorldEval:true`；省略或 false
不会重开运行环境。本机签出接口不声明此扩展，旧版调用方不受影响。

服务器与本机签出入口语义一致，原成员授权、持久运行归属、签出独占与故障恢复屏障不变。
本功能不依赖外部维护脚本或强制 native-attestation；原生验收仍是可选诊断。
旧版安装需要按正常维护窗口升级相应服务或桌面客户端，但升级后的每次按需启用无需重启服务。
主世界代码可被页面观察，不应对所有任务默认开启；返回值需能 JSON 序列化，不能返回元素句柄。
上游语义见 [Main World Execution](https://camoufox.com/python/main-world-eval/)。

## 6. 租约、并发和断线

正常流程为：读取连接文件 → 列出或创建环境 → 按需启动 → 申请借用 → 创建任务页面 →
调用页面或定位器 → 释放租约。

这里的自动化借用使用 `leaseId`、90 秒心跳期限；桌面签出使用独立的
`checkout_id` 和令牌，心跳中断不会自动归还环境。两者不能混用，签出归还契约见
[团队 API](studio-team-api.md#86-签出租约本机运行)。服务器不能启动已签出到客户端的环境。

- 每个环境同一时间只有一个租约。需要同一环境内并发时，在一个客户端租约下创建多个任务页面。
- 每个租约最多 8 个未关闭页面、32 个未释放事件句柄。
- 每 20–30 秒独立发送心跳。租约 90 秒未续期就失效，每 5 秒清理一次过期资源。
- 普通页面调用不会替代心跳；长时间等待页面、上传或下载时也必须续期。
- 页面是客户端新建的任务标签页，共享该环境的 Cookie、存储、代理和固定身份。
- 正常释放租约会清理客户端的页面、监听器和临时下载；人工页面及浏览器继续由 Studio 管理。
- 人工关闭某个任务页面会使它的页面 ID 和句柄失效，其他页面和租约仍可使用。
- 关闭浏览器最后一个原生标签页会结束运行；等待清理完成后可再次启动同一环境，
  网站数据和固定身份保留。旧借用、页面和句柄失效，需要重新申请。
- 环境关闭、接口关闭、服务退出或成员访问权失效会使租约不可用。
- 传输中断时服务可能已经释放该租约，重连后重新读取连接文件并申请新的租约和页面。

调用中断不能证明点击、提交或启动尚未发生。不要自动重放有副作用的业务操作；
先由业务层核对实际状态。启动请求被取消也不会自动关闭已经启动的环境。
停止团队服务或退出桌面客户端会关闭其管理的浏览器，这与调用方释放租约不同。

## 7. 页面、定位器和上下文调用

调用使用 `POST /leases/:id/call`。有 `pageId` 时返回最新的 `url`、`closed`；
业务结果在 `value` 中。JavaScript 风格的选项对象放在位置参数之后，超时单位为毫秒。

打开页面的消息示例；`pageId` 必须取自创建页面的实际响应：

```json
{
  "pageId": "page-id",
  "method": "goto",
  "args": ["https://example.com", {"waitUntil": "domcontentloaded", "timeout": 30000}]
}
```

`goto` 的 `value` 为 `null`，不会返回完整 Playwright `Response`。

| 目标 | 支持的方法 |
| --- | --- |
| 页面 | `goto`、`waitForLoadState`、`evaluate`、`title`、`content`、`close`、`keyboard.press`、`mouse.click`、`expectEvent` |
| 定位器构造步骤 | `locator`、`getByRole`、`getByText`、`getByLabel`、`getByPlaceholder`、`getByTestId`、`filter`、`first`、`last`、`nth`、`and`、`or` |
| 定位器操作 | `click`、`dblclick`、`fill`、`press`、`hover`、`scrollIntoViewIfNeeded`、`waitFor`、`isVisible`、`isEnabled`、`isChecked`、`count`、`innerText`、`textContent`、`allTextContents`、`getAttribute`、`inputValue`、`evaluate`、`evaluateAll`、`setInputFiles`、`selectOption`、`check`、`uncheck` |
| 上下文，不传页面或句柄 ID | `cookies`、`addCookies` |

不支持的方法会返回错误。不要把任意 Python 属性名或全部 Playwright API 透传到此接口。
`waitForLoadState` 显式传入状态，例如 `args:["domcontentloaded",{"timeout":30000}]`。
`mouse.click` 使用主页面视口的 CSS 像素坐标，参数为
`args:[x,y,{"button":"left","clickCount":1,"delay":80,"timeout":5000}]`。
坐标必须为有限数值；选项均可省略。`delay` 是按下和松开之间的毫秒数，
`timeout` 是桥接等待上限，默认 30000 毫秒，必须为正数且不超过 330000。
超时不会自动重试点击，并会尝试松开鼠标按钮；调用方仍需检查页面结果。
`evaluate` / `evaluateAll` 传入 `[JavaScript函数或表达式字符串, 可选JSON数据]`，
结果必须能被 JSON 序列化。其数据参数原样保留键名，求值默认在 Camoufox 隔离世界执行。
DOM 操作语义可参考 Playwright 的
[Page](https://playwright.dev/python/docs/api/class-page) 和
[Locator](https://playwright.dev/python/docs/api/class-locator) 文档，桥接支持范围以本表为准。

定位器由步骤数组表示，步骤只能含 `method` 和 `args`。以下消息点击名为“提交”的按钮：

```json
{
  "pageId": "page-id",
  "locator": [{"method": "getByRole", "args": ["button", {"name": "提交", "exact": true}]}],
  "method": "click",
  "args": [{"timeout": 30000}]
}
```

链式定位器继续追加步骤；每条链最多 32 步。例如在角色定位器后追加
`{"method":"nth","args":[0]}`。嵌套定位器使用 `{"$locator":[步骤数组]}`，
用于 `filter` 的 `has` / `hasNot` 或 `and` / `or` 等参数。
正则使用 `{"$regex":"下载|Download","flags":"i"}`，允许 `i`、`m`、`s`、`u`，
实际按 Python 正则语法解析；JavaScript 正则的全部语法不保证兼容。

读取 Cookie 使用 `{"method":"cookies","args":[]}`；按地址筛选时使用
`{"method":"cookies","args":["https://example.com"]}`。
写入使用 `{"method":"addCookies","args":[Cookie对象数组]}`，Cookie 字段遵循
Playwright [BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-cookies)。
这些方法访问环境共享的 Cookie，修改会影响人工页面和其他任务页面。

## 8. 文件上传和下载

### 上传

定位器 `setInputFiles` 或文件选择器 `setFiles` 使用文件字节，不能要求 Studio
读取调用方传入的本机文件路径。每个文件最多 30 MiB，一次最多 8 个文件。
文件名不能包含目录分隔符；空数组表示清空文件选择。

```json
{
  "pageId": "page-id",
  "locator": [{"method": "locator", "args": ["input[type=file]"]}],
  "method": "setInputFiles",
  "args": [[{"$file": {"name": "sample.txt", "mimeType": "text/plain", "base64": "aGk="}}]]
}
```

### 事件顺序

下载或打开文件选择器时，必须先完成事件注册，再触发点击：

1. 在页面上调用 `expectEvent`，`args:["download",{"timeout":30000}]` 或
   `args:["filechooser",{"timeout":30000}]`。
2. 等待响应 `value:{$handle:"event-id",type:"event"}`，此时监听器已注册。
3. 使用 `handleId:"event-id"`、`method:"result"`、`args:[]` 等待事件；
   同时通过另一个 HTTP 请求执行触发事件的点击。不要等 `result` 完成后才点击。
4. `result` 返回对应句柄：下载包含 `suggestedFilename`，文件选择器包含 `multiple`。
   后续 `handleId` 使用响应中的 `$handle`。

文件选择器调用 `setFiles`，参数为 `[文件payload或数组, 可选options]`，完成后消费句柄。
事件超时默认 30 秒，必须大于 0 且不超过 300 秒。

### 下载字节

对下载句柄调用：

```json
{
  "handleId": "download-id",
  "method": "read",
  "args": [{"maxBytes": 134217728}]
}
```

响应为 `{"value":{"base64":"..."}}`。`maxBytes` 必须是 1 到 134217728
之间的整数，省略时使用 128 MiB 上限。服务等待下载完成并传输原始字节，不缩放或重编码；
客户端 Base64 解码后保存到自己的目标文件，响应不提供 Studio 的文件路径。

完成后对该句柄调用 `delete`、`args:[]`，删除临时下载并消费句柄。
`failure`、`args:[]` 返回 `null` 或经处理的失败说明。读取失败会丢弃句柄和临时文件；
释放租约也会清理剩余下载与事件监听。

## 9. 错误、超时和大小限制

错误响应为非 2xx 状态码和以下结构，客户端按 `code` 判断，不匹配自然语言消息：

```json
{"error":{"code":"profile_leased","message":"This profile is already used by an automation client.","name":"Error"}}
```

| HTTP | 常见 `code` | 客户端处理 |
| --- | --- | --- |
| 400 | `invalid_request` | 检查方法、字段、参数数目和编码 |
| 401 | `unauthorized` | 缺少 Bearer 凭据，或本机入口 Token 已更换；本机入口重读文件，服务器入口检查请求头 |
| 401 | `session_expired` | 服务器密钥过期、吊销、成员停用或密码重置；本机入口则检查客户端登录；正常部署不要求换服务器密钥 |
| 403 | `access_denied` | 检查成员当前授权、密钥限定环境和来源 IP；不能扩大密钥原有范围 |
| 403 | `forbidden` | 检查 Host、Origin 和本机调用边界 |
| 403 | `admin_required` | 团队成员桥接的创建能力要求管理员权限 |
| 404 | `profile_not_found`、`page_not_found`、`handle_not_found`、`lease_not_found`、`not_found` | 检查资源 ID、协议能力和资源生命周期 |
| 408 | `operation_timeout` | 超时；`name` 为 `TimeoutError`，核对操作是否已发生 |
| 409 | `profile_not_running` | 按需启动，再重新申请租约 |
| 409 | `profile_leased` | 等现有客户端释放；不要接管其租约 |
| 409 | `profile_mode_mismatch` | 管理者先停止环境，再切换运行模式 |
| 409 | `profile_main_world_mismatch` | 当前会话未开启主世界；等待任务及人工标签完成，仅由管理者停止此环境后按需启动 |
| 409 | `main_world_unsupported` | 当前环境实际内核未声明支持；管理员核对对应 properties.json，不自动换内核或重置身份 |
| 409 | `profile_checked_out`、`profile_busy` | 环境已在服务器运行、被另一设备签出或有维护操作，按对应入口核对归属 |
| 409 | `profile_unavailable`、`profile_start_failed`、`resource_limit`、`service_busy` | 检查运行状态、启动条件、页面/句柄数量或团队维护状态 |
| 410 | `lease_expired`、`page_closed` | 已失效资源不能复用；过期租约也可能已清理成 404 |
| 413 | `request_too_large`、`image_size_limit` | 缩小请求或下载大小；后一个代码也用于非图片下载 |
| 502 | `browser_operation_failed`、`tab_creation_failed`、`download_failed`、`invalid_result` | 检查浏览器、页面和结果序列化 |
| 503 | `service_busy` | 服务并发连接已达上限，控制请求并发 |
| 503 | `server_unreachable` | 本机接口不能确认服务器授权或完成签出，恢复服务器连接后再检查状态 |
| 504 | `profile_start_timeout` | 核对环境是否最终启动，不要推定已停止 |

| 限制 | 数值 |
| --- | --- |
| 服务操作期限 | 每个请求 330 秒；客户端 HTTP 超时建议 340 秒 |
| 环境启动等待 | 120 秒；启动请求的客户端超时建议至少 125 秒 |
| 请求体接收 / 响应发送期限 | 各 30 秒 |
| JSON 请求体 | 321 MiB，含 Base64 膨胀 |
| JSON 响应体 | 180 MiB；无法序列化或超过限制返回 `invalid_result` |
| 同时 HTTP 连接 | 最多 256 个 |

## 10. 提供给调用方的信息

向调用方提供对应入口的连接文件，以及通过环境列表取得的环境 ID。
地址、端口和 Token 由调用方从文件读取；服务器文件支持同机与跨电脑，
桌面缓存文件仅用于该成员电脑。环境登录、代理和固定身份由 Studio 管理。
调用方不需要读取 `studio.sqlite3`、直接打开 Profile 目录或依赖桌面界面的按钮坐标。

以 `/automation/v1` 和 `protocolVersion:1` 作为当前兼容边界。新增客户端围绕这份
协议封装即可，业务网站的选择器和任务逻辑放在调用方。
协议版本不支持时明确失败，不能假定相同版本号意味着已实现全部 Playwright 方法。
重连不能恢复旧的租约、页面 ID 或事件句柄。

跨电脑自动化已经由服务器成员 API 密钥入口提供。已有主世界启动能力发现字段；独立 SDK、完整能力查询及
OpenAPI 文件尚未提供；不能用网页管理 Cookie、签出令牌或 Playwright/CDP endpoint
代替此接口的 Bearer 密钥。
