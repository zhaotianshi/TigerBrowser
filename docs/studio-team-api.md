# Studio 团队 API：管理、自动化凭据与客户端签出

核对日期：2026-10-18。本文依据 `pythonlib/camoufox/studio/team/service.py`、
`control.py`、`repository.py`、`store.py`、`access.py`、`api_keys.py`、
`checkouts.py`、`kernel_cache.py` 和 `client_releases.py` 整理。
第 1–8 节描述已实现的接口；第 8 节包含 API 密钥、客户端签出、安装包及仍要求离线的维护操作。
前端工程与同源联调见 [Vue 控制台说明](../frontend/studio/README.md)。

现有服务主动关闭了 `/docs`、`/redoc` 和 OpenAPI HTTP 地址，且多数路由使用
`Request` 手工校验 JSON。直接打开 FastAPI 文档并不能得到完整字段定义。
后续应将既有校验迁移为明确的请求/响应模型，生成与实现一致的离线 OpenAPI
及 TypeScript 类型；公开文档地址不是本次整理已经实现的功能。

## 1. 连接与通用约定

HTTP 前缀为 `/team/v1`，WebSocket 使用同一主机、端口和对应的 `ws` / `wss`。
生产前端与 API 同源提供。服务校验配置中的精确 Origin、Host 和协议，当前拒绝
所有 HTTP 与 WebSocket 查询参数。现有环境筛选发生在客户端，不是分页 API。
默认本机服务使用 HTTP 且不限客户端网段；`--private-network-only` 限制直连对端为回环/
RFC1918/ULA，可重复的 `--allow-client IP_OR_CIDR` 再施加白名单。两者并用时取交集，
HTTP 和两类 WebSocket 使用相同规则，不信任代理转发头。这些策略不提供传输加密。

登录 Cookie 由服务设置：HTTPS 使用 `__Host-camoufox-team`，HTTP 使用
`camoufox-team-local`。均为 HttpOnly、SameSite=Strict、Path=/；HTTPS 另有 Secure。
浏览器请求使用同源 Cookie，JavaScript 不读取 Cookie 值。

管理接口除登录外，HTTP 修改请求同时带登录 Cookie、精确 `Origin` 和 `/me` 返回的
`X-CSRF-Token`。普通 JSON 请求为 `Content-Type: application/json`，顶层必须为
对象，不接受未知字段。上传使用第 6 节的二进制格式。普通 JSON 请求上限 64 KiB；
Cookie 为 10 MiB，代理批量导入为 5 MiB 加 JSON 封装，配置导入为 16 MiB。
读取超时 10 秒；客户端不能把 Cookie 或 CSRF 放入 URL。

### 升级后的鉴权入口

| 入口 | 凭据与请求约定 | 运行位置 |
| --- | --- | --- |
| `/team/v1` 管理接口 | 网页登录 Cookie；修改同时要求精确 Origin 和 CSRF | 管理服务器环境、成员、配置和客户端资源 |
| 独立端口 `/automation/v1` | 下载的 `automation.json` 中的成员 API 密钥，使用 Bearer；拒绝 Origin，不使用网页 Cookie/CSRF | 服务器 |
| 桌面本机 `/automation/v1` | “本机”页缓存文件中的临时 Bearer Token；回环监听，依赖客户端当前登录 | 成员电脑上签出的环境 |
| `/team/v1/checkouts/{id}` 及子路径 | 签出响应中的专用 Bearer 租约令牌；POST 仍要求服务器精确 Origin，见第 8.6 节 | 同步唯一签出副本 |

API 密钥、网页 Cookie 和签出令牌不能互换。服务器重启、部署和显式崩溃恢复不要求
更换有效 API 密钥；显式恢复会撤销普通网页登录。自动化借用、页面和句柄不会跨重启恢复，
不能重放结果不明的操作。中文端到端示例见[自动化对接文档](studio-integration.zh-CN.md)。

| 角色 | 环境范围与操作 |
| --- | --- |
| admin | 管理全部环境、成员、成员组和授权 |
| operator | 在角色上限内，按环境的 `view` / `watch` / `operate` / `checkout` 授权操作 |
| viewer | 在角色上限内，按环境的 `view` / `watch` 授权观看 |

`view` 为环境元数据读取，`watch` 为原生画面观看，`operate` 为运行操作及控制。
`checkout` 单独允许把网站数据和代理凭据交给成员电脑本机运行，不能由 `operate` 推导。
角色限制由服务执行，成员组授权不能使 viewer 获得 operate。环境标签分组
`group_name` 与成员授权组是不同资源。

## 2. 登录会话

| 方法与路径 | 请求 JSON | 成功返回 |
| --- | --- | --- |
| `POST /login` | `name`, `password` | `member`, `expires_at`, `csrf`；设置 Cookie |
| `GET /me` | 无 | `member`, `expires_at`, `csrf`, `capabilities`, `workspace_id`（客户端按它区分各服务器的本地缓存） |
| `POST /logout` | 空对象 | `ok: true`；撤销登录及关联连接、删除 Cookie |
| `POST /password` | `current`, `password` | `ok: true`；改密后撤销登录及关联连接 |

本表及下文 HTTP 路径均省略 `/team/v1` 前缀。登录有效期为 12 小时，授权变动、
停用、密码重置立即使受影响登录失效。服务重启后必须重新读取 `/me` 获取新 CSRF，
不能复用前端内存里旧的值。登录会话不等于浏览器运行所有权。

`/me.capabilities` 当前包含：

