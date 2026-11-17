# Studio 环境参数与内核能力

本文区分「内核能执行的配置」和「管理界面中的名称」。本仓库已移除浏览器构建系统，
使用上游发布的内核。下表保留 `4f58a93` 的历史能力核查（Firefox 156.0.1，2026-10-01），
相关源码链接固定到清理前的历史提交。它不是当前安装内核的能力保证；实际可用配置应以
该内核的 `properties.json` 和运行验证为准，不能把历史源码清单当作当前版本声明。
已交付的界面选项及随机生成、固定复用和本地去重规则见
[Studio 使用说明](studio.md#advanced-fingerprint-controls)。

Camoufox 是 Firefox。其他浏览器产品的同名按钮，不一定对应 Firefox 的
同一种接口。「随机」应当表示创建身份时从记录的数据中抽取、保存后重复
使用；「真实」表示让该 Camoufox 内核读取当前主机或使用自身实现，不保证
跨操作系统的渲染结果相同，也不意味着所有其他身份覆盖一并关闭。

## 参数对应关系

| 界面参数 | 可兑现的选项 | 实际作用和限制 | 仓库依据 |
| --- | --- | --- | --- |
| 分辨率 | 跟随显示器、已记录的屏幕配置、经过校验的自定义尺寸 | `screen.*`、窗口尺寸和像素比需要一起保持一致。物理显示器、浏览器外窗和页面视口不是同一个尺寸；不能承诺任意超出显示器的可见窗口。 | screen 补丁、[尺寸协调](../pythonlib/camoufox/fingerprints.py) |
| 字体指纹 | 身份字体集合、使用主机可用字体 | 字体集合限制可见字体，不生成字体文件。主机模式要撤销身份字体过滤；字体包、操作系统字体和排版实现仍影响结果。跨 OS 身份不能凭字体名称复现另一 OS 的排版。 | 字体配置层、字体注册、字体说明 |
| WebRTC | 使用内核行为、替换可见 IP、禁用 | 替换通过 `webrtc:ipv4` / `webrtc:ipv6`；禁用使用 `media.peerconnection.enabled=false`。替换可见地址不等于建立了可用的 UDP/TURN 代理通道。 | WebRTC 补丁、[启动配置](../pythonlib/camoufox/utils.py) |
| WebGL 图像 | 保留实际渲染 | 没有 WebGL 像素随机化配置。修改 vendor/renderer 不能改变宿主 GPU 实际算出的像素。 | WebGL 补丁、渲染边界 |
| WebGL Info | 记录设备抽样、已记录 vendor/renderer 配对、内核原始信息 | 配对需要带上对应 WebGL1/2 参数、扩展和精度数据；不能将任意两个字符串当作完整 GPU 身份。主机模式必须清除整组覆盖，不能只清除名称。 | [WebGL 数据生成](../pythonlib/camoufox/webgl.py)、配置声明 |
| WebGPU | 使用内核默认行为、禁用 | 可通过 `dom.webgpu.enabled=false` 禁用；是否实际可用仍取决于系统、驱动、构建与安全上下文。没有「基于 WebGL 自动匹配」的 adapter 信息或功能模拟。 | 配置声明、下文 WebGPU 官方来源 |
| Canvas | 保留实际渲染 | 仓库明确不加入 Canvas 像素噪声，没有有效的 `canvas:seed`；不提供「随机 Canvas」开关。不同身份也可能得到同一图像哈希。 | 历史规则：`canvas-is-not-noised` |
| AudioContext | 身份固定的音频变化、使用实际结果 | 内核支持 `audio:seed`；`0` 关闭该变换。实际模式还应移除 `AudioContext:sampleRate`、`outputLatency`、`maxChannelCount` 覆盖，不能只清除种子而保留参数伪装。 | 音频变换、音频参数 |
| SpeechVoices | 身份语音列表、主机语音列表 | 身份列表来自记录的 OS/语言目录。身份语音的事件时序由内核处理，不代表已安装对应的真实语音合成引擎；主机模式的可用语音取决于系统服务。 | 语音注册、[语音生成](../pythonlib/camoufox/utils.py) |
| Do Not Track | 开启、关闭 | 对支持此 pref 的内核统一修改 `privacy.donottrackheader.enabled`。开启应同时得到 JS 值 `"1"` 和请求头 `DNT: 1`；关闭是 `"unspecified"` 且不发送该头，不是 `"0"`。 | [启动配置](../pythonlib/camoufox/utils.py)、下文 DNT 官方来源 |
| ClientRects | 正常布局结果 | 无独立随机化配置。矩形由字体、样式、缩放和布局产生；仓库已拒绝通过字形间距噪声制造差异。 | 历史规则：`no-glyph-spacing-noise` |
| 媒体设备 | 身份设备集合、主机真实设备 | 身份模式控制数量、名称与分组，并遵守授权前后的可见性规则；虚拟摄像头返回测试画面，不是完整的物理摄像头替身。主机模式使用 `mediaDevices:enabled=false` 并清除身份设备覆盖。 | 媒体设备说明、媒体设备补丁 |
| 设备名称，例如 `DESKTOP-…` | 仅可作为管理备注 | 没有将任意主机名映射为网页指纹的 Camoufox 配置，也不会重命名操作系统。不能把备注字段标成已生效的浏览器硬件参数。 | 配置声明、下文设备标识说明 |
| MAC 地址 | 不提供浏览器修改开关 | 网页标准接口没有直接读取网卡 MAC 的通用字段；本内核没有对应修改项。Profile、HTTP/SOCKS 代理都不创建或修改主机网卡。 | 配置声明、下文设备标识说明 |
| 硬件并发数 | 跟随主机或经过一致性校验的逻辑处理器数量 | 通过 `navigator.hardwareConcurrency` 控制页面与 Worker 的报告值。12 表示 12 个逻辑处理器，不保证 12 个物理核心，也不会增加计算能力。 | Navigator 补丁、[CPU 协调](../pythonlib/camoufox/fingerprints.py) |
| 内存，例如 8 GB | 显示「此 Firefox 接口不支持」 | Firefox 不提供 `navigator.deviceMemory`，本仓库没有增加它。不能用磁盘配额或运行时内存限制冒充该值，也不应注入一个 Chromium 专有形态的属性。 | [fpgen 映射](../pythonlib/camoufox/fpgen.yml)、[MDN 兼容数据][device-memory-compat] |
| SSL/TLS 指纹 | 保留 Firefox/NSS 的握手实现 | 本基线无 Profile TLS 指纹模板或随机化能力。「SSL 指纹开启」不应映射为忽略证书错误、改变 UA 或随机禁用密码套件。 | 网络补丁、下文 TLS 说明 |
| 端口扫描保护与白名单 | 明确范围的请求端口限制 | Firefox 有目标端口阻止机制，可限制经该机制检查的请求。它不是系统防火墙，也不是完整的局域网或 DNS 重绑定隔离；白名单应说明是端口号还是主机/端口，不能混为一谈。 | 下文端口限制说明 |

## WebGPU、WebGL 和图像输出

Firefox 156.0.1 的 [StaticPrefList][firefox-prefs] 定义了
`dom.webgpu.enabled`；[WebGPU Instance][webgpu-instance] 同时检查该开关、
驱动阻止列表和构建条件。因此禁用有实际内核路径，启用开关却不保证
`requestAdapter()` 一定获得设备。

WebGPU 的 [WebIDL][webgpu-idl] 单独暴露 adapter 信息、features 和 limits。
Camoufox 的 WebGL 补丁 修改的是 WebGL
接口，没有修改这些 WebGPU 实现。把 `webGl:vendor` / `webGl:renderer`
复制成某种界面描述，不会改变 WebGPU 设备。可提供原生行为或禁用，不能
宣称已完成跨 API 的 GPU 匹配。

Canvas、WebGL 和 ClientRects 不是三个可以随意替换的字符串。前两者涉及
实际渲染；[ClientRects][client-rects] 是布局盒的结果。本仓库保留 Canvas
像素和字形间距的正常实现；需要复现另一操作系统或 GPU 的渲染时，应当
使用相应运行环境，不能仅从信息字段覆盖推导出图像一致。

## 「真实」模式必须撤销完整覆盖

身份生成器会自动补齐字体、音频、语音、媒体设备和 WebGL 配置。切换某项
到真实模式时，应在统一的启动配置流程撤销这项的全部相关覆盖，并避免
后续生成步骤再次补回。空列表、缺失字段和 `false` 不是处处等价：

- 字体配置层将空列表视为未配置过滤；但启动生成器会给空字体列表补上身份
  字体，所以只在表单里保存空列表不够。
- 语音的 `voices` 数组即使为空也意味着内核管理列表。真实模式应移除
  `voices` 和 `voices:blockIfNotDefined`。当前补丁对后一个键检查的是
  `std::optional` 是否存在，写成 `false` 仍可能阻止系统语音注册。
- 音频需要同时处理种子和三个 `AudioContext:*` 参数；WebGL 需要同时处理
  名称、参数、扩展、精度和启动时强制设置的相关 prefs。
- 真实媒体设备依然受用户授权和操作系统权限约束。授权前看不到名称，
  不等于配置没有生效。[标准接口的权限规则][enumerate-devices] 和本仓库
  媒体实现 均保留这一约束。

WebRTC 地址替换也有独立边界：补丁控制可见候选信息，HTTP/SOCKS5 代理的
TCP 转发不自动变成 UDP 转发。使用代理出口作为可见地址，应先检查出口，
并处理 IPv4/IPv6 一致性；连接能否完成仍取决于真实 ICE/TURN 路径。需要
完全禁止 WebRTC 时应禁用接口，而不能把地址替换当作连接隔离。

## DNT 的版本与协议一致性

Firefox 156.0.1 的 [Navigator::GetDoNotTrack][firefox-navigator] 和
[nsHttpChannel::SetDoNotTrack][firefox-http] 读取同一个 pref；前者返回页面
值，后者决定发送请求头。[静态 pref 声明][firefox-prefs] 的默认值为关闭。
Firefox 设置页是否展示选项，与内核是否保留该 pref 是不同的问题。

对另一个已安装版本，应同时实测页面值与本地 HTTP 服务收到的请求头，
不能只检查 prefs 文件里有一行配置。DNT 表达隐私偏好，不负责强制阻止
网站跟踪；也不能将它和 `Sec-GPC` / Global Privacy Control 混用。

## 设备名称、MAC 与内存

Firefox 的 [Navigator 接口][navigator-idl] 没有通用主机名或网卡 MAC 属性。
媒体设备可能在授权后提供设备标签；这些标签与可配置的操作系统主机名
不是同一个接口。[MediaDeviceInfo.deviceId][media-device-id] 是按来源处理
的设备标识，不应当解释成网卡 MAC。

本基线的配置声明及补丁没有主机名/MAC 虚拟化能力，因此结论限于普通
网页接口和本仓库实现，不声称本机程序、扩展或已授权的设备接口永远无法
暴露其他设备信息。若管理器保存这些值，只能明确标为备注，不能作为
浏览器修改成功的证据。

`navigator.deviceMemory` 在 [MDN Firefox 兼容记录][device-memory-compat]
中为不支持；本仓库的 [fpgen 映射](../pythonlib/camoufox/fpgen.yml) 也明确
排除了它。物理 RAM、站点存储配额和单个进程内存上限需要分别描述。

## TLS 指纹

TLS 的 ClientHello 包含版本、密码套件、扩展等握手结构，见
[RFC 8446 的 ClientHello 定义][tls-clienthello]。Firefox 的安全协议实现
属于 [NSS][nss]，不由页面的 Navigator 字段或 HTTP User-Agent 决定。

在本核查基线中，未发现可为每个 Profile 选择 TLS 握手模板的配置或补丁。
保留 Firefox 的正常握手是可以兑现的行为；任意浏览器 TLS 身份匹配则需要
另一项明确的网络栈实现及验证。关闭证书验证改变的是连接安全检查，不能
作为 TLS 指纹功能；代理改变出口，也不自动改变浏览器的端到端握手实现。

## 端口限制的覆盖范围

Firefox 的 [nsIOService][firefox-io] 使用内置受限端口表，并读取
`network.security.ports.banned` 增加阻止项、
`network.security.ports.banned.override` 移除阻止项；列表解析器支持端口
号及范围。这与 [Fetch 标准的端口阻止规则][fetch-ports] 对应，是实际的
目标端口限制机制。它不区分用户是否正在「扫描」。

产品若采用该机制，界面应使用「限制请求端口」等准确名称，说明规则影响
所有适用目的地址。只允许 80/443 会影响合法的非标准端口服务；覆盖列表
还可能解除浏览器原本的危险端口限制，不能未经说明地修改。协议处理器的
例外、代理连接本身和其他网络路径仍需单独验证。

只拦截 URL 中的 `localhost` 或私网字面量，不等于阻止域名解析后访问私网。
如果采用 Playwright 路由，必须另外检查 Service Worker、重定向和 WebSocket
覆盖；[官方路由文档][playwright-route] 明确提示 Service Worker 请求可能
绕过路由。完整的局域网隔离还需要明确 DNS、IPv6、代理解析和 WebRTC 的
策略，不能由一个没有这些实现的「防扫描」按钮承诺。

[firefox-prefs]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/modules/libpref/init/StaticPrefList.yaml
[webgpu-instance]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/dom/webgpu/Instance.cpp
[webgpu-idl]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/dom/webidl/WebGPU.webidl
[client-rects]: https://developer.mozilla.org/en-US/docs/Web/API/Element/getClientRects
[enumerate-devices]: https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/enumerateDevices
[firefox-navigator]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/dom/base/Navigator.cpp
[firefox-http]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/netwerk/protocol/http/nsHttpChannel.cpp
[navigator-idl]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/dom/webidl/Navigator.webidl
[media-device-id]: https://developer.mozilla.org/en-US/docs/Web/API/MediaDeviceInfo/deviceId
[device-memory-compat]: https://github.com/mdn/browser-compat-data/blob/main/api/Navigator.json
[tls-clienthello]: https://www.rfc-editor.org/rfc/rfc8446.html#section-4.1.2
[nss]: https://firefox-source-docs.mozilla.org/security/nss/index.html
[firefox-io]: https://github.com/mozilla-firefox/firefox/blob/FIREFOX_156_0_1_RELEASE/netwerk/base/nsIOService.cpp
[fetch-ports]: https://fetch.spec.whatwg.org/#port-blocking
[playwright-route]: https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route
