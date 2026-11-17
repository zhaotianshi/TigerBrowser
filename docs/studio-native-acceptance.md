# 原生远程部署、可选测量与崩溃恢复

本文的远程模式让浏览器在固定执行机运行；成员电脑接收画面、发送输入和交换授权文件。
另行签出到本机的流程见[客户端说明](studio.md#running-on-this-computer)，不属于远程显示验收。
兼容 Windows/Linux 客户端不表示允许跨系统搬动活动 Profile。以下能力与正式反检测
认证不同：测量一致性不能证明任意网站均无法检测，也不能认证未运行的检查。

## 默认 HTTP 与可选网络范围

服务默认在 `http://127.0.0.1:8766` 监听，不要求证书、白名单或额外传输开关。
跨电脑连接时，Origin 必须与实际访问地址和端口完全一致，并显式选择外部监听：

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace --origin http://studio.example:8766 --listen 0.0.0.0 --browser-mode remote
```

需要只限私网时加 `--private-network-only`，直接对端只能为回环、RFC1918 IPv4 或 IPv6 ULA。
需要更窄的范围时，重复使用 `--allow-client`，参数只接受单个 IP 或 CIDR：

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace --origin http://192.168.10.20:8766 --listen 0.0.0.0 --private-network-only --allow-client 192.168.10.0/24 --allow-client 192.168.20.15 --browser-mode remote
```

私网开关与白名单同时使用时取交集。请求、管理状态 WebSocket 和原生画面 WebSocket 使用同一
直连对端规则；服务不信任 `X-Forwarded-For` 等代理头。默认不会限制客户端网段，也不会
自行开放防火墙。需要 HTTPS 时把 Origin 改为 `https://` 并同时提供 `--cert` 和 `--key`。
HTTP 发送密码、会话 Cookie、画面和文件时没有加密；同网段攻击者仍可窃取或修改
流量。来源/CSRF/成员授权、私网开关和 IP 白名单都不提供传输加密。需要防止路径上窃听或篡改时，
必须配置受客户端信任的 HTTPS。控制台在 HTTP 下持续显示风险提示。

请求 UUID 使用 Web Crypto 的安全随机字节，不依赖只在安全上下文提供的
`randomUUID()`；HTTP 下启动、停止仍有同样的持久化幂等约束。

## 远程窗口与 Linux 显示前提

“打开操作窗口”将同一运行放在独立网页窗口，可一键全屏、发送中文和重新连接。
客户端缩放不修改执行机显示或保存的身份；关闭画面不停止浏览器。输入法特殊事件
不直接转发，不支持的输入不会关闭 WebSocket。具体操作和回归边界见
[远程窗口、输入法与中文显示](studio-remote-view.md)。

Linux 原生模式使用每环境私有 KasmVNC 显示，不要求 Ubuntu Desktop、GNOME、WSLg 或 Qt。
浏览器系统库、用户命名空间、原生工具和匹配的字体制品仍是前提。旧制品字体名称
与保存集合不匹配可以导致汉字缺失；系统安装字体不能替代真实浏览器测试。
更换字体资产应停机并按部署流程测试；不替换保存身份，也不把历史测量当作启动门禁。

唯一 KasmVNC 传输使用同一原生会话、私有输入及成员授权，以增量 JPEG/WebP 传输
替代 PNG 采集。安装入口和固定版本见 [运行组件说明](studio-kasmvnc-runtime.md)。
唯一 Vue 控制台使用 KasmVNC；缺少固定组件时 remote 服务启动失败。真实回归入口为
`studio_kasmvnc_tests.py`、`studio_kasm_gateway_tests.py` 和 `studio_kasm_client_tests.py`，
命令见 [service-tester](../service-tester/README.md)。WSL 本地功能通过不代表目标
NAS 的性能、弱网、长时间运行及物理客户端认证完成。

## Windows 客户端

Windows 可使用桌面或网页客户端；远程执行统一在 Linux KasmVNC 服务器进行。
旧 Windows 专用会话串流与 `--windows-user` 已删除，不再采集 Windows 宿主桌面。

## 可选诊断测量

停机后选择一个已有固定身份的环境。Linux 仍需固定 KasmVNC 组件、xauth/xdotool/bubblewrap/xclip
和团队依赖。在 Bash 中输入环境 UUID：

```bash
read -r profile_id
python -m camoufox studio-attest --data-dir ./my-team-workspace --profile "$profile_id" --confirm-current-runtime
```

这是维护人员主动运行的诊断命令，不是启动授权。正常启动不读取它产生的证书；
重启服务、调整虚拟机资源或宿主运行条件都不会因历史测量要求管理员重新确认。
命令独占工作区并使用与服务相同的内核进程边界；不打开环境的启动 URL，不生成
替代身份。现有诊断采集器在独立合成页面的内容世界运行，未打开全局
主世界自动化。两次独立浏览器启动各采集两次，11 组必须完整、可测且一致：
navigator、screen、timezone、WebGL、canvas、离线 audio、字体度量、client rects、
emoji canvas、字体可用性及语音列表。实时音频分析器的时序和本地时钟不作为身份
摘要；离线音频的两种读取必须一致。合法的空语音列表按实测绑定，测量异常不能
变成空值通过。改变设备集合、内核/扩展/模型、软件包、原生工具或字体资产时，
原诊断结果不再可用于对比，但不会拒绝启动。证书将设备表面与网络表面分别记录：设备摘要保持固定，
时区名称和 UTC 偏移按本次已检查的代理出口或明确指定的时区核对；使用浏览器的
Intl 规范化名称处理别名，并使用 IANA 规则检查当前偏移和夏令时。
管理员在停止状态手动切换代理或修改代理参数，不重生成设备身份，也不要求因
新 IP 或自动时区变化重新验收。节点失败仍中止启动，不更换节点或改用直连。
停止环境后修改屏幕策略或自定义分辨率，保留原设备身份、字体、GPU、音频和代理绑定，
不要求重新测量。启动配置按原身份记录的任务栏和窗口边距应用所选尺寸；Linux 私有
显示与远程画面使用同一尺寸。每次启动仍核对实际屏幕宽高及窗口边界，色深和像素
比例仍属于固定设备测量。服务器显示尺寸仍须与所选尺寸一致，
不会自动修改整机分辨率。设备选项变化后，只有在需要新的诊断对比时才重新运行命令。
迁移由源端退役、加密收据和目标端激活确保唯一所有权，不依赖测量证书，激活后也不要求逐环境重新验收。

登记生成格式 3 诊断记录，要求两次启动、四次读取的全部 11 组一致。
旧格式记录只供历史对比；不会在普通启动时验证、升级或拒绝环境。
维护命令仍需要停服务、持有工作区锁并确认运行均已停止，避免诊断本身与正在运行的浏览器竞争。

仅把聚合摘要、身份配置绑定、制品清单、主机标识和接受时间存入工作区，不把
原始测量数据写入审计或公开报告。清单不包含密码或环境变量全集。采集器由
`npm --prefix frontend/studio run build:probe` 从
`frontend/studio/src/diagnostics/collectors.ts` 生成，需要先运行
`npm --prefix frontend/studio ci`；源摘要回归防止打包副本与采集源漂移，不新增第二套算法。

普通启动只使用当前保存的身份、制品和代理路由，不会在打开启动页前重放诊断测量，也不会把迁移用的运行清单当作启动许可。
修改固定设备配置、字体或驱动后仍应停机调查并运行适用测试，但不会产生管理员复验门禁。

这不是完整 GPU 驱动/显示硬件清单或所有反检测向量的认证。浏览器构建与补丁门禁
已随批次 A 移除；本仓库的 Studio 运行测试不能认证官方内核的全部行为，也不得以 11 组
稳定性验收作完整认证。`studio-check` 仍只报告静态前提，`remoteReady` 不因文件存在
或配置通过而变成 true。

## 显式崩溃恢复

`studio-team` 与可选诊断命令创建版本 2 的锁证明：Linux 管理器是私有 PID
命名空间的 PID 1；Windows 管理器持有禁止脱离且随最后句柄关闭终止的 Job。
管理器异常退出后，内核终止其后代，包含另建进程会话的浏览器/代理/显示工具。
正常退出仍先清理资源，再释放工作区锁。恢复不会按进程名称批量结束其它程序。
Linux 包装器通过 bubblewrap 的私有进程信息及 pidfd 转发 Ctrl+C/SIGTERM，并等待
管理器清理；不会先杀监视器触发整树强制退出。需要支持 pidfd 的 Linux 内核。

```bash
python -m camoufox studio-recover --data-dir ./my-team-workspace
python -m camoufox studio-recover --data-dir ./my-team-workspace --apply
```

第一条只检查。第二条要求主机标识、锁证明和原管理进程身份一致，并证明边界
已停止：Linux 使用启动标识、命名空间及 PID 创建时刻；Windows 使用创建时间及
Job 活动进程数。PID 复用不是结束其它进程的依据。权限不足、证据变化、仍存活
或旧版锁没有证明时均拒绝恢复；不要删除锁或改数据库来绕过。

恢复事务把属于原所有者的未结束运行标记为停止，把未确认命令标记为结果未知，
撤销所有登录，并把锁原子移入工作区 `recovery` 证据目录。不会自动重放命令、
重置身份、删除 Firefox 锁或解除备份隔离/来源退役屏障。运行归属不匹配即拒绝。
仅撤销普通登录会话；长期 API 密钥及其内部会话、授权范围、到期时间和撤销状态保持不变。
恢复后网页须重新登录；自动化调用方无需因部署或恢复更换密钥，已撤销或到期的密钥不会复活。
若数据库提交后文件移动失败，旧锁仍阻挡启动；同一恢复可再次执行，不可手工
补启动。浏览器未提交到磁盘的数据仍可能丢失；不能承诺强杀后的所有输入都耐久。

客户端缓存锁和早期版本留下的锁按[本机维护流程](studio-preflight.md#工作区互斥与故障恢复)
人工检查，不能追认成受内核约束的团队锁。失联主机、跨主机灾难接管和系统迁移
不属于此恢复协议。
旧服务器可操作时可使用[停机迁移](studio-migration.md)。旧端退役后，兼容的新端
重新绑定本机路径和运行清单；旧原生测量记录可保留在迁移历史中，但不影响新端启动。

## 已有证据与部署门禁

2026-10-17：当前 `official/152.0.4-beta.31` Linux x86_64 制品的中文字体门禁失败。
重新下载[官方归档](https://github.com/daijro/camoufox/releases/download/font-bundle-v1/camoufox-152.0.4-beta.31-lin.x86_64.zip)
得到 663,476,534 字节，SHA-256 为
`3a7958c84c0c1962574bb177a10a247b614823557308ccb7cf2e2bc9ce406c55`，与官方摘要一致；
归档内 143 个 Linux 字体与验收副本逐文件一致，但不包含要求的中文族。
`studio_native_tests.py::test_native_bundle_resolves_declared_chinese_font_and_covers_han_glyphs`
在实际浏览器专用 Fontconfig 下未能解析 `Noto Sans CJK SC`。没有混入系统字体、
放宽断言或替换身份；因此不能将下方历史字体结果用于认证这个制品。
须取得匹配保存身份的字体制品并重新通过真实字形检查，才能完成中文字体验收。

此项保持未完成，范围是 Linux 身份使用的字体集合，不是所有运行在 Linux
执行机上的环境。Windows 字体集合能显示中文，或控制台自带的 Noto Sans SC
网页字体显示正常，都不能替代这项验收。后续须确认匹配的官方制品在浏览器
实际 Fontconfig 下解析到所声明的中文族，覆盖测试汉字，并通过上述原生回归；
不以系统安装字体、改写保存身份或放宽断言代替。当前仅记录待验收事项，未修改字体资产。

2026-10-04：Ubuntu 24.04 / WSL 的真实浏览器完成双启动四读、登记后启动及基线
篡改拒绝；原身份保留。Linux 完成真实浏览器强制退出、确认全部子进程退出、
恢复运行记录及再次打开已提交 Cookie 的演练。Windows 完成实际 Job 后代退出、
真实 NTFS 文件保护、输入 ABI/编码和回调失败验证，未使用当前办公桌面采集画面。
历史 PNG 预览曾完成 Windows 客户端通过 WSL 私有 IP 的登录、中文上传与停止；该结果不认证当前 KasmVNC。
Linux 原生回归另验证了独立窗口、全屏、输入法无效事件保持连接、中文发送、
中文字体实际名称与汉字覆盖；客户端快捷键仍需用其实际输入法复测。
以上不是两台物理电脑的验收。第二台物理电脑及正式 HTTPS/WSS 部署
仍是独立验收门禁；HTTP 局域网模式不把这些记录冒充已通过。

依据：[Linux PID 命名空间](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)、
[Windows Job](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、
[原生输入](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、
[NTCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)、
[Web Crypto 随机数](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/getRandomValues)。