| 字段 | 类型与含义 |
| --- | --- |
| `browser_mode` | `disabled` / `headless` / `desktop` / `remote`，本次服务的执行模式 |
| `remote_display` | boolean，是否启用原生远程画面 |
| `control_handover` | boolean，是否启用独占控制与接管 |
| `file_transfer` | boolean，是否启用受限文件交换 |
| `launch_modes` | string[]，本服务允许的逐运行启动模式 |
| `team_automation` | boolean，服务器独立的成员 API 密钥监听是否启用；不表示桌面本机接口已开启 |
| `online_maintenance` | 当前为 false；备份/恢复仍要求离线维护所有权 |
| `secure_transport` | boolean，当前页面是否使用 HTTPS/WSS |
| `private_network_only` | boolean，是否要求直接对端为回环/RFC1918/ULA |
| `client_allowlist` | boolean，是否启用了直接客户端 IP/CIDR 白名单；不返回具体条目 |
| `key_codes` | string[]，当前服务支持的物理键代码；非 remote 模式为空 |

前端功能必须同时考虑服务能力与成员权限。登录返回不含 capabilities，登录后
需要读取 `/me`。无 Qt 初始化账号属于本机 CLI，不存在公开 HTTP 初始化接口。

## 3. 环境配置与运行

| 方法与路径 | 权限 | 请求/返回 |
| --- | --- | --- |
| `GET /profiles` | 已登录 | 返回 `profiles`；包含可见的普通及归档环境 |
| `GET /profiles/{profile_id}` | admin | 完整编辑字段、时间、修订号、权限、运行摘要及身份存在标记 |
| `GET /profiles/{profile_id}/remote` | view | 脱敏环境、远程可用状态、独立窗口路径和当前运行的 WebSocket 路径；详见第 7 节 |
| `GET/POST /profiles/{profile_id}/identity` | admin | 读取持久身份；或以 `action=rebuild`, `revision` 显式重建 |
| `POST /profiles/{profile_id}/clone` | admin | `revision`；复制设置到新 ID，不复制身份与网站数据 |
| `POST /profiles/{profile_id}/delete` | admin | `request_id`, `revision`, `remove_data`；返回 HTTP 202 Task |
| `GET/POST /profiles/{profile_id}/cookies` | admin | 有效运行的 Cookie 导出/导入，请求为 `cookies` 数组 |
| `POST /profiles/batch` | 按 action | 逐环境启动、无头启动、停止、归档或恢复的独立结果 |
| `POST /profiles` | admin | 接受下表配置字段，返回环境元数据 |
| `POST /profiles/{profile_id}` | admin | `data` 为配置修改对象，`revision` 为预期正整数修订号；返回更新后元数据 |
| `POST /profiles/{profile_id}/start` | operate | `request_id`，可选 `mode`；返回 HTTP 202 的 Command |
| `POST /profiles/{profile_id}/stop` | operate | `request_id`, `run_id`；返回 HTTP 202 的 Command |
| `GET /commands/{command_id}` | 已登录且 view | 返回 Command；非管理员仅能读取自己的命令 |

### 环境配置输入

创建与修改已经复用 `ProfileStore` 验证，服务并非只能接收 Web 当前表单中的名称与系统。
修改是合并配置字段，不要求前端发送一个自行补默认值的完整环境。

| 字段 | JSON 类型 | 语义 |
| --- | --- | --- |
| `name` | string | 环境名称，最多 128 字符，必需的有效名称由存储校验 |
| `group_name` | string | 环境标签分组 |
| `tags` | string[] | 标签 |
| `notes` | string | 备注 |
| `os` | string | `windows` / `linux` / `macos` |
| `locale` | string | 由既有语言校验器验证的语言标签 |
| `timezone` | string | 空值自动确定，或有效 IANA 时区 |
| `proxy_id` | UUID string 或 null | 已有代理 ID，null 为直连 |
| `kernel` | string | 精确内核标识 `repository/release`；创建时省略则使用新环境默认，修改为另一内核会重建身份并保留网站数据 |
| `start_urls` | string[] | 启动地址，复用既有 URL 校验 |
| `headless` | boolean | 环境保存的启动偏好；团队服务实际模式另见下文 |
| `humanize` | boolean | 拟人化输入设置 |
| `block_images` | boolean | 禁止加载图片设置 |
| `archived` | boolean | 归档状态；可以通过当前修改接口归档/恢复 |
| `fingerprint` | object | 下表指纹策略，复用 `fingerprint_options.py` |

| fingerprint 字段 | 当前可接受值 |
| --- | --- |
| `screen_mode` | `identity`, `native`, `custom` |
| `screen_width`, `screen_height` | 整数：320–16384、240–16384；原生远程容量另限于 640–4096 × 480–2160 |
| `fonts_mode` | `identity`, `native` |
| `webgl_mode` | `identity`, `native`, `custom` |
| `webgl_vendor`, `webgl_renderer` | 字符串；自定义组合须来自目标系统的已有实测数据 |
| `webgpu_mode` | `disabled`, `native`；native 要求真实 WebGL |
| `audio_mode`, `voices_mode`, `media_mode` | `identity`, `native` |
| `webrtc_mode` | `disabled`, `native`, `proxy`；proxy 要求有效代理 |
| `hardware_concurrency` | 0–256 的整数；0 跟随生成身份 |
| `do_not_track` | boolean |

创建环境时，未指定的屏幕策略默认为 `custom`、1920 × 1080；显式指定的策略和尺寸优先。
修改、复制及导入环境沿用其原屏幕配置，旧环境读取仍保留原有默认策略。

Web 中的 native 指执行机的真实能力，不是访问网页的成员电脑。真实字体、WebGL、
语音及媒体设备模式仍须满足既有操作系统匹配规则。
修改 `os`、`locale` 或 `fingerprint` 会使旧身份失效，下次启动生成新身份，保留网站数据；
前端必须在保存前明确表达这个效果。普通界面缩放不能调用此修改接口。

