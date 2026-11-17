export type ProxyProtocol = 'http' | 'https' | 'socks5'

export interface ProxyItem {
  id: string
  name: string
  server: string
  username: string
  has_password: boolean
  last_probe?: { timezone: string; checked_at: string }
}

export interface CreateProxyInput {
  name: string
  server: string
  username?: string
  password?: string
  clear_password?: boolean
}

export interface ProxyProbeResult {
  ip: string
  timezone: string
  latency_ms: number
  country?: string
}
