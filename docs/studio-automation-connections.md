# 自动化接口：同机与跨电脑调用

团队服务器提供独立的成员 API 密钥接口。同机和跨电脑使用相同的连接文件及
`/automation/v1` 协议，浏览器运行在服务器上。环境签出到成员电脑本机运行时，改用
桌面客户端的[本机接口](studio-automation.md#desktop-client)。

## 服务器配置

在已初始化的团队工作区启动服务：

```bash
python -m camoufox studio-team --data-dir ./my-team-workspace --listen 0.0.0.0 --origin http://192.168.10.6:8766 --browser-mode remote --automation-listen
```

网页端口为 8766，自动化端口默认为 8767，可用 `--automation-port` 指定其他端口。
二者使用相同的监听地址、TLS 证书、`--private-network-only` 与 `--allow-client` 限制。
只接受实际连接来源地址，不信任代理转发头。部署详情见[团队服务](studio-team-service.md)。

没有原生显示运行条件时使用 `--browser-mode headless`，调用方启动时指定
`headless:true`。接口默认关闭，必须显式开启。服务器重启、部署升级和显式崩溃恢复
均保留原有效密钥；已过期或已吊销的密钥不会恢复。已有借用、任务标签页和句柄不恢复，
调用方使用原密钥重新申请。显式崩溃恢复会撤销普通网页登录，需要时重新登录，
不影响该成员已有的有效 API 密钥。

## 创建与保存连接文件

1. 管理员或操作员登录 Vue 控制台，打开“API 密钥”。
2. 填写名称，可选到期时间、允许的环境和来源 IP/CIDR。
3. 创建后下载 `automation.json`。密钥只显示一次，之后只能吊销并重新创建。

管理员可以管理所有密钥，操作员只能管理自己的密钥；只读成员不能创建密钥。
密钥始终受所属成员的当前授权约束，限定环境与成员授权取交集。
限定环境的管理员密钥不能创建新环境。收回授权会释放受影响的借用；
吊销、密码重置和停用成员会使其密钥失效，重新启用成员不会恢复旧密钥。

连接文件包含 `protocolVersion`、`baseURL` 和 `token` 三个字段。
现有调用方只需让 `CAMOUFOX_STUDIO_CONNECTION_FILE` 指向下载的文件，
请求端点、参数、借用和心跳逻辑不变。连接文件包含控制凭据，
应保存在受信任用户目录，不加入 Git 或分享。

HTTP 下密钥与浏览器操作均明文传输。来源 IP 限制不能替代加密，需要保密时配置 HTTPS。
自动化监听使用与网页相同的证书，调用方必须正常验证证书，不关闭验证。

## 调用示例

以下 Node.js 22+ 示例只列出环境，不启动浏览器，在调用程序的电脑上运行：

```powershell
$env:CAMOUFOX_STUDIO_CONNECTION_FILE = Read-Host 'automation.json 绝对路径'
@'
import {readFile} from 'node:fs/promises';
const c = JSON.parse(await readFile(process.env.CAMOUFOX_STUDIO_CONNECTION_FILE, 'utf8'));
const r = await fetch(c.baseURL + '/automation/v1/profiles', {
  headers: {Authorization: 'Bearer ' + c.token},
});
if (!r.ok) throw new Error('Studio HTTP ' + r.status);
console.log(await r.json());
'@ | node --input-type=module
```

通用顺序：列出环境 → 必要时启动指定 ID → 申请独占借用 → 创建任务标签页 →
执行任务并每 20–30 秒续期 → 释放借用。每把密钥只能操作自己的借用和任务标签页；
同一环境仍只能有一个自动化借用。释放或吊销只关闭任务标签页，浏览器和人工标签页继续运行。
关闭最后一个原生标签页会结束运行；清理完成后可重新启动同一环境并申请新的借用，
原网站数据和固定身份保留。桌面客户端签出是另一种租约，不会因 90 秒心跳中断自动归还。

| 现象 | 检查 |
| --- | --- |
| 页面提示未开启监听 | 启动服务时添加 `--automation-listen`，选择可执行浏览器的模式 |
| 连接被拒绝 | 检查自动化端口、防火墙、监听地址及连接文件 |
| HTTP 401 | 核对错误代码：`unauthorized` 为缺少 Bearer 头，`session_expired` 为密钥过期、吊销、成员密码重置或停用；部署本身不要求重建密钥 |
| HTTP 403 | 当前授权、限定环境、来源 IP、Host；请求不能携带 Origin |
| HTTP 404 | 借用已释放，或属于另一把密钥 |
| HTTP 409 | 环境已有借用、正在停止、容量不足或运行模式不匹配 |

完整端点、错误结构、页面方法和上传下载见[协议说明](studio-automation.md)。
