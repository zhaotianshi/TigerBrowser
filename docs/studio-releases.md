# Studio 源码更新与安装包发布规则

## 1. 源码优先

所有修改通过关联 issue 的 PR 合并到 `main`。PR 保留完整检查：静态检查、
Python/Studio、Vue、真实 Linux 远程运行和四个平台的客户端构建。
必需检查仍为 **All tests passed**；任何失败、取消或缺少前提都不能视为通过。

源码检查不上传或下载 Actions artifacts。需要 Vue 资源的作业从同一提交和锁文件
各自构建，客户端构建共用同一个打包 action。PR 验证生成的安装包随临时执行机释放，
不会作为下载版本交付，也不创建 Release。

因此 Actions 产物存储不足时，可以先完成验证并更新源码。发布容量、预算或权限
不足时暂缓安装包发布，不扩大付费额度，不把上传失败改成测试成功。
“源码已更新”“安装包已发布”“服务器已部署”必须分别报告。

## 2. 正式安装包只从 GitHub Releases 交付

在需要交付新版本时，维护者按以下顺序操作：

1. 合并版本修改到 `main`，确认 `pythonlib/pyproject.toml` 中的包版本正确。
2. 在 Actions 手动运行 **Tests**，分支选择 `main`，`stage` 选择 `full`，等待
   **All tests passed** 成功。PR 的检查不能代替合并后这个精确提交的检查。
3. 手动运行 **Release Studio clients**，分支仍选择 `main`，`tag` 填
   `studio-v<包版本>`，例如包版本 `0.5.7` 对应 `studio-v0.5.7`。
4. 发布流程检查分支、版本和该提交的完整 CI 记录，再创建绑定源码 SHA 的 Release 草稿。
   已有标签指向其他提交，或已有同名 Release 时拒绝创建，不覆盖既有版本。
5. 在 Windows x64、macOS arm64、macOS x86_64 和 Ubuntu x64 分别使用与 PR
   相同的打包流程，完成冻结运行时检查、平台安装包和 SHA-256/协议记录。
6. 各平台直接上传到该源码的草稿，不经过 Actions artifact 存储。最终作业下载并
   核验四个平台的全部文件、版本、大小和摘要，生成 `manifest.json`，全部通过后才公开。

公开 Release 包含四个安装包、四个同名 `.json` 记录及一个完整 `manifest.json`。
单个资产须小于 2 GiB；该限制与 Actions 的存储额度是不同的规则。
发布说明包含源码提交；不使用浮动分支名标记安装包来源。

## 3. 失败、容量与重跑

- 发布失败保留草稿，未完成版本不作为正式下载版本。源码和已发布版本继续保留。
- 可在同一次发布运行中重跑失败作业。上传前会再次验证草稿、标签和源码 SHA，
  只允许替换该草稿中的对应安装包和记录；已公开或其他源码的版本不能被重跑覆盖。
- 完整重跑会重新执行创建步骤；同名草稿需维护者先检查处理，不能自动删除后重建。
- 存储或预算不足时先更新源码；清理或调整交付方式后再发布。不得自动启用付费、
  调高预算、删既有 Release 或重新标记旧安装包来冒充新版本。
- Actions 旧产物删除不会清除本周期已累计的存储用量，统计刷新也不等于额度重置。
  本规则中的源码检查和 Release 上传均不依赖 Actions artifact 存储。

## 4. 交给团队服务器

管理员从完整 Release 下载安装包及记录，核验后复制到工作区之外的客户端目录，
运行 `python -m camoufox studio-client-manifest <客户端目录>`，再由
`studio-team --client-root <客户端目录>` 提供下载和更新。GitHub 发布不会自动
替换服务器运行代码、用户数据或原生凭据。详见[客户端安装包](studio.md#client-packages)。

发布构建仍不等于各目标设备的安装、升级和跨电脑验收；安装包签名与公证的现状
继续以客户端文档为准。只报告实际完成的步骤。

依据：[Release 大小限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)、
[Actions 存储计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、
[GitHub CLI 发布命令](https://cli.github.com/manual/gh_release_create)。
