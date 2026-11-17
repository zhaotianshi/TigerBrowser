export type Role = 'admin' | 'operator' | 'viewer'

export interface ApiErrorResponse {
  error: string
  detail?: string
}

export const ERROR_MESSAGES: Record<string, string> = {
  timezone_catalog_unavailable: '服务器时区数据库或规范名称索引不可用，请检查 tzdata 安装。',
  session_expired: '登录已失效，请重新登录。',
  invalid_credentials: '用户名或密码不正确。',
  login_locked: '该账号暂时锁定，请 15 分钟后再试。',
  rate_limited: '请求过于频繁，请稍后再试。',
  csrf_denied: '登录状态已变化，请刷新后重新操作。',
  access_denied: '没有此环境或操作的访问权限。',
  admin_required: '此操作需要管理员权限。',
  role_permission_denied: '超出当前角色的权限上限。',
  private_network_required: '服务仅限本地或受信任的私有网络（RFC1918/ULA）访问。',
  client_not_allowed: '当前客户端 IP 不在服务允许的白名单范围内。',
  profile_busy: '环境正在运行或正在清理中，请停止后再操作。',
  profile_archived: '环境处于归档状态，请恢复后再操作。',
  revision_conflict: '数据已被其他管理员修改，请刷新后重新操作。',
  request_conflict: '请求标识已用于不同的操作，请刷新。',
  run_conflict: '运行状态已改变，请刷新状态后再操作。',
  capacity_reached: '已达到执行机的最大并发运行上限。',
  service_busy: '服务正忙或正在进行维护，请稍后再试。',
  execution_disabled: '执行机尚未启用浏览器任务。',
  automation_disabled: '服务器尚未开启自动化监听。',
  recovery_required: '执行机需要人工检查残留资源，无法直接启动。',
  invalid_request: '输入格式无效，请检查必填字段及密码要求。',
  invalid_proxy_link: '代理格式或参数无效。请检查协议、端口、认证、传输及链式入口；不支持关闭证书验证。',
  invalid_host: '非法 Host 头访问。',
  invalid_file_name: '文件名不兼容操作系统要求，请修改后再上传。',
  not_found: '请求的资源未找到。',
  file_not_found: '指定文件不存在。',
  request_timeout: '请求超时，请检查服务连接。',
  request_too_large: '请求体超出大小限制。',
  file_capacity: '文件大小超过限制（单文件最多 8 MiB）或目录已满。',
  unsafe_file: '拒绝读取链接或非普通文件。',
  file_changed: '文件仍在写入中，请在下载完成后刷新。',
  health_unavailable: '服务健康探针异常。',
  file_io_failed: '文件写入或读取失败。',
  operation_failed: '操作失败，请检查执行机日志。',
  launch_failed: '浏览器启动失败，请由管理员检查内核和代理。',
  browser_failed: '浏览器运行失败，请检查内核、原代理节点及执行机状态。',
  proxy_failed: '无法连接当前代理，请检查地址和凭据。',
  profile_checked_out: '环境已签出到成员电脑本机运行，请等待签回或由管理员强制解锁。',
  checkout_unavailable: '服务器未配置签回暂存目录（--staging-root），不能在本机运行。',
  checkout_denied: '本机运行凭据无效，请重新启动该环境。',
  checkout_revoked: '管理员已强制解锁此环境，本机副本已停用，请联系管理员。',
  checkout_closed: '此次本机运行已结束。',
  checkout_conflict: '服务器上的网站数据已变化，本机副本不能覆盖。',
  checkout_incomplete: '签回文件不完整，请重试。',
  checkout_committing: '环境正在签回，请稍后再试。',
  unsafe_profile_data: '环境数据包含链接或不可移植的文件名，不能签出。',
  kernel_cache_unavailable: '服务器未配置内核缓存目录（--kernel-root）。',
  kernel_not_published: '官方发布中缺少该版本的某个平台安装包。',
  kernel_ambiguous: '该版本存在多个同名安装包，无法唯一确定。',
  kernel_digest_missing: '官方安装包未提供 SHA-256 摘要，已拒绝缓存。',
  kernel_digest_mismatch: '安装包摘要与官方记录不一致，已丢弃。',
  kernel_size_mismatch: '安装包大小与官方记录不一致，已丢弃。',
  kernel_download_failed: '下载官方内核失败，请检查服务器网络后重试。',
  kernel_in_use: '仍有环境或新环境默认设置使用该内核，不能删除。',
  kernel_not_cached: '服务器尚未缓存该内核的此平台安装包。',
  kernel_repository_unsupported: '只支持官方发布的内核。',
  invalid_kernel: '内核标识无效。',
  client_not_attached: '桌面客户端尚未连接本机缓存，请重新登录后再试。',
  client_workspace_locked: '另一个客户端进程正在使用本服务器的本机缓存，请先关闭它。',
  workspace_changed: '服务器工作区已变化，请重启客户端。',
  workspace_origin_mismatch: '本机缓存属于另一个服务器地址，请使用原地址连接。',
  origin_denied: '只有所配置服务器的页面可以使用本机运行。',
  server_unreachable: '无法连接服务器，本机数据会在恢复连接后继续同步。',
  not_checked_out: '此环境没有签出到本机。',
  pending_return: '此环境还有未签回的本机数据，请等待签回完成。',
  return_in_progress: '此环境正在签回，完成前不能再次在本机打开。',
  return_required: '此环境已在本机运行过，数据必须签回服务器，不能直接放弃。',
  download_incomplete: '环境数据下载不完整，请重试。',
  kernel_platform_mismatch: '服务器提供的内核不适用于本机平台。',
  local_launch_failed: '本机启动失败，请查看客户端提示。',
  client_packages_unavailable: '服务器的客户端安装包清单损坏或与文件不符，请联系管理员。',
  client_update_invalid: '服务器返回的客户端更新信息无效。',
  client_update_unavailable: '服务器没有提供本平台的客户端安装包。',
  client_update_digest_mismatch: '下载的安装包大小或摘要不符，已丢弃。',
  client_update_failed: '无法启动安装程序，请从“客户端下载”页手动下载安装。',
  local_runs_active: '请先关闭本机运行的环境，再更新客户端。',
  client_update_unsupported: '源码运行的客户端请用 Git 更新，不能运行安装包。',
}

/**
 * Generate a cryptographically secure UUIDv4 that works across HTTPS and non-secure HTTP contexts.
 * Adheres to tribal rule: no timestamps, no Math.random, uses crypto.getRandomValues().
 */
export function generateRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64 // version 4
  bytes[8] = (bytes[8] & 63) | 128 // variant RFC4122
  const hex = Array.from(bytes).map((b) => b.toString(16).padStart(2, '0'))
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10).join(''),
  ].join('-')
}
