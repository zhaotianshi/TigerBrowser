# Studio 团队远程预览

状态：网页登录、成员组授权和持久执行已实现。Linux 原生远程预览增加完整浏览器
画面、鼠标/键盘、独占控制与管理员接管、显式文本粘贴和受限文件传输。客户端只
接收画面，浏览器与网站数据始终在同一执行机，不复制 Profile 到成员电脑。
Windows/Linux 均可作为网页客户端；远程执行仅支持 Linux KasmVNC，桌面客户端可签出环境在成员电脑本机运行。
已增加默认本机 HTTP 及可选网络范围、11 组测量基线及内核归属恢复；完整反检测认证和生产部署
门禁仍未完成，不能承诺指纹不会变化。使用方法见[原生部署验收](studio-native-acceptance.md)，
实施进度与验收见[服务器与客户端设计](studio-client-server.md#12-实施任务与验收)，架构决定见[架构决策记录](studio-team.md)。

开发与前端接入见[现有 API 契约](studio-team-api.md)。
管理员环境详情、成员远程连接、代理、Cookie、内核与身份接口已补齐；
唯一 Vue 前端位于 `frontend/studio/`，构建后随 Python 包同源提供，
代码入口、联调与测试见[前端说明](../frontend/studio/README.md)。

### 使用新版前端

源码检出在仓库根目录构建；既有工作区和 Python 团队依赖须已准备好。安装完整 wheel 时无需 Node：

```bash
npm --prefix frontend/studio ci
npm --prefix frontend/studio run build
python -m camoufox studio-team --data-dir ./my-team-workspace
```

服务按 Vite 清单提供 HTML、JS、CSS 及固定公共字体/图标，清单缺失、非法路径或
资产缺失会拒绝启动。不开放任意宿主文件；Origin、CSRF、CSP 和 WebSocket 校验仍适用。
`--web-root` 已移除；服务只使用包内 Vue 构建。wheel、sdist 与桌面包携带同一产物及
KasmVNC 许可和来源文件；源码构建输出目录不进 Git。
环境表格每行的“操作”列提供启动、无头启动与停止，无需先勾选；点击只作用于该行，
不改变已勾选项。按钮按成员权限、服务能力、签出占用与运行状态启用，请求期间禁止重复提交。
上方管理工具栏常驻，编辑、复制、归档、删除、详情与管理员 Cookie 入口不因未选择而隐藏；
未选择时置灰，选中后按既有权限及运行状态启用。多选后增加批量启停按钮，分别报告已接受的请求与拒绝原因，
并发上限不提供排队启动。原代理故障或执行机启动失败会显示对应原因；一次启动请求
被接受并不代表浏览器已经运行，须等待“运行中”。管理员可在停止状态手动切换代理；
启动仍检查所选代理并使用当前出口或指定时区。历史诊断测量和备份运行清单不作为
日常启动门槛；工作区归属、故障恢复屏障、原生会话安全与迁移退役条件仍独立检查。

### 时区选择

环境编辑的“网络与系统”页提供按地区分组的时区搜索。选择“自动（跟随代理出口）”并保存，
会清除原先指定的时区。旧的有效别名即使不在选项中也保留为“已保存”。绑定代理若有成功
探测记录，会显示当时的出口时区与时间；这是历史观察，启动时仍重新检查当前出口。
修改代理后旧提示失效。时区索引缺失时先修复服务器 tzdata 安装，
不要通过更换前端列表绕过错误。

## 安装和首次使用

以下命令在仓库根目录运行，Windows 和 Linux 使用同一入口。`python` 应指向已安装
项目依赖的 Python 3.10+ 环境；不要使用其他来源的 Camoufox 包替代本仓库。

```bash
python -m pip install -r pythonlib/team-requirements.lock
python -m pip install --no-deps -e ./pythonlib
```

团队额外依赖也可通过 `python -m pip install -e "./pythonlib[team]"` 安装；部署时优先
使用锁文件。这个 extra 不包含 Qt。新服务器工作区在本机终端创建首次管理员：

```bash
python -m camoufox studio-team-init --data-dir ./my-team-workspace --name admin --display Administrator
```

目录必须尚不存在，其父目录必须已存在。终端隐藏输入密码并要求确认；不要把密码
放进命令行参数或脚本。Linux 新目录权限为 `0700`；Windows 应在维护账号独占的
父目录下创建工作区。初始化使用同一存储和工作区锁，不打开监听端口。数据库初始化
失败时保留新目录供本机检查，不自动删除或覆盖，也不重试已有目录。

早期版本桌面留下的无成员工作区保留环境与网站数据，停机后显式加 `--existing` 创建首个管理员：

```bash
python -m camoufox studio-team-init --existing --data-dir ./my-studio-workspace --name admin --display Administrator
```

该模式只接受已含 `studio.sqlite3`、尚无成员且未被占用的目录；已有成员时拒绝并保持原账号。
服务不提供公开注册、默认口令、远程管理员初始化或自动接管旧数据的入口。

已有团队工作区无需证书或白名单即可在本机启动：

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace
```

打开 `http://127.0.0.1:8766`，使用本机创建的账号登录。管理员可创建环境和成员，
编辑组成员、组批量授权、逐环境直接授权，停用成员、重置密码并查看最近审计。
成员可搜索自己的环境，查看运行状态和修改自己的密码。列表显示归档环境时不会
扩大授权；成员组与环境分组仍是两种对象。密码输入使用遮罩，不保存到 localStorage。
跨机 HTTP 设置匹配的 `--origin` 并显式使用 `--listen 0.0.0.0`；可选
`--private-network-only` 和可重复的 `--allow-client IP_OR_CIDR` 限制直连客户端。
参数组合及明文风险见[原生部署验收](studio-native-acceptance.md#默认-http-与可选网络范围)。

默认 `--browser-mode disabled`，只提供管理功能。若要验证无头任务，显式增加
`--browser-mode headless`。它调用现有 `BrowserEngine`，保持同一 Profile 和身份，
不会提供可交互画面，也不是有头远程会话的降级实现。首次启动仍需预先安装内核、
模型和默认扩展；代理验证失败不会回退为直连。不要在未验证的平台上将无头结果
等同于原生画面或指纹一致性验收。

同机与跨电脑的自动化应用使用成员 API 密钥。启动时添加 `--automation-listen`，
另开默认 8767 端口，地址、TLS 和来源网络限制与网页服务共用。成员在 Vue“API 密钥”
下载一次性显示的连接文件；服务不写入共享发现令牌。启动仍经过团队授权、
持久运行记录和执行安全检查。详见[成员 API 密钥](studio-automation.md#team-service)。

## Ubuntu 服务器安装

全新的 Ubuntu 24.04 x86_64 服务器可用安装脚本部署为 systemd 服务。以 root 在源码检出中运行，
`--origin` 换成成员地址栏中的实际地址：

```bash
sudo python3 scripts/install-studio-server.py --origin http://192.168.1.10:8766 --listen 0.0.0.0 --private-network-only
```

脚本依次安装 `bubblewrap xauth xdotool xclip fontconfig dbus-daemon gnome-keyring` 与 Playwright 的 Firefox 系统依赖，
创建非 root 服务账号 `camoufox` 和以下目录，并以服务账号运行 `camoufox fetch`，
把当前默认内核的四平台包缓存到 `kernels/`：

```text
/opt/camoufox-studio/.venv          按 pythonlib/team-requirements.lock 安装的程序
/etc/camoufox-studio/studio.env     启动参数（root 所有，服务账号只读）
/etc/camoufox-studio/keyring-unlock.cred  systemd 加密的钥匙环解锁凭据（root 所有，0600）
/var/lib/camoufox-studio/           0700，服务账号所有
├── .local/share/keyrings/           原生加密钥匙环，不属于工作区快照
├── workspace/                      工作区
├── staging/                        签回暂存，与工作区同一文件系统
├── kernels/                        各平台内核包
└── clients/                        客户端安装包与 manifest.json
```

源码检出尚未构建前端时，脚本以调用 `sudo` 的用户运行 `npm ci` 和构建（需要 Node.js 22）。
默认 `--browser-mode remote` 会按[固定运行组件说明](studio-kasmvnc-runtime.md)构建 KasmVNC 到
`/opt/camoufox-studio/`，并以服务账号执行生产命名空间命令；Ubuntu 的 AppArmor 拒绝时安装失败，
按该文档为 bubblewrap 加载发行版策略后重跑，脚本不修改宿主策略。首次安装在终端交互运行
`studio-team-init` 创建管理员，密码不会出现在参数或文件中。最后写入 `studio.env` 与
`camoufox-studio.service` 并启用服务。可选 `--allow-client`、`--automation`（开启 8767 端口的 API 密钥接口）
以及 HTTPS 的 `--cert`/`--key`（服务账号须可读）。

单元以 `camoufox` 运行，`Type=exec` 让账号或入口执行失败直接报错，但不代表端口已监听。
安装器只报告已请求启动；须检查 `systemctl status camoufox-studio`、日志并实际登录，才能确认服务可用。
`Restart=no`：异常退出后留下的运行记录须先用
`sudo -u camoufox /opt/camoufox-studio/.venv/bin/python -m camoufox studio-recover --data-dir /var/lib/camoufox-studio/workspace`
检查并恢复，自动重启只会反复失败。`KillMode=mixed` 只向主进程发送停止信号，等待浏览器关闭、
签回与工作区释放，最长 300 秒后才强制结束。单元不启用 `RestrictNamespaces`、`PrivateUsers`
等会阻止用户命名空间的加固项；其余加固在实机验收后再加入。日志用 `journalctl -u camoufox-studio` 查看。

修改 `studio.env` 后执行 `sudo systemctl restart camoufox-studio`。防火墙只对局域网网段放行
网页端口，开启 API 密钥接口时再放行 8767，例如：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8766 proto tcp
```

升级：`sudo systemctl stop camoufox-studio`，更新源码检出。远程服务器从 `studio.1` 升级时，
须先按[运行组件说明](studio-kasmvnc-runtime.md#source-provenance-and-cache-correction)在新目录构建 `studio.2`，
并将 `/etc/camoufox-studio/studio.env` 的 `CAMOUFOX_KASMVNC_TOOLS` 显式改为新目录；不要删除旧组件或工作区。
然后重跑同一安装命令。脚本拒绝在服务运行时执行，
重装依赖与本包、刷新 systemd 单元，保留工作区、`studio.env`、钥匙环、解锁凭据和已构建的 KasmVNC。
升级检查以保留的配置为准，不以本次命令行默认值替代：由 systemd 读取环境文件并展开实际启动参数，
复用团队入口解析器检查 TLS、浏览器模式与端口。远程模式缺少 `CAMOUFOX_KASMVNC_TOOLS` 或其路径不存在时
明确失败；管理员须恢复该组件或修正配置，不自动改用另一路径。

### 服务账号凭据

服务账号不需要图形登录。安装器在内存中生成随机解锁口令，经标准输入交给
`systemd-creds encrypt --with-key=host`，只保存密文；口令不进入命令行、环境文件或日志。
单元通过 `LoadCredentialEncrypted` 将解密后的凭据提供给启动进程，启动进程通过标准输入
解锁 GNOME Keyring。每次启动使用 `/run/camoufox-studio/` 下的独立文件系统 D-Bus 套接字，
不连接桌面用户的会话。运行目录权限为 `0700`，服务关闭后由 systemd 清理凭据会话进程和目录。

HTTP 服务启动前确认默认集合是已解锁的持久 `login` 钥匙环，并使用现有原生凭据存储执行
随机探针的写入、读回与删除，不读取已有代理秘密。缺少凭据、口令错误、钥匙环锁定、
组件缺失或检查超时均拒绝启动；不使用空密码、内存集合或明文后端兜底。
启动器随后原位执行现有团队入口，保留主进程 PID 和 `KillMode=mixed` 的正常关闭顺序。

主机密钥由 systemd 保存在 `/var/lib/systemd/credential.secret`，适用于没有 TPM 的虚拟机；
这不防御 root 或取得完整主机磁盘与主机密钥的攻击者。不要将主机密钥、解锁凭据、钥匙环
放入源码、安装包或普通工作区备份。跨主机迁移仍使用[迁移流程](studio-migration.md)，
重新配置目标主机原生秘密，不直接复制本机绑定的 `.cred` 文件。

已有钥匙环但没有解锁凭据时，安装器拒绝生成新口令，以免把已有秘密锁在旧钥匙环中。
优先从管理员的独立安全备份恢复原 `.cred` 与对应主机密钥。若此前自行配置过同账号的
`login` 钥匙环，且知道它的非空口令，在服务停止时可用以下命令接入；输入的是旧钥匙环
口令，不是 Studio 管理员密码：

```bash
sudo bash -o pipefail -c 'umask 077; test ! -e /etc/camoufox-studio/keyring-unlock.cred && systemd-ask-password -n "Existing service login keyring password:" | systemd-creds encrypt --with-key=host --name=keyring-unlock - /etc/camoufox-studio/keyring-unlock.cred'
```

已有 `.cred` 不会被安装器覆盖；忘记口令时不要删除钥匙环或生成新口令强行启动。
接入已有口令文件时必须保留全部字节：GNOME Keyring 会把标准输入中的末尾换行也视为
口令的一部分，不能去除空白或额外追加换行。启动器原样传递解密后的字节，不修改原钥匙环。
若原集合不是 `login` 或保存在自定义数据目录，先由管理员按原后端的流程迁移，不自动改写它。
WSL Ubuntu 24.04 已验证真实 systemd 凭据交付、无登录账号读写、服务重启后的持久读取、
错误口令拒绝及原口令恢复；另经真实团队入口、HTTP 登录和代理保存，验证重启后密码仍可读、
停止后工作区锁释放。这不代替全新 Ubuntu 服务器安装、整机重启和 `studio-recover` 验收。

## KasmVNC 远程传输

每个环境使用自身配置或身份记录中的精确内核 ID，不再读取团队设置中的全局可执行文件路径。
内核管理的默认值只影响新建环境；更换单个环境的内核会重建身份并保留网站数据。
旧身份没有完整内核 ID 时，保留其 `browser_version`：完整版本（如 `152.0.4-beta.31`）
只匹配该构建，裸版本（如 `152.0.4`）只接受唯一匹配的已安装构建；缺失或歧义均拒绝启动。
历史版本引用也会阻止删除对应内核。原地升级须保留原服务账号下可识别的浏览器安装；
不要通过更换环境内核来修复安装位置，因为该操作会重建身份。
旧身份签出时也按同一规则解析服务器已安装的精确构建，分发该构建的客户端平台包，不改写身份。

用 `--staging-root DIR` 开启签出：有“本机运行”授权的成员可把环境签到自己的电脑运行，
关闭浏览器后签回。目录须在工作区之外、与工作区同一文件系统，签回提交才能用原子改名完成。
签出期间服务器不运行、不编辑该环境，接口与恢复规则见 [团队 API 8.6](studio-team-api.md#86-签出租约本机运行)。

用 `--kernel-root DIR` 指定工作区之外的内核缓存目录后，管理员可在设置页缓存官方版本：
服务为四个客户端平台各下载一份官方包，只接受带 GitHub SHA-256 摘要的资产，并把 Linux 包
安装为服务器本机内核。不配置时不提供缓存与分发。仍被任何环境引用的内核不能删除。

用 `--client-root DIR` 提供桌面客户端安装包：把各平台构建的安装包及其 `.json` 记录放入工作区之外的
该目录，运行 `python -m camoufox studio-client-manifest DIR` 校验并生成清单。成员在“客户端下载”页
下载，桌面客户端登录后据此提示更新；构建与发布见[客户端安装包](studio.md#client-packages)，
接口见 [团队 API 8.7](studio-team-api.md#87-客户端安装包与更新)。

唯一 Vue 前端使用 KasmVNC，不提供显示后端选择。固定组件缺失、摘要不符或版本不符时，
remote 服务启动明确失败；不会回退到 PNG 或宿主桌面。
KasmVNC 使用增量 JPEG/WebP，保留同一环境的身份、网站数据、权限、控制租约和文件流程。
连接关闭不停止浏览器，重新连接保持只读。

Linux 上先按 [固定运行组件说明](studio-kasmvnc-runtime.md) 构建并安装组件，给既有
服务设置 `CAMOUFOX_KASMVNC_TOOLS`，随后通过正常停机维护重启服务。无需安装完整
Kasm Workspaces、GNOME、KDE 或另建一台虚拟机。服务启动不会临时下载组件。
服务启动验证固定版本、两项二进制摘要和修复标记。客户端解码器和许可/源码材料
随 Vue 构建打包，由 Python 服务同源提供。

组件只为自己的显示分配回环监听，内部认证材料不会进入 URL、Profile、页面或浏览器
挂载；对外仍使用团队入口。各环境的 X11 显示号由服务器原子分配，内部通信使用私有
文件系统套接字，关闭未使用的 UDP/WebRTC。Windows 不提供远程执行。
群晖已有 Ubuntu 虚拟机可复用；原生 DSM 容器仍须单独验证内核和隔离能力。

## Linux 原生远程操作

Ubuntu 24.04 x86_64 的依赖与本机验证命令如下；使用非 root 专用服务账号。
宿主必须允许 bubblewrap 创建用户命名空间；失败即拒绝启动，不回退到宿主桌面。

```bash
sudo apt-get install xauth xdotool bubblewrap xclip fontconfig
python -m camoufox fetch
python scripts/install-studio-kasmvnc.py --install-system-deps --prefix "$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
export CAMOUFOX_KASMVNC_TOOLS="$HOME/.local/share/camoufox-studio/kasmvnc-1.5.0-studio.2"
python -m camoufox studio-team --data-dir ./my-team-workspace --browser-mode remote
```

Linux 服务器不要求完整桌面或 Qt，使用每环境私有 KasmVNC 显示；仍需浏览器系统运行库和
配套字体资产。新服务器前提见[远程窗口与中文显示](studio-remote-view.md#ubuntu-server-不需要完整桌面)。
登录后创建 Linux 环境、启动、点击“打开操作窗口”，点击“申请控制”，需要时再点击“全屏”。
画面占满独立窗口，工具可收起，Esc 退出全屏；不再提供管理页内嵌的小预览。
默认 100% 原始大小，可在工具中切换“适应窗口”。没有控制权时只能观看；
管理员可确认后强制接管。释放、断线、窗口失焦、登录撤销或 30 秒心跳过期会释放
按住的键/按钮和控制权，不停止浏览器。重新打开连接默认为只读，不自动重放输入。
客户端缩放不改变服务器显示尺寸；尺寸来自保存的身份，当前容量为
640–4096 × 480–2160。画面经 KasmVNC 增量编码；实际帧率和容量依赖服务器及网络。

Linux 的鼠标、按键、滚轮与复制/粘贴快捷键复用每环境一个常驻 python-xlib/XTest
进程，不再逐事件启动 xdotool。输入管道同一时刻仅有一条未确认请求，X11 确认执行
后才返回；释放与接管等待按键和按钮的原生释放确认。进程退出、异常确认或请求
超时会封锁该输入进程并停止所属运行，不重放输入。xdotool 仍用于启动窗口几何校验。
实际交互收益仍需在 Linux 执行机测量。
Vue 使用一个输入合并模块：连续移动只保留未发送的最新位置，点击、按键、
滚轮及文本之前先发送待发移动。序号仅在实际发送时递增；释放、重置、控制代次变化、
撤权或断线会清除待发移动。页面按同源 JavaScript 模块加载，无需额外 Node 服务。

“发送文本”显式粘贴最多 1 MiB UTF-8 文本，不截断。文本只保存在该环境的私有
X11 选择中，直到下一次替换或浏览器停止；不读取本机剪贴板，不同步宿主剪贴板。
管理员接管本身不清空网站数据或重建身份。
新版控制台支持当前控制者主动复制 Linux 环境的选中内容或剪贴板至客户端，
最多 1 MiB，读取前后重新检查授权；不自动同步或读取执行机宿主剪贴板。
Shift/本机输入法组合事件不会直接转发为不支持的原生按键；后端返回输入错误时
保持画面连接与浏览器。中文发送和字体排查见[输入法说明](studio-remote-view.md)。

上传要求 `watch` 和 `operate`，单个非空文件最多 8 MiB，保存到当前 Profile 的
`Uploads`；文件名添加随机前缀避免覆盖。在原生文件选择器中选择该目录内文件。
上传目录最多 128 个文件、128 MiB；下载列表仅列出 `Downloads` 中稳定的普通文件，
读取上限 8 MiB。链接、硬链接、设备、FIFO、跨目录文件名和变化中的文件拒绝读取；
临时下载文件及无法表示为 UTF-8 的名称不列出。文件操作不要求持有输入控制权，
但每次重新检查当前授权与运行。
上传不会自动过期删除；容量满时由管理员停机维护，不以自动清理覆盖或删除成员文件。

每环境独立 KasmVNC 显示服务、随机 Xauthority、私有临时目录及 bubblewrap 文件命名空间。
浏览器只绑定内核/扩展只读目录、系统运行库和该 Profile 的可写目录，不共享宿主 home、
WSLg 桌面或其它环境；浏览器网络仍用于网站与既有代理，不是不可信租户的网络隔离。
按键和文本工具也有独立文件命名空间，隐藏宿主显示套接字并只绑定该显示，
防止 WSLg 与私有显示同为 `:0` 时误连；工具运行资产只读，home 为临时目录。
输入经私有进程管道/X11，画面经过配对的团队 WebSocket；不公开 VNC、CDP 或额外桌面端口。
软件 X11 渲染没有 GPU 直通；无头基线不能认证此模式的图形指纹。

控制台中文字体随包提供并仅从本站加载，不依赖客户端 WSL 字体。启动器会核验
生成的 Fontconfig 缓存、用 UTF-8/XML 转义写入并原子替换，空文件或损坏缓存不会继续复用。
这不修改保存的字体指纹集合；配置内核的网页字体加载仍需单独核验，不能用系统字体
安装或关闭字体保护掩盖差异。

完整指纹配置可经管理员详情及环境配置接口读写，旧网页尚未提供全字段编辑器。
代理、并发设置、内核和运行中 Cookie 已有 admin API；代理秘密不回读，内核清单不返回绝对路径，
Cookie 不接受服务器文件路径。API 仍不提供页面代码执行、终端或调试协议旁路。
管理功能不会把浏览器数据目录复制到客户端电脑。

## 传输、网络范围与运行边界

默认 Origin 为 `http://127.0.0.1:8766`，默认监听回环地址，不要求证书或白名单。
跨电脑时显式设置匹配的 Origin 和监听地址；`--private-network-only` 与可重复的
`--allow-client IP_OR_CIDR` 都是可选限制。风险与组合规则见
[HTTP 网络范围](studio-native-acceptance.md#默认-http-与可选网络范围)。
需要传输保密性时使用 HTTPS；路径和域名必须换成真实配置，证书和私钥不得放进仓库：

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace --origin https://team.example.com:8766 --listen 0.0.0.0 --cert /secure/tls/fullchain.pem --key /secure/tls/private-key.pem --browser-mode remote
```

默认监听 `127.0.0.1`，任何外部监听都需要显式 `--listen`。HTTP 下服务会持续输出
明文风险警告；网络范围开关与成员鉴权都不能提供传输加密。
当前入口直接处理 TLS，不信任 `Forwarded` / `X-Forwarded-*`，不能照搬成 TLS 终止
反向代理配置。无多 worker、reload 或共享网络文件系统模式；`WEB_CONCURRENCY`
不能改变单 worker 所有权。

HTTPS 登录 Cookie 为随机 256 位能力，仅不可逆摘要存数据库，设置 HttpOnly、Secure、
SameSite=Strict、Path=/，不设置 Domain。本机登录会话 ID 不能作为 HTTP 登录 Cookie。
登录和所有变更检查精确 Origin；已登录变更还检查会话绑定 CSRF。新登录轮换当前
浏览器的 Cookie 并撤销旧登录。没有 URL 令牌、长期 localStorage Token 或宽松 CORS。

API 每次重新验证登录和角色上限；成员停用、密码或授权变化后，旧 HTTP 会话不可用。
已有 WebSocket 每次发送重新验证，管理变更还主动断开被撤销的连接、释放控制权。
状态最多 64 个连接，每会话最多 4 个订阅；事件仅发送获授权的元数据。
原生画面另有全局 16、每登录会话 2 个连接上限，包含等待 CSRF 的握手；首消息须
在 5 秒内提交当前 CSRF。输入每秒最多 80 条，文本最多 1 MiB UTF-8；单条消息最多
6 MiB + 1024 字节以容纳 JSON 转义，队列最多 2 条；
控制代次和严格递增序号阻止旧控制者、重复和乱序输入。
一般请求体最多 64 KiB，上传最多 8 MiB，读取限时 10 秒；数据库与密码工作限制为 4 个后台任务；请求和
登录分别按直接对端及全局限速。网络错误仅返回固定代码，不返回第三方异常、密码
散列、身份、代理配置、网站内容或宿主目录。访问日志默认关闭。

网页状态连接中断后，以 3 秒到 30 秒的退避重新查询当前登录，再建立状态连接。
重连刷新当前会话的 CSRF；退出、撤权或旧连接迟到回调不能恢复旧登录。
自动重试仅用于只读登录核验，不重放启动、停止、密码或授权等变更请求。

这些边界不是不可信租户隔离。宿主账号权限、后台凭据服务、磁盘权限及浏览器沙箱
仍必须单独配置。[离线加密快照及隔离恢复](studio-backup.md)已实现，不开放网页密钥或任意路径接口。
完整部署门禁尚未完成，不建议直接公开部署。

## API 与执行所有权

接口前缀为 `/team/v1`。除登录外均需要 Cookie；变更需要精确 `Origin` 和
`X-CSRF-Token`。`GET /me` 返回当前 CSRF 和明确的能力状态；不接收查询字符串令牌。

| 接口 | 当前行为 |
| --- | --- |
| `POST /login` | `{name,password}`；返回成员、期限、CSRF，设置 Cookie |
| `GET /me` | 当前成员与能力；返回 `launch_modes`，仅 remote 模式启用画面/控制/文件；旧自动化与在线维护 capability 为 false |
| `POST /logout` | 空对象；撤销当前登录和状态连接 |
| `POST /password` | `{current,password}`；修改自身密码并撤销该成员登录 |
| `GET /profiles` | 活动及归档环境的授权元数据、语言、脱敏代理和上次启动，不返回完整配置或身份 |
| `GET /profiles/{id}` | 管理员完整编辑字段、时间、修订号和身份存在标记；不返回内部身份或路径 |
| `GET /profiles/{id}/remote` | view；脱敏环境、当前运行、可用原因和同源窗口/画面路径；只读、不申请控制 |
| `POST /profiles` | 管理员创建环境，接受现有可编辑 Profile 字段 |
| `POST /profiles/{id}` | 管理员 `{revision,data}`；修订冲突或活动运行时拒绝 |
| `POST /profiles/{id}/start` | `{request_id, mode?}`，需要 operate，mode 受 `/me.launch_modes` 约束，返回 202 |
| `POST /profiles/{id}/stop` | `{request_id,run_id}`，需要 operate；旧 run ID 不可停止新运行 |
| `GET /commands/{id}` | 本人命令或管理员命令，且仍具有该环境查看权限；显示最终固定结果码 |
| `GET /tasks/{id}` | 删除、代理、配置与内核后台任务的有界状态/结果 |
| `/profiles/{id}/identity|clone|delete|cookies` | 管理员身份、复制、删除和有效运行 Cookie 资源 |
| `/proxies` | 管理员脱敏代理管理、检查和批量导入 |
| `/activity`, `/settings`, `/configuration`, `/kernels` | 工作区活动与管理员执行机资源；运行中可保存不低于活动运行数的并发上限，只有实际变更引擎路径/网卡时才拒绝；原值重复保存不算切换，维护独占期间仍拒绝保存 |
| `GET /admin` | 管理员专用成员、直接授权、组关系和最近 100 条审计，不返回密码材料 |
| `GET /health` | 管理员专用运行/命令/任务数量、服务级 maintenance、并发上限、实际可用磁盘字节数、运行基线注册及执行屏障；不返回宿主路径 |
| `POST /members` | 管理员 create/active/password/grant/revoke，动作按固定字段验证 |
| `POST /groups` | 管理员 create/members/grants/delete；修改携带当前组 revision |
| `WS /events` | 同源授权状态快照；只能发送 `ping`，不是输入或画面通道 |
| `WS /remote/{run_id}/control` | Cookie、Origin、watch 与首消息 CSRF；控制状态和有代次的原生输入 |
| `WS /remote/{run_id}/display` | 同一登录与控制连接配对后，只传输 KasmVNC 画面 |
| `GET /files/{run_id}` | watch + operate；列出当前运行的 Downloads |
| `GET /files/{run_id}/{name}` | 同上；有界下载稳定普通文件 |
| `POST /files/{run_id}/{name}` | 同上及 CSRF；octet-stream 上传，随机新文件名，不覆盖 |

预览版的 operate 允许启动或停止获授权的运行，不绑定原生输入控制租约。
只有输入需要同时具备 watch、operate 和当前控制权。与完整设计“持有控制权才可停止”
的 M05 契约仍有差异；当前权限模型明确允许已授权操作员停止其他控制者的运行。
`request_id` 为规范 UUID，同一成员重复相同命令返回原命令；改变环境、操作或 run ID
返回 `request_conflict`。重用旧的启动 request ID 不会在浏览器已停止后重新启动。
客户端遇到未知 HTTP 结果应查询状态，不自动更换 request ID 重试副作用。

团队 schema v4 新增 HTTP 能力、运行和命令表，兼容旧版成员、组、身份和直接授权。
启动先事务性预留 `starting` 运行；SQLite 部分唯一索引确保每环境只有一个活动运行，
并发容量包含启动、停止和恢复状态。命令在离开请求生命周期后由服务拥有，执行前
再次鉴权。停止先标记停止意图，迟到的启动不能复活已停止运行。普通成员配置读取
使用同一个 `MemberStore` 和角色限制，没有第二套授权规则。
启动准备前后及确认成功前复核授权；准备中的成员退出或撤权会取消待启动浏览器。
若撤销发生在浏览器建立中，清理上下文而不访问启动页；已成功运行的浏览器不会因
登录撤销而重启或停止；原生画面连接与输入控制同步撤销。

执行成功、失败、停止与清理不确定分别持久化；清理不确定保留 `recovery_required`
和工作区锁，不把租约、缺失 PID 或过期锁视为浏览器已停止。浏览器崩溃与关闭请求
竞争时，启动器将 Playwright 的 `TargetClosedError` 作为已关闭状态处理，但仍等待
本地驱动退出；其他浏览器关闭、驱动和原生资源清理异常继续保留恢复屏障。
服务关闭等待引擎、命令和运行监视任务全部结束，仅在资源与数据库状态明确安全后
释放工作区。
桌面和本机执行器也检查持久化执行所有权；手动删除锁不能绕过未完成的运行记录。

新团队入口使用内核进程边界及版本 2 锁证明，异常退出后由 `studio-recover` 明确
检查并恢复，步骤见[恢复协议](studio-native-acceptance.md#显式崩溃恢复)。仍存活、
旧版锁或证据不完整时拒绝恢复。**不要用删表、改状态或自动删锁脚本恢复**，不能
直接把数据库副本同时运行。

## 验证与未完成项

测试覆盖网络认证、来源和 CSRF、Cookie 轮换、成员/组撤销、字段脱敏、事件连接、
限流配置、重复及并发命令、迟到启动、忙环境编辑、工作区互斥和失败清理。
测试使用临时工作区，不迁移真实数据。真实浏览器、网页交互与制品测试的
实际结果记录在[实施进度](studio-client-server.md#12-实施任务与验收)，不会把模拟浏览器测试当成实机通过。
Ubuntu 24.04 x86_64 的真实浏览器 TCP 管理和离线恢复回归现已通过；运行方法见
[服务测试说明](../service-tester/README.md#studio-runtime-regression)。此测试不涉及原生画面。

Linux 实机原生显示、输入、接管、帧连接撤销和受限文件网关的回归见
[原生测试说明](../service-tester/README.md#studio-native-regression)。仍未完成：
完整原生显示/GPU/字体清单与反检测认证、
在线维护和失联主机灾难恢复、磁盘主动告警及正式跨机器 HTTPS 部署演练。
离线备份、隔离恢复和可选无头基线见[维护说明](studio-backup.md)。

实现依据：[FastAPI 生命周期](https://fastapi.tiangolo.com/advanced/events/)、
[Cookie 设置](https://fastapi.tiangolo.com/advanced/response-cookies/)、
[Starlette 请求](https://starlette.dev/requests/)、
[WebSocket](https://starlette.dev/websockets/) 和 [Uvicorn 设置](https://uvicorn.dev/settings/)。
FastAPI 0.142.2（MIT）、Uvicorn 0.54.0（BSD-3-Clause）和 websockets 15.0.1（BSD-3-Clause）
是团队专用运行依赖，用于 ASGI 生命周期、HTTP/TLS 与有界 WebSocket；它们不编解码
画面，也不代替浏览器引擎。独立锁文件覆盖 Python 3.10+ 和跨平台依赖条件。
新增 MSS 10.2.0 用于有界原生采集，python-xlib 0.33 核验私有选择所有者；xclip
以前台受监督进程提供 GTK 文本转换，避免重复实现选择协议。依赖只供团队模式使用。
控制台 Noto Sans SC 5.2.9（OFL-1.1）字体与许可证由固定摘要锁定，
维护脚本为 `python scripts/fetch-studio-font.py`，不会在网页登录时访问字体 CDN。
依赖锁不代替持续漏洞审查或 Linux/Python 3.10 实机验证。

CI 安装 `geoip,studio,team` 全部测试前提，使用 `team-ci-requirements.lock`，不因缺少
团队依赖而跳过接口测试。该测试锁通过团队运行锁约束公共依赖，桌面发布锁保持独立。
维护者有意更新依赖后，在仓库根目录重新生成：

```bash
uv pip compile pythonlib/pyproject.toml --extra team --python-version 3.10 --universal --output-file pythonlib/team-requirements.lock
uv pip compile pythonlib/pyproject.toml --extra studio --extra team --extra geoip --python-version 3.10 --universal -c pythonlib/team-requirements.lock --output-file pythonlib/team-ci-requirements.lock --no-annotate --no-header
```
