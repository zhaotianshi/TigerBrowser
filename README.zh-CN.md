<div align="center">

# TigerBrowser

[English](README.md) · **简体中文**

**面向团队浏览器环境管理的服务器与桌面客户端**

持久化环境 · 指纹配置 · 网络连接管理 · 批量会话管理

[快速开始](#快速开始) · [功能概览](#功能概览) · [完整文档](#文档) · [上游项目](https://github.com/daijro/camoufox)

</div>

> [!IMPORTANT]
> 本项目仍在持续开发中，已提供含成员授权的团队服务器、网页与桌面客户端、签出本机运行及 Linux 原生远程预览。远程串流统一使用 Linux KasmVNC；三端客户端与目标设备的实机验收仍待完成。请仅在获得授权的自动化、测试和数据采集场景中使用。

## 项目简介

TigerBrowser 基于开源项目 [Camoufox](https://github.com/daijro/camoufox) 深度开发。它使用上游发布的 Camoufox 浏览器，保留 Python 启动器、原生指纹配置和 Playwright 接口，提供持久化环境管理。此仓库不再包含 Firefox 补丁、内核构建系统或独立 TypeScript 启动器。

TigerBrowser 为每个环境保存独立的浏览器目录、设备身份、代理绑定和运行状态。关闭并重新启动后，Cookie、站点存储和已生成的身份仍会保留。

团队服务器保存全部环境、成员和授权，环境在服务器上无头或远程运行，也可签出到成员电脑本机运行，同一时刻只在一处运行。当前界面为中文，桌面客户端支持 Windows、macOS 和 Linux，仓库提供 Windows x64 独立客户端包的构建流程。

## 功能概览

### 持久化浏览器环境

- 每个环境使用独立的 Firefox Profile，隔离 Cookie、缓存、Local Storage、IndexedDB 和站点数据。
- 首次启动时生成设备身份，后续启动复用完整身份，不会每次随机变化。
- 支持名称、分组、标签、备注、启动网址、语言、时区和目标操作系统。
- Vue 编辑页提供可搜索的服务器时区列表，保留旧值，可切回自动跟随代理出口。
- 支持创建、编辑、复制、归档、恢复、批量启动、批量停止和批量删除。
- 选择环境后点击“无头启动”，可单个或批量在后台运行，不显示浏览器窗口；列表显示“无头 · 运行中”，使用“停止”结束会话。
- 复制环境只复制配置，新环境会生成新身份并使用空的浏览器目录。
- 团队环境固定精确内核版本并写入身份记录；更换新环境默认内核不影响已有环境。缺失或匹配不唯一时明确阻止启动，手动更换环境内核会重建身份并保留网站数据。
- 自动化程序可通过成员 API 密钥直连服务器，创建环境、按环境 ID 启动并独占借用会话、创建任务标签页；释放借用不关闭人工标签页。Vue“API 密钥”支持有效期、环境和来源 IP 限制，创建环境要求管理员权限。服务器使用 `--automation-listen` 开启独立端口。见[接口协议](docs/studio-automation.md)与[接入说明](docs/studio-automation-connections.md)。
- 页面 RPC 可在启动时按需申请 `mainWorldEval:true`，服务器与本机接口均报告实际会话能力；不修改永久身份。团队接口声明 `mainWorldRestart` 时，集成程序可在排空任务并释放租约后显式请求仅重开缺少能力的目标会话；普通 UI 和缺省请求不重启。见[主世界对接](docs/studio-integration.zh-CN.md#52-按需启用主世界执行)。

- 自动化接口支持页面范围的被动响应观察及原字节二进制分块传输，保留原生下载与 Base64 兼容路径。客户端按能力发现接入，先登记监听再点击；不拦截、重放网站请求，也不替应用判断原图。见[协议、边界与 Node 示例](docs/studio-response-transfer.md)。

### 指纹与浏览器能力

TigerBrowser 复用 Camoufox 的原生配置层。可配置项目包括：

- 操作系统、语言、时区、屏幕分辨率和字体集合；
- 新建环境默认使用 1920 × 1080 分辨率，可在高级指纹中调整；已有环境继续使用保存的尺寸。
- WebGL 设备信息与原生 WebGPU 开关；
- AudioContext、语音列表和媒体设备；
- 逻辑处理器数量、Do Not Track 和 WebRTC 模式；
- 使用已记录的真实设备组合，拒绝不一致的 WebGL 参数；
- 工作区内记录身份摘要，避免新生成的完整设备配置重复。

Canvas、WebGL 像素和 ClientRects 保持浏览器真实渲染结果。项目不会通过 Canvas 噪声或字形间距噪声制造不存在于真实设备上的输出。完整边界见[环境参数与内核能力](docs/studio-capabilities.md)。

### 代理

每个环境可以独立绑定网络出口。启动前会检查代理；检查失败会停止启动，不会悄悄回退为本机直连。

| 类型 | 支持范围 |
| --- | --- |
| HTTP / HTTPS | 地址、端口和用户名密码认证 |
| SOCKS5 | 无认证或用户名密码认证；域名由上游代理解析 |

可在“代理资源”中手动添加、解析单条地址，或逐行批量导入代理。格式和认证说明见[代理配置](docs/studio-nodes.md)。

### 本地数据与凭据

- 环境、代理元数据、设置和活动记录存入本地 SQLite 数据库。
- 浏览器数据保存在 `profiles/<环境 ID>/`，不会写入仓库。
- 代理密码进入操作系统原生凭据服务。
- Windows 使用 Credential Manager，macOS 使用 Keychain，Linux 使用 Secret Service。
- 原生凭据服务不可用时拒绝保存密码，不会降级为明文文件。
- 支持 Playwright Cookie JSON 导入和导出。
- 配置导出不包含密码、生成身份、Cookie、历史记录或浏览器目录。

## 快速开始

### 部署服务器

TigerBrowser 的团队服务器保存环境、代理、成员和授权，网页与桌面客户端都连接这台服务器。在仓库根目录的 Python 3.10+ 隔离环境中运行：

```bash
python -m pip install -r pythonlib/team-requirements.lock
python -m pip install --no-deps -e ./pythonlib
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build
python -m camoufox fetch
python -m camoufox studio-team-init --data-dir ./my-team-workspace --name admin --display Administrator
python -m camoufox studio-team --data-dir ./my-team-workspace
```

全新 Ubuntu 24.04 x86_64 服务器可改用 `sudo python3 scripts/install-studio-server.py --origin http://<服务器IP>:8766 --listen 0.0.0.0` 安装为 systemd 服务，见[Ubuntu 服务器安装](docs/studio-team-service.md#ubuntu-服务器安装)。
安装器同时配置服务账号的独立 Secret Service；systemd 用本机加密凭据解锁钥匙环，启动前检查读写，失败即停止，不降级为明文。升级保留解锁凭据与钥匙环；全新服务器完整验收仍待完成。

终端隐藏输入管理员密码，不提供远程初始化或默认口令。服务默认只监听 `http://127.0.0.1:8766`，浏览器执行默认关闭；跨电脑访问、无头或 Linux 远程运行、HTTPS、签出与内核缓存见[团队服务说明](docs/studio-team-service.md)。

早期桌面版本留下的无成员工作区不会自动迁移。停止旧桌面后运行 `python -m camoufox studio-team-init --existing --data-dir <原工作区>` 创建管理员，环境、身份和网站数据保持不变，再用 `studio-team` 提供服务。

### 安装桌面客户端

锁定的桌面依赖需要 Python 3.12 或更高版本，以及可用的桌面图形环境：

```bash
git clone https://github.com/zhaotianshi/TigerBrowser.git
cd TigerBrowser
python -m pip install -r pythonlib/studio-requirements.lock
python -m pip install --no-deps -e "./pythonlib[studio]"
python -m camoufox fetch
python -m camoufox studio
```

首次启动填写服务器地址，须与服务器的 `--origin` 完全一致。客户端验证 TigerBrowser 服务器后保存地址，可保存多个地址。登录状态按服务器分别保存到过期。

`fetch` 会安装默认扩展和经过摘要校验的 fpgen 模型；本机运行所需的浏览器内核按环境从服务器缓存取得。启动时不会偷偷下载缺失组件，安装不完整会给出明确提示。

三端桌面均需完整安装上述锁文件；签出同步复用归档校验模块，Windows 和 macOS 也需要 `cryptography`，不能只依赖开发环境中已安装的服务器依赖。

产品只保留“团队服务器＋图形客户端”，不另行开发或打包独立单机版。桌面必须连接服务器；“本机启动”通过签出、运行和签回完成，不是独立工作区。服务器工作区与客户端缓存不能共用可写数据目录。旧的 Qt 管理窗口、`--local` 与 `studio-host` 已删除。范围决定见[设计第 7.6 节](docs/studio-client-server.md#76-仅提供连接服务器的客户端)。

### Ubuntu Desktop 安装与桌面启动

Ubuntu Desktop 24.04 或更新版本可从源码安装，支持上游提供依赖和浏览器资源的 x64、arm64 平台。以当前桌面用户运行：

```bash
python3 scripts/install-studio-ubuntu.py
bash start-studio.sh
```

安装器通过 `sudo` 安装 Qt、浏览器和 GNOME Keyring 的系统依赖，在项目的 `.venv-studio` 中安装锁定的 Python 依赖，再调用现有下载器安装扩展和指纹模型。整个过程无需编译 Firefox，也不会覆盖已有数据或凭据。安装后可从应用列表打开 **TigerBrowser**。Linux 本机运行带认证代理的环境需要已登录桌面会话中的原生钥匙环。依赖、中文字体修正与验证范围见 [Ubuntu 安装说明](docs/studio-ubuntu.md)。

### 第一次使用

1. 管理员在“代理资源”添加 HTTP、HTTPS 或 SOCKS5 代理，也可以使用直连。见[代理配置](docs/studio-nodes.md)。
2. 创建环境，选择目标系统、语言、时区和需要的指纹选项，绑定代理。
3. 在“团队管理”添加成员并授予环境权限；需要在成员电脑上运行时授予“本机运行”。
4. 启动环境：在服务器上无头或远程运行，或在桌面客户端点击“本机启动”在本电脑运行。第一次启动生成身份，以后继续使用同一身份和浏览器数据。

需要后台运行时，选中环境点击“无头启动”，仅本次使用无头模式。要长期使用，
在“编辑 → 启动行为”勾选“默认无头启动”，之后点击“启动”即可。
启动页面、Cookie、站点存储、固定指纹和代理绑定沿用原环境；切换模式需要先停止。

## 客户端安装包

正式安装包在 [GitHub Releases](https://github.com/zhaotianshi/TigerBrowser/releases) 提供下载。

成员通常从服务器的“客户端下载”页安装打包好的客户端：Windows 10/11 x64 为按当前用户安装的 Inno Setup 程序（无需管理员权限），macOS arm64/x86_64 为 `.dmg`，Ubuntu 24.04 x64 为 `.deb`。安装包包含 Python、Qt（含 QtWebEngine）、Playwright 驱动、指纹模型、默认扩展；浏览器内核不随包分发，本机运行时按环境从服务器取得。安装包未签名，Windows 首次运行会出现 SmartScreen 提示，macOS 需右键选择“打开”。代理资源支持 HTTP、HTTPS 和 SOCKS5，可逐行批量导入；参数与限制见[网络连接说明](docs/studio-nodes.md)。

登录后客户端读取服务器提供的版本：与服务器协议不兼容时只显示下载页；有兼容的新版本时可在“客户端下载”页一键更新，客户端校验摘要后退出并安装。HTTP 下安装包与摘要来自同一明文连接，无法防止中间人替换；需要传输保护时启用 HTTPS。

每个平台须在各自系统上按[客户端安装包构建说明](docs/studio.md#client-packages)准备锁定依赖和打包工具，再执行构建。该文档保留完整安装命令及平台前提。

输出在 `dist/studio-client/`：安装包及记录版本、平台、大小、SHA-256 和协议范围的 `.json`。管理员把它们复制到服务器的客户端目录，运行 `python -m camoufox studio-client-manifest <客户端目录>` 校验并生成清单，再以 `studio-team --client-root <客户端目录>` 提供。详见[客户端安装包](docs/studio.md#client-packages)。

源码检查继续验证四个平台，但不上传 Actions 产物。正式安装包由独立发布流程直接交付到 GitHub Releases，四平台及摘要核验完成后才公开；容量不足时先更新验证通过的源码，暂缓安装包发布。详见[发布规则](docs/studio-releases.md)。

安装包不会携带环境数据、本机缓存或系统凭据。客户端按服务器在用户数据目录（Windows 为 `%LOCALAPPDATA%\TigerBrowser\servers\<工作区 ID>\`）保存签出过的环境、设备标识和本机设置；更新和卸载都不影响它。操作员只可启动、停止获得授权的环境，只读成员不能操作；签出到本机还需要“本机运行”授权。详见[本机运行说明](docs/studio.md#running-on-this-computer)。

## 数据位置

服务器工作区是 `studio-team --data-dir` 指定的目录，请放在版本控制之外：

```text
<工作区>/
├── studio.sqlite3        # 环境配置、身份、代理元数据、成员、授权、设置和审计
└── profiles/
    └── <environment-id>/ # Cookie、缓存、历史和网站存储
```

完整备份请使用[加密快照与隔离恢复](docs/studio-backup.md)，先停止团队服务和浏览器；有签出未签回的环境时快照会被拒绝。换服务器使用[停机迁移](docs/studio-migration.md)：保留固定身份、网站数据和团队账号，旧端退役后新端重新绑定本机运行条件；默认要求相同 OS/架构及一致的核心制品。同架构跨 OS 迁移须显式接受平台差异，匹配保存的浏览器版本，并重新检查目标机的实际运行条件；迁移激活不依赖测量证书。原目录保留而不删除。代理密码仍在原生凭据服务中，不进入快照，恢复端需重新录入。不要并行启动同一身份的手工副本。

## 当前范围

跨 OS 交接明确接受的新主机资源也适用于已迁移固定环境的原生字体等设置，
具体许可条件和可选测量见[迁移策略](docs/studio-migration.md#接受哪些差异)。
网页环境列表可直接在每行启动、无头启动或停止；上方管理工具栏常驻，多选后显示批量启停。
列表采用更易读的字号与分层深色界面，桌面操作列固定在右侧，手机窄屏显示环境卡片。
批量启动按环境报告拒绝原因；请求已接受后仍需等待实际“运行中”状态。
管理员可停止环境并手动切换代理，无需先取得设备测量证书，
保留身份和网站数据；启动重新检查当前代理与时区，不因换 IP 要求重复设备验收。

桌面之外已增加[远程管理预览](docs/studio-team-service.md)：网页登录、成员/组授权、
实时状态，以及固定执行机的逐运行启停、环境/代理/Cookie/设置/内核与配置管理。
Linux 的 `--browser-mode remote`
提供原生浏览器全屏画面、独占鼠标/键盘、管理员接管、显式中文文本与受限文件交换；
数据留在执行机，客户端缩放不改变身份尺寸。默认仅监听本机并禁用任务执行。
网页以独立操作窗口显示，默认 100% 原始大小，支持一键全屏、适应窗口和可收起工具；
Linux 主窗口铺满既有固定屏幕，团队服务器无需完整桌面。输入法与配套
中文字体的使用和排查见[远程窗口说明](docs/studio-remote-view.md)。
关闭观看窗口不停止浏览器；在远程画面内关闭浏览器最后一个标签页会正常停止环境，
资源清理完成后可再次启动，保留固定身份和已保存的网站数据。
Linux 键鼠输入复用每环境常驻 XTest 进程，原生确认后才推进输入及释放；
该进程异常会停止所属环境。Linux 安装并通过校验的
[固定 KasmVNC 组件](docs/studio-kasmvnc-runtime.md)默认用于后续远程启动，在现有 Vue
控制台显示增量 JPEG/WebP 画面。KasmVNC 是唯一远程传输；组件缺失或校验失败时服务拒绝启动。
当前要求 `1.5.0+studio.2`；旧服务器需按组件说明在新目录构建并切换运行组件，修复启动时的系统库目录扫描。环境数据与身份保持不变。
账号、控制权、中文输入及文件流程保持一致。
Vue 控制台使用共享鼠标移动队列，保留最后位置；点击和按键前保序发送，控制权变化后清除待发移动。
新版网页支持最多 1 MiB 的完整文本发送及 Linux 环境内容复制至本机，普通窗口下也可使用；
环境启动有加载动画，实际运行结果自动更新列表。
唯一 Vue 前端随 Python wheel 和桌面包提供。源码检出须先运行 `npm --prefix frontend/studio run build`，再启动 `studio-team`；缺失构建时明确失败。步骤见[团队服务](docs/studio-team-service.md#使用新版前端)。
现有可调用接口及仍保持的离线安全边界见[团队 API 契约](docs/studio-team-api.md)。
管理员完整环境详情、成员只读远程连接信息接口已提供；新版前端可保留
`/#remote=<环境 ID>` 独立窗口链接，再解析当前运行，打开窗口不启动浏览器或申请控制。
默认 HTTP、可选网络范围、Linux 显示前提、测量基线和显式崩溃恢复见[原生部署验收](docs/studio-native-acceptance.md)。
停止后修改分辨率保留其它固定指纹；新版验收独立核对所选屏幕尺寸，无需重复人工验收。
管理员只通过服务器本机的 `studio-team-init` 创建，口令在终端隐藏输入，不提供远程初始化。尚不包含：

- Windows 服务器上的远程执行、完整显示/GPU/字体及反检测认证、云端同步或计费；
- 安装包签名与公证；
- 不经来源退役的跨机器直接激活、自动移植原生运行基线或失联主机自动接管；
- 为每个环境伪造任意 TLS ClientHello；
- 修改 MAC 地址、系统主机名或物理内存；
- 系统级网络隧道、虚拟网络接口、系统防火墙或对其他应用的网络隔离。

TigerBrowser 管理基于 Camoufox 的浏览器环境。代理配置作用于选定环境。

## 文档

- [服务器与客户端开发设计：部署规范、签出协议、多版本内核、三端客户端与代码清理（实施中）](docs/studio-client-server.md)
- [TigerBrowser 完整使用说明](docs/studio.md)
- [成员组、批量授权与登录撤销](docs/studio-member-groups.md)
- [代理配置](docs/studio-nodes.md)
- [环境参数与内核能力](docs/studio-capabilities.md)
- [自动化对接：服务器 API 密钥、本机接口、鉴权、借用与 Node.js 示例](docs/studio-integration.zh-CN.md)
- [部署前检查与工作区互斥、故障恢复](docs/studio-preflight.md)
- [团队远程管理预览：安装、网页控制台、接口、安全边界与未完成项](docs/studio-team-service.md)
- [Vue 控制台：代码入口、本地联调、构建与回归](frontend/studio/README.md)
- [加密快照、隔离恢复、无头运行基线与同主机独占切换](docs/studio-backup.md)
- [更换服务器：停机迁移、来源退役与新端运行基线](docs/studio-migration.md)
- [团队版架构决策记录：语言、复用边界、数据流与远程画面](docs/studio-team.md)
- [CI 与测试流程](ci/README.md)
- [构建系统与贡献规则](CONTRIBUTING.md)

## 开发与测试

代码位于 `pythonlib/` 与 `frontend/studio/`；浏览器使用上游发布资产，不在本仓库编译。
升级按[服务器与客户端设计](docs/studio-client-server.md)分批推进，未实现的能力以任务表为准。
开发前阅读 [AGENTS.md](AGENTS.md) 与 [既定规则](ci/tribal-rules.yml)。

在隔离环境中安装测试依赖，先运行受影响的测试文件，再运行相关门禁：

```bash
python -m pip install -r ci/requirements.txt -c pythonlib/team-ci-requirements.lock -e './pythonlib[geoip,studio,team]'
python scripts/pin-fpgen-model.py
python -m pytest ci/tests -q
python -m ci.run_pythonlib
npm --prefix frontend/studio ci
npm --prefix frontend/studio test
npm --prefix frontend/studio run build
```

测试输出写入临时日志，检查退出码与失败项后删除日志。原生运行验收见
[service-tester](service-tester/README.md)，Windows 客户端打包步骤见上文。
云端检查仍名为 **All tests passed**；工作流不可用时须明确区分本机验证与云端 CI 状态。

## 上游与许可证

TigerBrowser 是 [daijro/camoufox](https://github.com/daijro/camoufox) 的衍生项目。感谢 Camoufox、Firefox、Playwright、fpgen、PySide6 及其他第三方组件的贡献者提供的基础工作。

仓库根目录使用 [MPL-2.0](LICENSE)。Python 包保留自身的 [MIT 许可证](pythonlib/LICENSE)。原项目及第三方组件的版权和许可证声明继续保留；独立桌面包也会收集随运行时分发的依赖许可证。