### 环境读取模型

环境元数据字段为 `id`, `name`, `group_name`, `tags`, `os`, `archived`, `revision`。
列表另含 `locale`, `headless`, `last_launch_at`, `proxy`, `permissions` 及 `run`。非管理员的
`proxy` 只含名称；管理员另可见代理 ID 和不含凭据的 server。`run` 为 null 或包含
`id`, `state`, `mode`, `error`, `updated_at` 的对象。新建的 remote 运行另含
`display_backend: "kasmvnc"`。历史运行记录保留原值供审计，不用于选择新运行的传输。

`GET /profiles/{profile_id}` 返回一个平铺对象：上述元数据、`permissions`、`run`、
全部配置输入字段，以及以下只读字段。

| 字段 | 类型与含义 |
| --- | --- |
| `created_at`, `updated_at` | UTC ISO 8601 string；创建及最新持久状态更新时间 |
| `headless` | boolean；环境保存的默认启动模式 |
| `last_launch_at` | UTC ISO 8601 string 或 null；上次启动时间 |
| `identity_present` | boolean；是否保存了身份，不包含实际身份内容 |

该详情仅允许管理员读取，即使 operator 拥有 operate 也不能读取完整配置。
管理员读取不存在的环境返回 404 `not_found`，非规范 UUID 返回 400 `invalid_request`。
未登录返回 401；非管理员返回 403 `admin_required`。读取不要求环境停止、不修改
修订号、不生成身份、不启动浏览器；真正修改仍执行修订号和运行屏障检查。

前端编辑时从此详情填充全部配置，保存时只发送修改字段及读取时的 `revision`。
不能把 `permissions`、`run`、时间或 `identity_present` 回写到 `data`。
列表和远程连接信息仍只返回脱敏元数据，保持原有观看者边界。
持久身份只对管理员返回面向前端的 `browser_version` 和指纹 `config`。
读模型排除 addons 宿主路径、持久启动 options、env、可执行路径及代理凭据。
重建不删除 Cookie、缓存或网站数据。

### KasmVNC 画面传输

远程模式只提供固定 KasmVNC 传输；组件缺失或校验失败时服务拒绝启动。
`GET /me` 不再提供 `display_backends` 和 `default_display_backend`；单个和批量启动
均不接受 `display_backend`，旧字段返回 400 `invalid_request`。
画面或控制连接被取消时，服务仍完成上游关闭和原生输入释放，再清理配对观看会话。

`GET /profiles/{id}/remote` 返回 `transport: "kasmvnc"`、`protocol_version: 2`、
`control_path` 和 `display_path`，不可用时两个路径及 transport 为 null。
路径分别为 `/team/v1/remote/{run_id}/control` 和 `/team/v1/remote/{run_id}/display`。
旧 `websocket_path` 字段与 `WS /remote/{run_id}` 路由已移除。
接口不返回上游地址、端口或认证材料。

控制连接先提交 `{csrf}`；初始状态中的 `viewer_id` 仅用于同一 Cookie、运行和控制
连接的配对。画面连接先提交 `{csrf, viewer_id}`，服务回复 `{ready:true}`；客户端
装好 `/kasm-client.js` 的解码回调后再回复 `{ready:true}`，随后仅传输二进制 RFB。
握手各限 5 秒。每个观看会话最多配对一次；错误来源、重复配对和跨登录均拒绝。
现有全局 16、每登录 2 个窗口的限制按控制连接计数，待配对画面连接也有相同上限。

画面连接始终使用上游只读凭据，输入、中文文本和文件仍通过 Studio 原接口。
未配对完成不能申请控制。任一连接断开均关闭另一条并释放控制；浏览器继续运行，
重新打开恢复只读。成员撤权会同时停止画面转发并关闭两个连接。画面使用有界队列、
转发时限和大小/速率限制，协议消息不丢弃、不重放；慢连接关闭后重新建立解码状态。
浏览器最后一个原生标签页关闭后，该次运行完成资源清理并转为 stopped；旧观看连接
的后续释放不会把正常退出变为 recovery_required，也不会操作同一环境的新运行。

### 执行契约

Command 字段为 `id`, `request_id`, `profile_id`, `run_id`, `operation`, `state`,
`error`, `updated_at`。`operation` 为 start / stop；通常经历 accepted → succeeded
或 failed。显式崩溃恢复将未确认命令置为 `state:"failed", error:"result_unknown"`；
不存在 `state:"unknown"`，此时不能报告操作成功或自动重放。
运行状态独立于命令状态，为 starting / running / stopping / stopped / recovery_required。

HTTP 202 表示已受理，客户端必须查询命令和运行状态确认最终结果。`request_id`
为有效 UUID；同一成员重复提交同一请求 ID 与相同操作签名，复用原命令，签名变化
返回 `request_conflict`。停止必须携带所见运行 ID，防止停止另一代运行。

`mode` 必须来自 `/me.capabilities.launch_modes`。headless 服务只接受 headless；
desktop 服务接受 desktop/headless；remote 服务接受 remote/headless。模式进入持久
命令签名和运行记录；停止仍只按所见 `run_id` 操作那一代运行。

修改环境在 `ensure_editable` 检查下拒绝未结束运行；并发配置修改返回
`revision_conflict`。客户端应提示重新读取并处理冲突，不能自动覆盖别人保存的配置。

## 4. 成员、成员组、审计与健康

这些 HTTP 接口均要求 admin。

