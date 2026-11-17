import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useEventsStore } from '../stores/events'

const session = (csrf: string) => ({
  member: { id: 'member', name: 'admin', display: 'Admin', role: 'admin', active: true },
  csrf, expires_at: '2027-01-01T00:00:00Z', capabilities: {},
})
const json = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'Content-Type': 'application/json' },
})

class Socket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: Socket[] = []
  readyState = 0
  onopen?: () => void
  onclose?: () => void
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  send = vi.fn()
  constructor() { Socket.instances.push(this) }
  open() { this.readyState = 1; this.onopen?.() }
  close() { this.readyState = 3; this.onclose?.() }
}

beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('location', { protocol: 'http:', host: 'studio.test' })
  api.setCsrf('')
})

afterEach(() => {
  useEventsStore().disconnect()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function connected() {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(session('before-restart'))))
  await useAuthStore().checkSession()
  const events = useEventsStore()
  events.connect()
  Socket.instances[0].open()
  return events
}

describe('session recovery and event ownership', () => {
  it('refreshes CSRF with a read-only session check before reconnecting', async () => {
    await connected()
    const fetch = vi.fn().mockResolvedValue(json(session('after-restart')))
    vi.stubGlobal('fetch', fetch)
    Socket.instances[0].close()
    await vi.advanceTimersByTimeAsync(3000)
    expect(fetch).toHaveBeenCalledWith('/team/v1/me', expect.objectContaining({ method: 'GET' }))
    expect(api.getCsrf()).toBe('after-restart')
    expect(Socket.instances).toHaveLength(2)
  })

  it('retries read-only validation after an outage without discarding the session', async () => {
    await connected()
    const fetch = vi.fn().mockRejectedValue(new TypeError('network unavailable'))
    vi.stubGlobal('fetch', fetch)
    Socket.instances[0].close()
    await vi.advanceTimersByTimeAsync(3000)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(useAuthStore().isAuthenticated).toBe(true)
    expect(Socket.instances).toHaveLength(1)
    fetch.mockResolvedValue(json(session('recovered')))
    await vi.advanceTimersByTimeAsync(4500)
    expect(Socket.instances).toHaveLength(2)
    expect(fetch.mock.calls.every(([, options]) => options.method === 'GET' &&
      !Object.hasOwn(options.headers, 'X-CSRF-Token'))).toBe(true)
  })

  it('ignores a session response received after logout, including its CSRF', async () => {
    const auth = useAuthStore()
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn((url: string) => url.endsWith('/me')
      ? new Promise<Response>((resolve) => { finish = resolve })
      : Promise.resolve(json({ ok: true }))))
    const pending = auth.checkSession()
    await auth.logout()
    finish(json(session('stale-response')))
    expect(await pending).toBe(false)
    expect(auth.session).toBeNull()
    expect(api.getCsrf()).toBe('')
  })

  it('ignores stale close and snapshot messages from a replaced socket', async () => {
    const events = await connected()
    const old = Socket.instances[0]
    old.close()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(session('recovered'))))
    await vi.advanceTimersByTimeAsync(3000)
    Socket.instances[1].open()
    old.onclose?.()
    old.onmessage?.({ data: JSON.stringify({ profiles: [] }) })
    expect(events.state).toBe('connected')
    expect(events.lastUpdated).toBeNull()
    await vi.advanceTimersByTimeAsync(15000)
    expect(Socket.instances).toHaveLength(2)
  })
})
