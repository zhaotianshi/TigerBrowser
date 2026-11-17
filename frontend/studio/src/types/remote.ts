import type { ProfileSummary } from './profile'

export const MAX_TEXT_BYTES = 1024 * 1024

export type RemoteUnavailableReason =
  | 'watch_required'
  | 'remote_disabled'
  | 'service_closing'
  | 'recovery_required'
  | 'not_running'
  | 'starting'
  | 'stopping'
  | 'not_remote'
  | 'runtime_unavailable'

export const REMOTE_REASON_TEXT: Record<RemoteUnavailableReason, string> = {
  watch_required: '没有观看权限，请联系管理员分配环境授权。',
  remote_disabled: '服务执行机当前不是原生远程（remote）模式。',
  service_closing: '团队服务正在关闭，暂无法连接。',
  recovery_required: '执行机遇到屏障需要恢复，无法连接。',
  not_running: '环境尚未启动，请在控制台先启动环境。',
  starting: '环境正在启动中，请稍候…',
  stopping: '环境正在停止中，无法连接。',
  not_remote: '当前运行的浏览器不是原生 remote 模式（例如处于无头模式）。',
  runtime_unavailable: '原生显示会话暂不可用，请等待初始化。',
}

export interface RemoteEntryResponse {
  profile: ProfileSummary
  available: boolean
  reason: RemoteUnavailableReason | null
  viewer_path: string
  transport: 'kasmvnc' | null
  protocol_version: 2
  control_path: string | null
  display_path: string | null
}

export interface RemoteStatusMessage {
  run_id: string
  generation: number
  controller: string | null
  mine: boolean
  width: number
  height: number
  error?: string
  viewer_id?: string
}

export type RemotePointerEvent = {
  kind: 'pointer'
  x: number
  y: number
  button: number
  down: boolean
}

export type RemoteKeyEvent = {
  kind: 'key'
  code: string
  down: boolean
}

export type RemoteWheelEvent = {
  kind: 'wheel'
  up: boolean
}

export type RemoteTextEvent = {
  kind: 'text'
  text: string
}

export type RemoteEvent =
  | RemotePointerEvent
  | RemoteKeyEvent
  | RemoteWheelEvent
  | RemoteTextEvent

export interface RemoteFileItem {
  name: string
  size: number
}
