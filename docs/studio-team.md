# Studio 团队版：架构决策记录

本文只记录团队服务已定下的架构决定及理由。安装与使用见[团队服务](studio-team-service.md)，
接口见[团队 API](studio-team-api.md)，服务器、客户端与签出的目标形态和实施进度见
[服务器与客户端设计](studio-client-server.md)，已定规则及证据见[既定规则](../ci/tribal-rules.yml)。

## 1. 产品边界

团队成员从不同电脑登录同一服务器，访问获授权的浏览器环境。活动环境只在一个位置运行：
服务器（成员通过 KasmVNC 画面操作），或持有签出租约的那一台成员电脑。
换操作电脑不重新生成身份；签出复制网站数据并在签回后以服务器为准，详见[签出协议](studio-client-server.md#5-签出协议)。

范围是一个团队、一个控制服务、多个成员与多个持久化环境。不同环境可并发运行；
同一环境同一时刻只有一个输入控制者，可授权其他成员观看并显式交接。
这是可信团队内的访问控制，不是供互不信任租户运行任意代码的隔离平台；宿主机管理员
仍能访问本机数据，团队权限不取代操作系统或浏览器沙箱。

不包含：多执行节点与自动迁移、高可用控制服务、多租户计费、公开注册、SSO，以及开放
任意 Playwright/WebSocket 控制端点；不承诺通过所有网站的身份检查。

## 2. 保留 Python，不引入 Go

主要约束是复用正确的浏览器生命周期和身份配置，不是重新搭建 HTTP 服务。

| 比较项 | Python 控制服务 | Go 控制服务 |
| --- | --- | --- |
| 复用现有引擎 | 直接调用 `BrowserEngine`、`ProfileStore`、代理和凭据模块 | 仍需 Python 执行服务及跨语言契约，或重写这些模块 |
| 一致性维护 | 桌面、客户端和服务器走同一生成、验证、启动路径 | 重写会产生两套配置与错误处理 |
| 并发模型 | asyncio，阻塞操作放到受限线程 | goroutine，但浏览器和画面的资源消耗不变 |
| 当前证据 | 已有引擎及回归测试 | 没有测量证明 Python 控制层是瓶颈 |

服务采用 FastAPI/ASGI，Uvicorn 单 worker；一个进程持有工作区和 `BrowserEngine`，
禁止通过增加 worker 产生多个引擎所有者。数据库、代理探测、密码计算和归档不能阻塞
事件循环。只有测得控制层 CPU、事件循环延迟或连接开销成为瓶颈后，才评估 Go 网关；
即使采用，Python 执行器仍是唯一的身份生成和浏览器启动实现。
依据：[asyncio](https://docs.python.org/3/library/asyncio.html)、
[FastAPI 异步说明](https://fastapi.tiangolo.com/async/)。

## 3. 复用而不复制

| 实现 | 团队版增加的边界 |
| --- | --- |
| [engine.py](../pythonlib/camoufox/studio/engine.py) | 工作区所有权、持久化运行记录、显示资源注入 |
| [store.py](../pythonlib/camoufox/studio/store.py) | 成员授权、修订号、运行记录、签出租约和审计，复用已有字段验证 |
| [access.py](../pythonlib/camoufox/studio/access.py) | 唯一的成员与权限解析，网页、API 密钥和客户端共用 |
| [secrets.py](../pythonlib/camoufox/studio/secrets.py) | 原生系统凭据，禁止明文回退 |
| [client/shell.py](../pythonlib/camoufox/studio/client/shell.py) | 只在桌面客户端使用；服务器不导入 Qt |
| [server.py](../pythonlib/camoufox/server.py) | 非持久化 Playwright server，不能冒充团队环境共享端点 |

团队专用模块位于 `pythonlib/camoufox/studio/team/`，依赖由 `team` extra 和独立锁文件隔离。
同一工作区只能由一个进程直接管理；服务、维护命令和每个客户端缓存都使用
`WorkspaceLock`，异常关闭后保留屏障，不自动清除旧锁，见[部署前检查](studio-preflight.md)。
客户端不直接打开服务器工作区，只通过签出租约取得环境数据。

## 4. 服务与数据流

所有成员请求依次经过身份认证、环境授权、控制权校验，再到同一个浏览器引擎。
画面连接经过同样的授权边界，不能凭环境 ID 直连显示服务。

| 层 | 职责 | 不承担 |
| --- | --- | --- |
| Vue 控制台 | 成员、环境、控制者、运行状态、远程画面 | 保存代理密码、决定运行锁是否释放 |
| 团队服务 | 会话、权限、命令、审计、控制与签出租约、恢复协调 | 重新生成已有身份、信任前端权限判断 |
| 执行层 | `BrowserEngine`、浏览器、代理、每环境显示服务 | 对局域网开放调试端口或共享宿主桌面 |
| 持久化层 | 本机 SQLite、环境目录、身份记录、备份 | 让客户端直接读写数据库或运行中的 Profile |

数据库与访问它的进程在同一主机，远端只发送应用请求，符合
[SQLite 的网络访问建议](https://www.sqlite.org/useovernet.html)；不采用共享盘数据库，
不预先引入 Redis、PostgreSQL 或消息队列。

## 5. 共享身份不等于复制几个参数

一个环境由固定 Profile ID 与完整身份、真实网站数据、精确内核版本、字体包与扩展集合、
代理绑定及语言和时区策略共同定义。环境记录精确内核 ID，不能仅凭相同 Firefox 大版本认定
环境相同；历史记录缺失时由管理员明确选择，不填入虚构值，也不重新抽取身份来“修复”。

[能力矩阵](studio-capabilities.md)和既定规则仍然适用：Canvas、WebGL 像素和布局保留真实渲染。
不把操作员电脑的屏幕大小、字体或时区覆盖到环境；远程画面在客户端缩放，不随窗口修改
服务器显示尺寸。代理检查失败不回退直连。

## 6. 远程画面：只用 Linux KasmVNC

远程串流只在 Linux 服务器提供，每环境一个私有 KasmVNC 显示、随机 Xauthority 和
bubblewrap 文件命名空间，画面与输入都经过团队鉴权网关，不向成员暴露 VNC、X11 或 CDP。
Windows 不提供远程执行：服务与交互用户会话分离
（[Microsoft 说明](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)），
专用会话方案无法做到每环境隔离，已退役；需要在 Windows 上运行的成员签出环境在本机启动。
早期的 Xvfb + PNG 采集路径已被 KasmVNC 增量编码取代。

显示服务只监听回环或私有套接字，禁止一套共享 VNC 密码暴露所有环境，只读由服务端执行，
不靠前端隐藏按钮。软件 X11 渲染没有 GPU 直通，功能可用不等于渲染指纹认证；
组件与限制见 [KasmVNC 运行组件](studio-kasmvnc-runtime.md)和[远程窗口](studio-remote-view.md)。

## 7. BrowseForge 的参考范围

参考公开项目 [BrowseForge](https://github.com/nczz/BrowseForge)，分析基线为
`fa9e0bb670e749706a9c8f2d4095333347d6c521`。

- 借鉴统一 Dashboard/API、Profile 与 Session 分层和按环境复用运行实例。
- [路由认证](https://github.com/nczz/BrowseForge/blob/fa9e0bb670e749706a9c8f2d4095333347d6c521/internal/api/router.go)
  使用全局 Token，不是本设计需要的成员和环境权限。
- [会话管理](https://github.com/nczz/BrowseForge/blob/fa9e0bb670e749706a9c8f2d4095333347d6c521/internal/browser/manager.go)
  的进程内互斥不能充当跨进程锁。

本设计不移植其 Go 启动器、指纹池、共享管理员 Token 或全员共用桌面。