| 方法与路径 | 返回/用途 |
| --- | --- |
| `GET /admin` | `members`, `groups`, `audit`；最近 100 条脱敏管理审计 |
| `POST /members` | 按下表 action 修改成员；create 返回成员，其余返回 `ok: true` |
| `POST /groups` | 按下表 action 修改成员组；create/members/grants 返回组，delete 返回 `ok: true` |
| `GET /health` | 当前运行、容量、磁盘与恢复状态；不是匿名存活探针 |

成员字段为 `id`, `name`, `display`, `role`, `active`, `permission_version`。
`/admin` 中每名成员还含直接授权 `grants`；每个组含组记录、成员 ID 数组
`members` 和组授权 `grants`。组记录包含 `id`, `name`, `revision` 和时间信息。
成员名区分大小写，不会在停用后重新分配。

| `/members` action | action 外必需字段 |
| --- | --- |
| `create` | `name`, `display`, `role`, `password` |
| `active` | `id`, `active` |
| `password` | `id`, `password` |
| `grant` | `id`, `profile_id`, `permissions` |
| `revoke` | `id`, `profile_id` |

新增成员为 operator 或 viewer，既有唯一管理员由本机初始化。授权 permissions 是
权限字符串数组，必须符合角色上限。授权修改会撤销受影响成员已有登录。

| `/groups` action | action 外必需字段 |
| --- | --- |
| `create` | `name` |
| `members` | `id`, `revision`, `members`，完整成员 ID 数组 |
| `grants` | `id`, `revision`, `changes`，环境 ID 到权限数组的映射 |
| `delete` | `id`, `revision` |

成员组的 members/grants 修改返回新修订号。changes 只修改指定环境的组授权，不应
清空本次界面没有显示的归档环境。组授权与直接授权取并集，再由角色上限约束。

health 字段为 `active_runs`, `pending_commands`, `active_tasks`, `max_running`, `disk_free_bytes`,
`runtime_registered`, `execution_blocked`, `recovery_required`, `closing`, `remote_display`。
另有 `maintenance` 表示配置/内核服务级占用。当前不包含 CPU、内存、执行机列表
或在线备份任务；不能显示虚构统计值。

## 5. 实时环境状态

连接 `WS /team/v1/events`。浏览器自动带同源 Cookie；服务每轮重新核验登录，
发送与 `GET /profiles` 相同形状的完整快照。约每秒更新，不是带游标的增量日志。
客户端可以发送文本 `ping`，没有 CSRF hello，不能在此通道发送浏览器输入。

当前全服务最多 64 条管理状态连接，同 Cookie 最多 4 条。新界面应由一个应用级连接
服务多页面，避免每个统计卡、表格、弹窗单独订阅。断线后保留最后状态但明确标记
连接中断；重新登录或重新读取 `/me` 后再建立连接。

## 6. 环境文件交换

| 方法与路径 | 请求/返回 |
| --- | --- |
| `GET /files/{run_id}` | `files` 数组，列出 Downloads 中已完成且安全的文件 |
| `GET /files/{run_id}/{name}` | application/octet-stream 下载 |
| `POST /files/{run_id}/{name}` | application/octet-stream 原始字节；返回保存后的文件元数据 |

三者都要求该运行仍有效，成员同时有 watch 和 operate；上传另要求 CSRF。
上传为非空文件，单文件最多 8 MiB，落入该环境 Uploads，名称添加随机前缀。
Uploads 最多 128 个文件、128 MiB。下载拒绝链接、非普通文件和仍在变化的文件。
文件名路径段需要 URL 编码；不能传任意主机绝对路径。

这些接口不等于配置、Cookie 或备份导入导出；新功能应有各自的校验与授权接口。

## 7. 原生画面与独占控制

### 7.1 独立操作窗口与连接信息

保留同源入口 `/#remote=<profile_id>`，其中 profile_id 为规范小写 UUID。
这是前端页面地址，不是原生通道地址；片段不发送到 HTTP 服务。
窗口可以书签、直接打开或从管理页的点击事件中 `window.open`，管理页不必跳转。
登录 Cookie 同源共享，但新窗口仍独立读取 `/me`、取得当前 CSRF 和能力。
地址不得包含密码、Cookie、CSRF、控制代次或运行 ID，不携带任意外部 return URL。
登录后保留原环境片段；打开链接、登录或重连都不自动启动浏览器或申请控制。

`GET /profiles/{profile_id}/remote` 是已实现的只读连接信息接口；要求 view，
无需 CSRF。成功响应有以下固定字段：

| 字段 | JSON 类型与含义 |
| --- | --- |
| `profile` | 与列表单项相同的脱敏元数据、permissions 和 run；run 为最新持久运行或 null |
| `available` | boolean；本次读取时运行与原生会话均可观看 |
| `reason` | 下表原因 string；available=true 时为 null |
| `viewer_path` | string；同源独立页面路径 `/#remote=<profile_id>` |
| `transport` | 可用时为 `kasmvnc`，否则为 null |
| `protocol_version` | 固定为 2 |
| `control_path` | string 或 null；可用时为 `/team/v1/remote/<run_id>/control` |
| `display_path` | string 或 null；可用时为 `/team/v1/remote/<run_id>/display` |

无观看权限仍可在拥有 view 时读取连接状态，得到 `watch_required`，不会拿到画面地址。
没有 view 的成员对未知或未授权环境均得到 403 `access_denied`；管理员查询不存在
环境得到 404 `not_found`。不返回备注、代理、身份、运行 owner 或服务器目录。

原因按表中顺序判断；成功的状态读取为 HTTP 200，即使 available=false。
认证、授权和非法请求仍使用对应 HTTP 错误，不包装成不可用状态。

