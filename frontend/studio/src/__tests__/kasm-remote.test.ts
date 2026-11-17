import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NativeRemoteController } from '../components/remote/NativeRemoteController'
import { createKasmDisplay } from '../components/remote/kasm-display'
import type { SessionInfo } from '../types/member'

vi.mock('../components/remote/kasm-display', () => ({ createKasmDisplay: vi.fn() }))

const session = {
  csrf: 'session-csrf',
  capabilities: { key_codes: ['KeyA'] },
} as SessionInfo

class Socket {
  static OPEN = 1
  static instances: Socket[] = []
  readyState = 1
  bufferedAmount = 0
  binaryType = 'blob'
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string | Blob }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = 3; this.onclose?.() })
  constructor(public url: string) { Socket.instances.push(this) }
  receive(message: object) { this.onmessage?.({ data: JSON.stringify(message) }) }
  messages() { return this.send.mock.calls.map(([data]) => JSON.parse(data)) }
}

describe('Kasm display and native control pairing', () => {
  let controller: NativeRemoteController
  let closed: ReturnType<typeof vi.fn>
  let notify: ReturnType<typeof vi.fn>
  let disconnect: ReturnType<typeof vi.fn>
  const target = {} as HTMLElement

  beforeEach(() => {
    vi.useFakeTimers()
    Socket.instances = []
    vi.stubGlobal('WebSocket', Socket)
    vi.stubGlobal('location', { protocol: 'https:', host: 'studio.test' })
    closed = vi.fn()
    notify = vi.fn()
    disconnect = vi.fn()
    vi.mocked(createKasmDisplay).mockResolvedValue({ disconnect })
    controller = new NativeRemoteController('run', session, vi.fn(), notify, closed)
  })

  afterEach(() => {
    controller.close()
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function connect() {
    controller.connectKasm('/team/v1/remote/run/control', '/team/v1/remote/run/display', target)
    const control = Socket.instances[0]!
    control.onopen?.()
    control.receive({ viewer_id: 'logical-viewer', generation: 1, mine: false, width: 1920, height: 1080 })
    const display = Socket.instances[1]!
    display.onopen?.()
    return { control, display }
  }

  it('pairs same-origin sockets through CSRF and viewer hello, then waits for the decoder before binary starts', async () => {
    let install!: (value: { disconnect: () => void }) => void
    vi.mocked(createKasmDisplay).mockReturnValue(new Promise((resolve) => { install = resolve }))
    const { control, display } = connect()
    expect(control.url).toBe('wss://studio.test/team/v1/remote/run/control')
    expect(control.messages()).toEqual([{ csrf: 'session-csrf' }])
    expect(display.messages()).toEqual([{ csrf: 'session-csrf', viewer_id: 'logical-viewer' }])
    expect(display.binaryType).toBe('arraybuffer')
    display.receive({ ready: true })
    expect(createKasmDisplay).toHaveBeenCalledWith(target, display)
    expect(display.messages()).toHaveLength(1)
    install({ disconnect })
    await Promise.resolve()
    expect(display.messages()).toEqual([{ csrf: 'session-csrf', viewer_id: 'logical-viewer' }, { ready: true }])
    control.receive({ generation: 2, mine: true })
    controller.handleCompositionText('中文😀')
    expect(control.messages().at(-1)).toMatchObject({ kind: 'input', generation: 2, sequence: 1, event: { kind: 'text', text: '中文😀' } })
    expect(display.messages()).toHaveLength(2)
  })

  it.each(['control', 'display'] as const)('closes both channels and cancels pending input when %s disconnects', async (channel) => {
    const sockets = connect()
    sockets.display.receive({ ready: true })
    await Promise.resolve()
    sockets.control.receive({ generation: 2, mine: true })
    const rect = { left: 0, top: 0, width: 1920, height: 1080 } as DOMRect
    controller.handlePointerMove(10, 20, rect)
    controller.handlePointerMove(30, 20, rect)
    const pending = controller.readClipboard('clipboard')
    const rejected = expect(pending).rejects.toThrow('断开')
    sockets[channel].onclose?.()
    vi.advanceTimersByTime(10000)
    await rejected
    expect(sockets.control.close).toHaveBeenCalledOnce()
    expect(sockets.display.close).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(closed).toHaveBeenCalledOnce()
    expect(controller.mine).toBe(false)
    expect(sockets.control.messages().filter((m) => m.kind === 'input')).toHaveLength(1)
  })

  it('rejects decoder failure without switching transport or leaving an input connection alive', async () => {
    vi.mocked(createKasmDisplay).mockRejectedValue(new Error('decode failed'))
    const { control, display } = connect()
    display.receive({ ready: true })
    await Promise.resolve()
    expect(closed).toHaveBeenCalledOnce()
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('KasmVNC'), true)
    expect(control.close).toHaveBeenCalledOnce()
    expect(display.close).toHaveBeenCalledOnce()
    expect(display.messages()).toHaveLength(1)
    expect(Socket.instances).toHaveLength(2)
  })

  it('disconnects a late decoder after the viewer is closed without acknowledging readiness', async () => {
    let install!: (value: { disconnect: () => void }) => void
    vi.mocked(createKasmDisplay).mockReturnValue(new Promise((resolve) => { install = resolve }))
    const { display } = connect()
    display.receive({ ready: true })
    controller.close()
    install({ disconnect })
    await Promise.resolve()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(display.messages()).toHaveLength(1)
    expect(closed).not.toHaveBeenCalled()
  })

  it('requires the paired display to be ready before claiming control or sending input', () => {
    const { control } = connect()
    controller.claimControl()
    control.receive({ generation: 2, mine: true })
    expect(controller.sendText('pending')).toBe(false)
    expect(control.messages()).toEqual([{ csrf: 'session-csrf' }])
  })

  it('rejects a display URL containing a query before opening either connection', () => {
    expect(() => controller.connectKasm('/team/v1/remote/run/control', '/team/v1/remote/run/display?secret=x', target)).toThrow('地址无效')
    expect(Socket.instances).toHaveLength(0)
  })

  it('notifies the view when control backpressure closes both Kasm channels', async () => {
    const { control, display } = connect()
    display.receive({ ready: true })
    await Promise.resolve()
    control.receive({ generation: 2, mine: true })
    control.bufferedAmount = 8 * 1024 * 1024
    expect(controller.sendText('中文')).toBe(false)
    expect(control.close).toHaveBeenCalledOnce()
    expect(display.close).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(closed).toHaveBeenCalledOnce()
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('网络积压'), true)
  })

  it('closes a rejected display hello before installing the decoder', () => {
    const { control, display } = connect()
    display.receive({ error: 'access_denied' })
    expect(createKasmDisplay).not.toHaveBeenCalled()
    expect(control.close).toHaveBeenCalledOnce()
    expect(display.close).toHaveBeenCalledOnce()
    expect(closed).toHaveBeenCalledOnce()
  })
})
