# Studio 服务器停机迁移

适用于旧服务器仍可操作、迁移后停止使用旧工作区的场景。新服务器保留 Profile ID、
固定指纹配置、身份历史、Cookie、localStorage、IndexedDB、成员账号和授权。
成员使用原账号密码重新登录；旧登录 Cookie、会话和运行能力不会迁移。

迁移顺序是：**关闭浏览器 → 加密快照 → 新端隔离恢复与准备 → 旧端退役 → 新端激活**。
旧端先持久化禁止执行状态，再输出只对一个恢复副本有效的加密凭证。
不需要两台服务器挂载同一个目录，也不复制运行中的 Firefox 数据库。

## 商业产品的做法与本项目的选择

账号登录需要配合环境数据保存与恢复才能跨设备使用。
[GoLogin](https://support.gologin.com/en/articles/14404855-profile-synchronization)
在正常关闭环境后上传指纹和网站数据，要求同步完成并避免同时使用同一环境。
[AdsPower](https://help.adspower.com/docs/global_settings)也在关闭环境时执行所选数据的云同步。
[Multilogin](https://multilogin.com/help/en_US/cloud-and-local-storage)
区分跨设备云存储与本地存储，本地数据需另行导出导入。

本项目提供现有团队账号和完整工作区的受控停机迁移。当前没有集中账号云、
自动云同步、在线迁移按钮或失联主机强制接管；密钥和维护路径只由本机管理员处理。

## 接受哪些差异

| 项目 | 策略 |
|---|---|
| Profile ID、固定身份、网站数据、成员和授权 | 原样保留，准备和激活前核对快照摘要 |
| OS 类型和 CPU 架构 | 默认必须相同；显式 `--accept-platform-change` 允许同架构 OS 互迁，仍拒绝 x86/ARM 互迁 |
| 浏览器目录、包内字体、固定模型、扩展内容、核心 Python 包版本 | 默认与来源一致；跨 OS 只接受平台对应内核，版本须匹配所有保存身份，模型、扩展和核心包仍必须一致 |
| 主机标识、OS 版本、标准原生字体目录 | 新端显式接受并记录当前值，后续启动继续校验 |
| 浏览器与扩展安装路径 | 新端重绑定；不改写保存的指纹身份 |
| 代理密码 | 不进快照，在新端原生钥匙环重新录入；准备与激活都检查 |
| 原生远程测量证书 | 留存历史，旧证书失效；新端需逐环境重新测量接受 |

换服务器可能改变 GPU/驱动、原生渲染和字体度量；同一份固定配置不保证所有网站
测到的值完全相同。若同时改变出口 IP，网站也可能要求重新登录或验证。
保留可用的原代理及地区、内核和字体，有助于减少变化，但不能保证零损失。
Windows 与 Linux 的平台内核、系统字体、图形驱动和原生控件并不相同。
跨 OS 时必须在准备命令额外传入 `--accept-platform-change`，该接受状态绑定进加密请求；
须用当前代码重新生成 `--for-migration` 快照。新版迁移制品记录用相对路径文本统一排序，
逐一校验默认及自定义扩展内容；旧迁移快照仍支持同平台交接，既有宿主运行摘要不改版。
它不放宽同 OS 的制品摘要检查，也不重新生成保存的身份。激活后仍需重新完成原生测量。
已明确接受并完成跨 OS 交接的固定环境，可保留来源 OS 与“使用本机”的字体、
WebGL、语音或媒体设备设置。原生模式会读取新主机的真实资源，因此这些表面会改变；
原代理绑定和保存身份不改写，远程使用前仍需逐环境重新测量。该许可核对完整请求、
退役凭证、新工作区、来源 OS 与迁移环境 ID；未激活、副本、新建环境及身份已重置的
环境不能借此绕过普通的 OS 一致性校验。
远程浏览器的只读扩展挂载使用本次启动已解析的路径，保存身份内的来源路径不被改写。
浏览器网站数据的原样复制不保证每个网站继续接受原登录，尤其是设备或出口变化时。
准备命令输出 `changed_runtime_fields`，列出接受的宿主基线差异；这不是完整指纹报告。

`desktop` 运行基线允许有窗口和无头启动；原生远程显示仍独立校验。
`headless` 基线只允许无头启动。两种模式都绑定新端实际制品及主机，
以后运行条件变化仍会明确失败，不会自动重置身份。

## 操作步骤

两端安装相同版本的源码包及[备份依赖](studio-backup.md#安装与密钥)，
并准备内容相同的 Camoufox 内核、模型和扩展。先正常关闭 Studio、团队服务、
所有浏览器及代理；有锁或未完成运行时应先完成[明确恢复](studio-native-acceptance.md)，
不能删除锁绕过。以下例子中的工作区、内核和受保护目录须事先存在。

`../protected-keys` 与 `../backups`、`../transfers`、`../restore-area` 是相互独立的目录。
密钥文件不能位于工作区、归档、请求或凭证所在目录下；Linux 密钥文件权限为 0600，
Windows 使用限制维护账号访问的 NTFS ACL。使用已有备份密钥，或按备份文档生成。
通过受保护的传输方式分别送达归档、请求/凭证及密钥，不能将密钥塞进归档或仓库。

### 1. 旧端创建迁移快照

```bash
python -m camoufox studio-backup --data-dir ./my-team-workspace --output ../backups/team.cfxbackup --key-file ../protected-keys/studio.key --for-migration
```

`--for-migration` 显式记录当时的实际制品和扩展路径，支持没有历史运行基线的旧环境，
但不追认其历史渲染已通过测量。已有运行基线必须仍匹配。
普通备份及早期快照不含这份来源记录，不能用于跨服务器激活，需重新创建迁移快照。
创建快照后保持旧端停机；若继续使用或修改配置/数据，需重新制作快照。

### 2. 新端隔离恢复并准备迁移请求

```bash
python -m camoufox studio-restore --archive ../backups/team.cfxbackup --destination ../restore-area/team-restored --key-file ../protected-keys/studio.key
python -m camoufox studio-migration-prepare --restored ../restore-area/team-restored/workspace --executable-path /opt/camoufox/camoufox --output ../transfers/team.request --key-file ../protected-keys/studio.key --confirm-current-runtime
```

`/opt/camoufox/camoufox` 替换为新端实际内核路径；Windows 使用其 `camoufox.exe` 路径。
省略 `--executable-path` 则解析新端已选择的本地内核，不沿用旧服务器的绝对路径。
默认 `--mode desktop`，只执行无头任务时可加 `--mode headless`。
默认扩展按已记录的来源角色映射到本机目录并核对内容摘要；自定义扩展使用可重复的
`--addon-path OLD NEW` 指定完整路径映射。缺少目录或内容变化都会拒绝。
迁移前请确认环境引用的 HTTP、HTTPS 或 SOCKS5 代理可在新端使用。

若快照包含受支持代理的密码标志，先在恢复工作区重新录入密码，保持其他配置不变。
旧版节点或链式代理会阻止迁移。先在源工作区替换环境的代理绑定、删除旧代理记录，再创建迁移快照。
准备不会修改环境身份、网站文件或运行设置，也不会解除隔离。
请求保存后冻结这一目标；同一输入可以重新输出请求，修改目标条件需从快照另行恢复。

### 3. 旧端接收请求并永久退役

将 `team.request` 送回旧端的 `../transfers` 目录，再执行：

```bash
python -m camoufox studio-migration-retire --source ./my-team-workspace --request ../transfers/team.request --output ../transfers/team.receipt --key-file ../protected-keys/studio.key --retire-source
```

命令持有来源工作区锁，核对原工作区 ID、快照数据和当时的实际制品。
成功后来源目录保留供检查，桌面、本地宿主和团队引擎都拒绝其启动，旧登录全部撤销。
凭证绑定快照、新工作区 ID 和完整请求摘要，不能给另一恢复副本使用。
备份密钥同时具有生成迁移记录的权限，应只交给负责两端维护的管理员。

### 4. 新端接收凭证并激活

将 `team.receipt` 送到新端，再执行：

```bash
python -m camoufox studio-migration-activate --restored ../restore-area/team-restored/workspace --receipt ../transfers/team.receipt --key-file ../protected-keys/studio.key
```

新端再次核对请求对应的快照、宿主制品和秘密，在一个事务内更新本机路径、运行清单、
迁移历史并解除隔离。使用恢复后的目录作为 `--data-dir` 启动 Studio 或团队服务，
成员可用原账号密码登录。管理服务的 Origin、TLS 和网络范围按新地址另行配置。

激活成功即完成迁移，无需管理员逐环境重新验收。历史原生测量记录不参与启动授权；
重启、资源调整或宿主条件变化不会单独触发复验。跨系统的固定身份仅在匹配已激活的请求、
来源退役收据、目标工作区和迁移环境集时获得启动例外；新建环境不继承该例外。
代理不可用时保留环境的原代理绑定，待管理员修复后再启动，不会自动改用其他代理或直连。

## 中断与重复执行

准备请求发布失败时，恢复端保持隔离，可用相同输入重新准备。
旧端已退役但凭证发布失败时，两端均禁止浏览器执行；回到旧端，对同一请求重新执行
退役命令即可重新输出凭证。不能为另一副本发出凭证，也不自动重新启用旧端。
新端激活事务失败时隔离仍在，解决原因后可重复同一激活命令。
已完成的激活可以重复确认，不覆盖后续数据；下一次迁移需要新快照和新的目标请求。

单次退役操作只能封禁它所管理的来源工作区。维护账号不得保留另一份可执行的手工副本、
回滚来源磁盘快照、复制主机标识或修改数据库绕过屏障；本机维护账号仍是可信管理员。
这是受控离线交接，不能提供中央服务对任意旧副本的远程撤销保证。

## 验证范围

单元回归覆盖主机与路径变更、账号重登录、数据与身份保持、来源/目标变化、
不兼容制品、秘密缺失、篡改凭证、第二副本拒绝及两端提交/发布故障恢复。
真实浏览器回归复用团队登录与站点存储测试，用两个独立主机注册目录验证完整交接，
保留 Cookie、localStorage 和 IndexedDB。它模拟不同宿主注册，不等同于两台物理
服务器、跨 OS 迁移或所有 GPU/字体指纹的实际部署验收。