| reason | 含义与前端行为 |
| --- | --- |
| `watch_required` | 没有 watch；提示申请授权，不建立原生连接 |
| `remote_disabled` | 服务不是 remote 模式；提示执行模式不支持 |
| `service_closing` | 服务正在关闭；结束连接，等待服务可用 |
| `recovery_required` | 服务/引擎故障屏障或运行需要恢复；显示阻塞，不自动重启 |
| `checked_out` | 环境已签出到客户端，服务器不能启动或建立远程画面 |
| `not_running` | 无运行或最新运行已停止；显示未运行，启动必须是单独的明确操作 |
| `starting`, `stopping` | 运行转换中；等待状态变化后重新读取 |
| `not_remote` | 当前 running 运行不是 remote 模式；不能用截图接口代替 |
| `runtime_unavailable` | 运行记录为 running，但缺少或失效的实际原生会话；不伪装成可连接 |

实现复用原生通道的 `RemoteRun.watch` 检查，不能仅凭数据库的 running 就宣告可用。
该接口不创建 WebSocket、不占连接名额、不读取画面、不改变控制租约或身份。
它是当前状态快照，不是预留连接或控制权；连接建立时仍重新验证实际状态。
唯一 Vue 前端使用此接口解析当前运行。

可连接响应示例（UUID 为说明用值，不是预置环境）：

```json
{
  "profile": {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "测试环境",
    "group_name": "",
    "tags": [],
    "os": "linux",
    "archived": false,
    "revision": 1,
    "permissions": ["view", "watch"],
    "run": {
      "id": "22222222-2222-4222-8222-222222222222",
      "state": "running",
      "mode": "remote",
      "error": null,
      "updated_at": "2026-10-05T00:00:00+00:00"
    }
  },
  "available": true,
  "reason": null,
  "viewer_path": "/#remote=11111111-1111-4111-8111-111111111111",
  "transport": "kasmvnc",
  "protocol_version": 2,
  "control_path": "/team/v1/remote/22222222-2222-4222-8222-222222222222/control",
  "display_path": "/team/v1/remote/22222222-2222-4222-8222-222222222222/display"
}
```

### 7.2 前端连接与重连流程

| 步骤 | 动作与边界 |
| --- | --- |
| 解析页面 | 只接受本应用的规范环境 UUID；无效链接显示错误，不尝试连接 |
| 恢复登录 | GET /me；401 显示登录，登录后再次读取 /me，并保留同源环境片段 |
| 读取入口 | GET /profiles/{profile_id}/remote；available=false 展示对应状态，不建立画面连接 |
| 建立画面 | 按 control_path/display_path 构建同源 ws/wss，完成 CSRF 与 viewer_id 配对 |
| 初始观看 | 首个状态和 KasmVNC 画面正常显示后仍只读；明确点击申请控制才发送 claim |
| 状态变化 | 管理事件快照显示运行 ID/状态变化时重新读取入口；旧运行连接先释放并关闭 |
| 连接中断 | 显示断开，重新核对 /me 和入口；会话/授权失效要求登录或授权，不盲目重试 |
| 恢复连接 | 即使同一成员，新的连接也不继承旧输入序号或控制权；不得自动 claim 或重放输入 |
| 关闭窗口 | 清理 WebSocket、心跳、事件监听和解码资源；不发送 stop 命令 |

远程页最多维护一个管理状态订阅及一组配对的原生连接；不得为每个控件单独订阅。
重连采用有上限的退避，状态稳定时不高频轮询入口。首次连接/claim 只发送一次，
路由或组件重渲染不能重新创建浏览器、接管、重复心跳或重放输入。
HTTPS 页面只用 wss，HTTP 页面只用 ws；不能绕过精确 Host/Origin 或改成跨域登录。

### 7.3 原生 WebSocket 协议

控制连接为 `WS /team/v1/remote/{run_id}/control`，画面连接为对应的 `/display`。
要求 watch、有效运行及上节的配对握手。控制连接仅发送 JSON；画面连接仅传输 RFB。
两个通道任一关闭均清理配对和控制租约，浏览器继续运行。

状态字段为 `run_id`, `generation`, `controller`, `mine`, `width`, `height`。
controller 为成员 ID 或 null，mine 为本连接是否持有控制权。画面尺寸来自保存的身份。

| 客户端 kind | 必需字段 | 语义 |
| --- | --- | --- |
| `status` | `kind` | 获取状态；不要求控制权 |
| `claim` | `kind`, `generation`, `force` | 申请控制；force=true 要求 admin |
| `heartbeat` | `kind`, `generation` | 当前控制者续租 |
| `release` | `kind`, `generation` | 释放输入与控制租约 |
| `reset-input` | `kind`, `generation` | 释放已按键/按钮，保留控制权 |
| `input` | `kind`, `generation`, `sequence`, `event` | 原生输入；序号从 1 开始严格递增 |
| `clipboard` | `kind`, `generation`, `source`, `request_id` | Linux 显式读取，source 为 primary（复制当前选中内容）或 clipboard（已有剪贴板）；request_id 为正整数 |

除 status 外都要 operate；heartbeat/release/reset-input/input/clipboard 还要当前连接持有控制权。
控制代次变化后以最新状态为准，不能复用旧代次或重放未确认输入。
输入序号在事件校验前消耗，因此 invalid_input 返回后客户端继续递增，不重发同序号。
Vue 控制台只在分配序号前合并连续的纯鼠标移动，保留首点和最后位置，通常最多
每 40 毫秒发送一次后续移动。点击、按键、滚轮与文本前先发送待发移动；已发送事件
不合并、不重放。释放、重置、代次变化、撤权和断线清除待发移动，不消耗输入序号。

| event.kind | 字段 |
| --- | --- |
| `pointer` | `kind`, `x`, `y`, `button`, `down`；整数坐标在屏幕内，button 为 0–3 |
| `key` | `kind`, `code`, `down`；code 必须来自 `/me.capabilities.key_codes` |
| `wheel` | `kind`, `up`，boolean |
| `text` | `kind`, `text`；非空，最多 1 MiB UTF-8 字节，无 NUL/代理字符，不截断 |

