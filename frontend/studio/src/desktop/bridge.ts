import { ERROR_MESSAGES } from '../types/api'
import type { ProfileCheckout } from '../types/profile'

// Injected by the desktop client only; ordinary browsers never see these globals.
interface QtChannel {
  objects: Record<string, any>
}

declare global {
  interface Window {
    qt?: { webChannelTransport?: unknown }
    QWebChannel?: new (transport: unknown, ready: (channel: QtChannel) => void) => unknown
  }
}

export interface LocalState {
  browser: string
  error: string | null
  lease: 'syncing' | 'ready' | 'pending_return' | 'revoked' | null
  sync_error: string | null
  recovery_required: boolean
  error_code?: string
}

export interface DesktopDevice {
  device_id: string
  device_name: string
  platform: string
}

export interface LocalSettings {
  max_running: number
  automation_enabled: boolean
}

export interface AutomationStatus {
  enabled: boolean
  connection_file: string
  error: string | null
}

export interface StaleCopy {
  checkout_id: string
  profile_id: string
  revoked_at: string
}

export interface DesktopCapabilities {
  protocol: number
  minimum_protocol: number
  platform: string
  version: string
  // False for a source checkout, which updates through Git rather than an installer.
  packaged: boolean
  attached: boolean
}

export type UpdateState =
  | { state: 'downloading' }
  | { state: 'installing' }
  | { state: 'error'; error: string }

export interface CheckoutLease {
  checkout: ProfileCheckout & { profile_id: string }
  token: string
}

export class BridgeError extends Error {
  constructor(public readonly code: string) {
    super(ERROR_MESSAGES[code] || `本机操作失败 (${code})`)
  }
}

export function desktopAvailable(): boolean {
  return typeof window !== 'undefined' && !!window.qt?.webChannelTransport && typeof window.QWebChannel === 'function'
}

let connection: Promise<any> | null = null

function connect(): Promise<any> {
  if (!desktopAvailable()) return Promise.reject(new BridgeError('client_not_attached'))
  connection ??= new Promise(resolve => {
    new window.QWebChannel!(window.qt!.webChannelTransport, channel => resolve(channel.objects.studio))
  })
  return connection
}

async function call<T>(method: string, ...args: string[]): Promise<T> {
  const studio = await connect()
  const reply = JSON.parse(await studio[method](...args))
  if (reply.error) throw new BridgeError(reply.error)
  return reply.ok as T
}

export const desktop = {
  capabilities: () => call<DesktopCapabilities>('capabilities'),
  installUpdate: () => call<boolean>('installUpdate'),
  async onUpdate(listener: (state: UpdateState) => void) {
    const studio = await connect()
    studio.updateChanged.connect((state: string) => listener(JSON.parse(state)))
  },
  attach: (workspaceId: string) => call<DesktopDevice>('attach', workspaceId),
  launch: (profileId: string, lease?: CheckoutLease) =>
    call<boolean>('launchLocal', profileId, lease ? JSON.stringify(lease) : ''),
  stop: (profileId: string) => call<boolean>('stopLocal', profileId),
  cancel: (profileId: string) => call<boolean>('cancelLocal', profileId),
  states: () => call<Record<string, LocalState>>('localStates'),
  pending: () => call<{ profiles: Record<string, LocalState>; stale: StaleCopy[] }>('pendingReturns'),
  openFolder: (profileId: string) => call<boolean>('openDataFolder', profileId),
  settings: () => call<LocalSettings>('localSettings'),
  saveSettings: (settings: Partial<LocalSettings>) => call<LocalSettings>('saveLocalSettings', JSON.stringify(settings)),
  automation: () => call<AutomationStatus>('automationStatus'),
  async onChange(listener: (profileId: string, state: any) => void) {
    const studio = await connect()
    studio.localChanged.connect((profileId: string, state: string) => listener(profileId, JSON.parse(state)))
  },
}
