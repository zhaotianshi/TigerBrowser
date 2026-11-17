import { useNotificationStore } from '../stores/notification'

export function openProfileWindow(profileId: string) {
  // Keep window creation in the user gesture so popup blockers can recognize it.
  const targetUrl = `${window.location.origin}${window.location.pathname}#remote=${profileId}`
  const opened = window.open(targetUrl, '_blank')
  if (opened) opened.opener = null
  else useNotificationStore().warning('弹窗已被浏览器拦截，请在地址栏允许弹出窗口或直接复制链接。')
}