clipboard 成功返回普通状态字段及 `clipboard` 文本、`clipboard_request` 请求编号；
失败返回 error 和相同编号。固定错误为 `clipboard_unavailable`（无文字/复制失败）、
`clipboard_capacity`（超过 1 MiB）。读取后
重新校验权限与运行；撤权不返回文字。客户端只接收当前待处理编号，超时或断线后不重放。
WebSocket 入站容量为 6 MiB + 1024 字节，以覆盖最大文本的 JSON 转义。

已有客户端每 5 秒心跳，服务租约为 30 秒。断线、失焦、撤权释放控制，不停止浏览器。
IME 组合事件不直接转换成远端物理键。输入校验拒绝返回错误 JSON，连接保持；
真实原生输入失败仍走故障关闭。全屏、100% 比例和适应窗口属于客户端显示行为。

全服务最多 16 条原生连接，同 Cookie 最多 2 条；输入请求每连接最多 80 条/秒。
管理与原生连接可能使用关闭码 4400（消息格式）、4401（会话/授权或运行失效）、
4403（来源/握手）、4429（容量）、1011（原生内部故障）、1012（服务关闭）。
前端按当前实际权限和运行状态判断重连，不将 4401 一律解释为密码错误。

### HTTP 错误

错误 JSON 为只含稳定原因码的 `error` 对象，不返回原始第三方异常。

| HTTP 状态 | 常见原因码 |
| --- | --- |
| 400 | `invalid_request`, `invalid_host`, `invalid_file_name` |
| 401 | `session_expired`, `invalid_credentials` |
| 403 | `origin_denied`, `csrf_denied`, `access_denied`, `admin_required`, `role_permission_denied`, `private_network_required`, `client_not_allowed` |
| 404 | `not_found`, `file_not_found` |
| 408 | `request_timeout` |
| 409 | `profile_busy`, `profile_archived`, `revision_conflict`, `request_conflict`, `run_conflict`, `capacity_reached`, `service_busy`, `unsafe_file`, `file_changed` |
| 413 | `request_too_large`, `file_capacity` |
| 429 | `login_locked`, `rate_limited` |
| 503 | `execution_disabled`, `recovery_required`, `health_unavailable`, `file_io_failed` |
| 500 | `operation_failed` |

当前总请求限额为全服务 1200/分钟、每来源 300/分钟，登录另有全服务 60/分钟、
每来源 20/分钟及账号失败锁定。无变化的状态优先走已有订阅，命令查询采用有界间隔。

## 8. 桌面功能补齐接口

以下资源已接入团队服务。start/stop 动态路由仍只支持这两个后缀；
其他操作使用明确路由，不从未知后缀探测能力。

### 8.1 后台 Task

`GET /tasks/{task_id}` 返回 `id`, `request_id`, `kind`, `profile_id`, `state`,
`result`, `error`, `created_at`, `updated_at`。状态为 accepted / running / succeeded /
failed。同成员相同 request_id、kind 和签名复用任务；类型或签名改变返回
`request_conflict`。服务最多同时执行 16 个后台任务，两个 worker，最多保留
256 个已完成结果。Task 是当前服务进程内的有界操作记录；浏览器启停仍使用
数据库持久 Command/Run 契约。

删除、归档、恢复会在任务存续期间占用环境，同环境启动或修改返回
`profile_busy`。配置导入、内核安装和切换使用服务级维护占用，期间拒绝新启动和
冲突修改。不将多个环境包装成可回滚的分布式事务。

### 8.2 环境、代理与 Cookie

| 方法与路径 | 权限 | 请求/返回 |
| --- | --- | --- |
| `POST /profiles/batch` | 按 action | `action` 为 start/headless_start/stop/archive/restore；`items` 最多 50 项，每项自带 request_id，停止另带 run_id，归档/恢复另带 revision |
| `GET /proxies` | admin | 脱敏代理数组；只有 `has_*` 标志，不回读凭据 |
| `POST /proxies` | admin | 创建代理；返回 201 |
| `POST /proxies/{id}` | admin | 使用既有代理校验更新 |
| `POST /proxies/{id}/delete` | admin | 空对象；被环境引用时拒绝 |
| `POST /proxies/import` | admin | `request_id`, `text`；逐行验证并返回 Task |
| `POST /proxies/{id}/probe` | admin | `request_id`；在执行机检查出口 IP/时区/延迟 |
| `GET /settings` | admin | 执行机设置；团队工作区的 `automation_enabled` 始终为 false |
| `POST /settings` | admin | 允许的设置字段；活动运行期间禁止切换执行文件，不能把上限降到当前运行数以下 |
| `GET /timezones` | 已登录 | `version` 与 `timezones: [{name, region}]`，按规范名称排序的服务器时区目录 |
| `GET /configuration` | admin | 既有配置导出；不含身份、成员、登录、授权、秘密或网站数据 |
| `POST /configuration/import` | admin | `request_id`, `configuration`；全部运行停止时创建新环境/代理，返回 Task |
| `GET /kernels` | admin | 已安装 Camoufox 的相对资源 `id`、精确 `kernel_id`、版本/摘要元数据，新环境默认 `default`，各平台缓存 `cache`（未配置 `--kernel-root` 时为 `null`）；不返回绝对路径 |
| `POST /kernels/catalog` | admin | `request_id`；Task 结果 `kernels: [{kernel_id, version, build, prerelease, cached, complete}]`，`complete` 表示四个平台都有唯一且带摘要的官方资产 |
| `POST /kernels/cache` | admin | `request_id`, `kernel_id`；停止状态下下载 `lin-x86_64`、`win-x86_64`、`mac-arm64`、`mac-x86_64` 官方包，按 GitHub 资产摘要和大小校验，并把 Linux 包安装为服务器本机内核；任一平台缺少摘要即失败，返回 Task |
| `POST /kernels/remove` | admin | `request_id`, `kernel_id`；仍被环境、身份或新环境默认引用时以 `kernel_in_use` 失败，否则删除缓存包与本机安装，返回 Task |
| `GET /kernels/{repository}/{release}/{platform}` | 已登录 | 缓存的官方 ZIP，响应头 `X-Content-SHA256`；客户端安装前须自行校验 |
| `POST /kernels/activate` | admin | `request_id`, `id`；ID 必须来自当前已安装清单，设置新环境默认并返回 Task，不改变已有环境或运行 |

这些资源只管理一个固定执行机。CPU/内存、多执行节点和分布式调度仍未实现，
前端不得使用固定值伪造。

`POST /settings` 接受 `max_running`（1–50 的整数）、`executable_path`、`automation_enabled`，省略的字段保留原值。只修改并发上限时，活动运行
不妨碍保存，只要新值不低于未结束运行数；提交与原值相同的路径/网卡不会被视为切换。
维护占用返回 409 `service_busy`，上限低于未结束运行数返回 409 `capacity_reached`。
`automation_enabled:true` 返回 403 `role_permission_denied`，成员 API 密钥监听由服务启动参数控制。

时区目录从 `zoneinfo.TZPATH` 的首个非空目录读取 `tzdata.zi`；没有系统目录时按
标准库规则读取已安装的 `tzdata` 包。目录使用 `Zone`/`Z` 记录中带地区前缀的规范名称，
不把 `Link`/`L` 别名或旧 POSIX 名称作为选项，并逐项经过保存配置使用的 `ZoneInfo` 校验。
系统目录缺少索引、索引非法或命名无法验证时返回 503 `timezone_catalog_unavailable`，
不会换用另一份名称表。数据来源见 [Python zoneinfo](https://docs.python.org/3/library/zoneinfo.html)
和 [tzdata 包内容](https://tzdata.python.org/#contents)。

网卡枚举失败返回 503 `network_interfaces_unavailable`。网页保留未出现在当前目录中的
网卡名称或已保存时区别名，不能据此判断当前路由已可用。时区保存 `""` 明确切回自动模式；
省略字段仍表示保留原值。手动代理探测成功后，代理读取模型可附带
`last_probe: {timezone, checked_at}`，只保存时区和探测时间，不保存 IP 或凭据。
修改代理会清除此观察值。
探测完成时代理已更新或相关路由设置不再匹配，则不记录该结果。配置导出不含此观察数据。

### 8.4 仍保持的安全边界

成员 API 密钥通过独立的 `/automation/v1` 监听调用，启动参数为
`--automation-listen`，默认端口 8767。协议 v1 不变，连接文件由 Vue“API 密钥”
页面创建并只显示一次。环境范围与成员当前授权取交集；借用和任务标签页按密钥归属隔离。
`team_automation` 表示监听是否开启。启动复用团队服务的持久运行与维护门禁，
创建环境要求未限定环境范围的管理员密钥。详见[接口协议](studio-automation.md#team-service)。

加密备份、隔离恢复和来源退役仍是本机离线维护命令。它们要求团队服务先释放
工作区锁，密钥与归档分离，并对来源工作区执行退役证明。服务不接受任意执行机
路径或密钥内容，`online_maintenance=false`。

### 8.5 API 密钥管理

| 方法 | 路径 | 权限与返回 |
| --- | --- | --- |
| GET | `/team/v1/api-keys` | 管理员读取全部、操作员读取自己的密钥，返回 `{keys, enabled}` |
| POST | `/team/v1/api-keys` | 管理员或操作员；字段 `name`、可选 `profile_ids`、`allowed_ips`、`expires_at` |
| POST | `/team/v1/api-keys/{id}/revoke` | 管理员或密钥所有者；请求体 `{}`，立即清理该密钥的任务借用 |

创建返回 `{key, connection:{protocolVersion:1, baseURL, token}}`；后续读取不返回密钥。
`profile_ids:null` 表示不额外缩小成员范围，指定列表不能为空；来源 IP 留空表示不额外限制。
有效期必须是带时区的未来 ISO 时间。成员停用或密码重置后旧密钥永久失效；
授权调整只更新可用环境范围，不要求重建密钥。监听关闭时创建返回 409
`automation_disabled`，只读成员管理密钥返回 403。所有写接口仍要求网页会话、Origin 与 CSRF。
密钥记录包含 `id`、`member_id`、`member_name`、`name`、`profile_ids`、`allowed_ips`、
`created_at`、`expires_at`、`last_used_at` 和 `revoked_at`；仅创建响应返回原始 `token`。
正常部署及显式崩溃恢复保留密钥记录及其内部会话，不改变范围、到期时间或撤销状态。

### 8.6 签出租约（本机运行）

服务以 `--staging-root DIR` 启动时提供。目录在工作区之外、与工作区同一文件系统；
未配置时下列接口返回 409 `checkout_unavailable`。前两项使用网页会话、Origin 与 CSRF，
其余使用 `Authorization: Bearer <租约令牌>`，与 12 小时登录会话解耦；POST 仍带服务器
精确 `Origin`，不带网页 CSRF。此令牌不能调用管理或自动化接口。

| 方法 | 路径 | 鉴权与作用 |
| --- | --- | --- |
| POST | `/profiles/{id}/checkout` | `checkout` 授权（管理员默认具备）；`{request_id, device_id, device_name, platform}`，返回 201 `{checkout, token}`。同一设备以同一 `request_id` 重试会换发令牌；有活动运行、活动租约、维护任务或环境已归档时拒绝 |
| POST | `/profiles/{id}/release` | 管理员；`{checkout_id}`，强制解锁，服务器保留最后一次签回的数据 |
| GET | `/checkouts/{id}` | 租约令牌；签出清单：可编辑配置、`revision`、`data_revision`、固定身份、绑定代理（含密码）、`kernel`（租约平台的缓存包记录，未缓存时为 `null`）、网站数据文件 `{相对路径: {size, sha256}}` |
| GET | `/checkouts/{id}/kernel` | 租约令牌；环境所需精确内核在租约平台上的缓存 ZIP，响应头 `X-Content-SHA256`；未缓存时 404 `kernel_not_cached` |
| POST | `/checkouts/{id}/files` | 租约令牌；`{names}`，返回所列文件的 `ZIP_STORED` 流 |
| POST | `/checkouts/{id}/identity` | 租约令牌；`{identity}`，在服务器身份账本中原子判重保存，返回 `{claimed}`；须与环境 `kernel` 一致 |
| POST | `/checkouts/{id}/heartbeat` | 租约令牌；`{}`，返回 `{state, release_requested}`，授权被收回或成员停用时 `release_requested` 为 true |
| POST | `/checkouts/{id}/cancel` | 租约令牌；`{}`，尚未开始签回的租约由持有者放弃，服务器数据不变，租约置为 `cancelled`；已上传数据后返回 `checkout_closed`，只能提交或由管理员强制解锁 |
| POST | `/checkouts/{id}/return` | 租约令牌；`Content-Type: application/zip`，上传变化文件（`ZIP_STORED`，单文件 4 GiB、总量 32 GiB），租约进入 `returning` |
| POST | `/checkouts/{id}/commit` | 租约令牌；`{files, data_revision}` 为完整清单与签出时的数据版本，成功返回新的 `data_revision`；缺少的文件以 409 `{error: 'checkout_incomplete', missing}` 列出，补传后重试 |

- 有活动租约时，远程/无头启动和编辑、删除、重建身份返回 `profile_checked_out`；
  远程入口返回原因 `checked_out`；复制环境仍允许，只复制配置。备份快照与迁移准备拒绝。
- `GET /profiles` 每项增加 `checked_out`：`id`、`state`、`member_name`、`device_name`、
  `platform`、`created_at`、`heartbeat_at`、`mine`；无租约时为 `null`。
- 租约不会因心跳中断而过期，只有签回提交或管理员强制解锁能结束它。强制解锁后，
  旧令牌的所有请求返回 `checkout_revoked`。
- 提交先在暂存区组装并校验完整清单，再写入持久提交记录，最后以目录改名替换网站数据并把
  `data_revision` 加一。服务启动时完成未结束的提交，并删除不属于活动租约的暂存目录。
- `cache2/`、`startupCache/`、`thumbnails/`、`crashes/`、`minidumps/` 与 Firefox 锁文件不同步；
  链接、硬链接、不可移植文件名和大小写冲突的数据拒绝签出或签回。

### 8.7 客户端安装包与更新

服务以 `--client-root DIR` 启动时，从该目录的 `manifest.json` 提供客户端安装包；目录在工作区之外，
清单由 `camoufox studio-client-manifest DIR` 校验安装包后生成。未配置或没有清单时 `packages` 为空。

| 方法 | 路径 | 鉴权与作用 |
| --- | --- | --- |
| GET | `/client/update` | 已登录；`{protocol, minimum_protocol, packages}`，每项 `{platform, version, file, sha256, size, minimum_protocol, protocol}`；只列出与服务器协议互相兼容、文件存在且大小一致的安装包，不返回路径 |
| GET | `/client/packages/{platform}` | 已登录；该平台的安装包，响应头 `X-Content-SHA256`；未提供时 404 `not_found` |

- `platform` 与内核缓存相同：`win-x86_64`、`mac-arm64`、`mac-x86_64`、`lin-x86_64`，
  安装包后缀分别为 `.exe`、`.dmg`、`.dmg`、`.deb`。
- 清单损坏、重复平台、类路径文件名、协议不兼容或安装包缺失、截断时两项接口返回 503
  `client_packages_unavailable`，不提供部分结果。
- 客户端和服务器各自声明 `protocol` 与 `minimum_protocol`；任一方低于对方的最低值即不兼容，
  客户端只显示下载页。客户端下载后自行校验大小与摘要；HTTP 下记录与安装包来自同一明文连接。

## 9. 契约维护方式

1. 将当前请求、响应和错误结构建模，保留现有路由及精确校验语义。
2. 从实际应用模型导出 OpenAPI；不维护第二份手写服务 schema。
3. 生成 TypeScript 类型，在 API 访问层集中处理登录、CSRF、错误与命令查询。
4. WebSocket 状态/输入协议独立定义，HTTP OpenAPI 不替代它的类型与回归。
5. 每项扩展从表中转为已实现章节，更新功能映射，并核对 wheel 静态资产与冷启动。

环境详情与远程连接信息的回归在 `pythonlib/tests/test_studio_team_service.py`；
真实原生会话读取、返回地址的实际握手/KasmVNC 画面、只读控制边界和运行停止后的入口失效在
`service-tester/studio_native_tests.py::test_remote_entry_tracks_real_run_without_acquiring_control`。
接口测试不等于新版前端已经接入；新版登录返回、弹窗拦截、运行换代与重连仍需浏览器验收。

依据：[FastAPI 模型与 OpenAPI](https://fastapi.tiangolo.com/tutorial/schema-extra-example/)。
