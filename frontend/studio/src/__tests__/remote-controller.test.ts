import { afterEach, describe, it, expect, vi } from 'vitest'
import { NativeRemoteController } from '../components/remote/NativeRemoteController'
import type { SessionInfo } from '../types/member'

const mockSession: SessionInfo = {
  member: {
    id: 'm1',
    name: 'admin',
    display: 'Admin',
    role: 'admin',
    active: true,
    permission_version: 1,
  },
  expires_at: '2026-10-06T00:00:00Z',
  csrf: 'test-csrf-token',
  workspace_id: '44444444-4444-4444-8444-444444444444',
  capabilities: {
    browser_mode: 'remote',
    remote_display: true,
    control_handover: true,
    file_transfer: true,
    launch_modes: ['remote', 'headless'],
    team_automation: false,
    online_maintenance: false,
    secure_transport: true,
    private_network_only: false,
    client_allowlist: false,
    key_codes: ['KeyA', 'KeyB', 'Enter', 'Escape'],
  },
}

describe('NativeRemoteController', () => {
  it('matches clipboard responses to the pending request and rejects on disconnect', async () => {
    let socket: any
    class Socket {
      static OPEN = 1
      readyState = 1
      bufferedAmount = 0
      onmessage: any
      onclose: any
      send = vi.fn()
      close = vi.fn()
      constructor() { socket = this }
    }
    vi.stubGlobal('WebSocket', Socket)
    vi.stubGlobal('location', { protocol: 'http:', host: 'example.test' })
    const controller = new NativeRemoteController('run', mockSession, vi.fn(), vi.fn(), vi.fn())
    try {
      controller.connectKasm('/remote/run/control', '/remote/run/display', {} as HTMLElement)
      ;(controller as any).displaySocket = { close() {} }
      controller.mine = true
      controller.generation = 1
      const received = controller.readClipboard('primary')
      expect(JSON.parse(socket.send.mock.calls[0][0])).toEqual({ kind: 'clipboard', generation: 1, source: 'primary', request_id: 1 })
      socket.onmessage({ data: JSON.stringify({ generation: 1, mine: true, clipboard: 'stale', clipboard_request: 0 }) })
      socket.onmessage({ data: JSON.stringify({ generation: 1, mine: true, clipboard: '中文😀\n'.repeat(1000), clipboard_request: 1 }) })
      expect(await received).toBe('中文😀\n'.repeat(1000))
      const pending = controller.readClipboard('clipboard')
      const rejection = expect(pending).rejects.toThrow('断开')
      socket.onclose()
      await rejection
      expect(controller.mine).toBe(false)
    } finally {
      controller.close()
      vi.unstubAllGlobals()
    }
  })
  it('sends long Unicode text intact, including whitespace and line breaks', () => {
    const controller = new NativeRemoteController('run', mockSession, vi.fn(), vi.fn(), vi.fn())
    controller.mine = true
    const send = vi.spyOn(controller, 'send').mockReturnValue(true)
    const text = '  https://example.com/?q=' + '中文😀'.repeat(10000) + '\nsecond line  '
    controller.sendText(text)
    expect(send).toHaveBeenCalledWith('input', { sequence: 1, event: { kind: 'text', text } })
    controller.handleCompositionText(text)
    expect(send).toHaveBeenLastCalledWith('input', { sequence: 2, event: { kind: 'text', text } })
  })

  it('rejects oversized text without truncating or consuming an input sequence', () => {
    const notify = vi.fn()
    const controller = new NativeRemoteController('run', mockSession, vi.fn(), notify, vi.fn())
    controller.mine = true
    const send = vi.spyOn(controller, 'send')
    controller.sendText('中'.repeat(400000))
    expect(send).not.toHaveBeenCalled()
    expect(controller.sequence).toBe(0)
    expect(notify).toHaveBeenCalled()
  })

  it('correctly manages sequence numbers and coordinate clamping', () => {
    const notify = vi.fn()
    const onStatus = vi.fn()
    const onClosed = vi.fn()

    const controller = new NativeRemoteController(
      'test-run-id',
      mockSession,
      onStatus,
      notify,
      onClosed,
    )

    controller.mine = true
    controller.width = 1920
    controller.height = 1080
    controller.generation = 1

    const sentMessages: any[] = []
    controller.send = (kind, fields) => {
      sentMessages.push({ kind, generation: controller.generation, ...fields })
      return true
    }

    // Pointer event with coordinate clamping
    const rect = {
      left: 0,
      top: 0,
      width: 960,
      height: 540,
    } as DOMRect

    // Click inside
    controller.handlePointer(480, 270, rect, true, 1)
    expect(controller.sequence).toBe(1)
    expect(sentMessages[0].kind).toBe('input')
    expect(sentMessages[0].sequence).toBe(1)
    expect(sentMessages[0].event.kind).toBe('pointer')
    expect(sentMessages[0].event.x).toBe(960)
    expect(sentMessages[0].event.y).toBe(540)
    expect(sentMessages[0].event.button).toBe(1)
    expect(sentMessages[0].event.down).toBe(true)

    // Click outside boundary (clamp check)
    controller.handlePointer(1200, 600, rect, false, 1)
    expect(controller.sequence).toBe(2)
    expect(sentMessages[1].event.x).toBe(1919)
    expect(sentMessages[1].event.y).toBe(1079)
  })

  it('filters keys against key_codes whitelist and routes IME text to text events', () => {
    const controller = new NativeRemoteController(
      'test-run-id',
      mockSession,
      () => {},
      () => {},
      () => {},
    )

    controller.mine = true
    controller.generation = 1

    const sentMessages: any[] = []
    controller.send = (kind, fields) => {
      sentMessages.push({ kind, ...fields })
      return true
    }

    // Whitelisted key
    controller.handleKey('KeyA', true, false)
    expect(sentMessages[sentMessages.length - 1].kind).toBe('input')
    expect(sentMessages[sentMessages.length - 1].event.code).toBe('KeyA')

    // Non-whitelisted key: silently ignored (the service rejects unknown
    // codes with invalid_input, so the client must not send them).
    const before = sentMessages.length
    controller.handleKey('F12', true, false)
    expect(sentMessages.length).toBe(before)

    // Keys during IME composition belong to the IME, not the remote browser;
    // they must neither be sent nor release held keys.
    controller.handleKey('KeyA', true, true)
    expect(sentMessages.length).toBe(before)

    // Committed IME text (e.g. Chinese) goes out as an explicit text event.
    controller.handleCompositionText('中文')
    expect(sentMessages[sentMessages.length - 1].kind).toBe('input')
    expect(sentMessages[sentMessages.length - 1].event.kind).toBe('text')
    expect(sentMessages[sentMessages.length - 1].event.text).toBe('中文')
  })

  it('cleans up without sending stop command', () => {
    const controller = new NativeRemoteController(
      'test-run-id',
      mockSession,
      () => {},
      () => {},
      () => {},
    )

    const sentMessages: any[] = []
    controller.send = (kind, fields) => {
      sentMessages.push({ kind, ...fields })
      return true
    }

    controller.close()
    // Closing remote viewer MUST NOT send stop to the browser!
    expect(sentMessages.some((m) => m.kind === 'stop')).toBe(false)
  })
})

describe('Remote pointer coalescing', () => {
  let controller: NativeRemoteController
  let socket: any
  const rect = { left: 0, top: 0, width: 1920, height: 1080 } as DOMRect

  function connect() {
    vi.useFakeTimers()
    vi.spyOn(performance, 'now').mockReturnValue(100)
    class Socket {
      static OPEN = 1
      readyState = 1
      bufferedAmount = 0
      onmessage: any
      onclose: any
      send = vi.fn()
      close = vi.fn()
      constructor() { socket = this }
    }
    vi.stubGlobal('WebSocket', Socket)
    vi.stubGlobal('location', { protocol: 'http:', host: 'example.test' })
    controller = new NativeRemoteController('run', mockSession, vi.fn(), vi.fn(), vi.fn())
    controller.connectKasm('/remote/run/control', '/remote/run/display', {} as HTMLElement)
    ;(controller as any).displayReady = true
    ;(controller as any).displaySocket = { close() {} }
    status(1, true)
  }

  function status(generation: number, mine: boolean) {
    socket.onmessage({ data: JSON.stringify({ generation, mine, width: 1920, height: 1080 }) })
  }

  function messages() {
    return socket.send.mock.calls.map(([data]: [string]) => JSON.parse(data))
  }

  afterEach(() => {
    controller?.close()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('delivers the last move in a burst without consuming sequences for replaced moves', () => {
    connect()
    for (const x of [10, 20, 30]) controller.handlePointerMove(x, 50, rect)
    expect(messages()).toHaveLength(1)
    expect(controller.sequence).toBe(1)
    vi.advanceTimersByTime(40)
    expect(messages().map((message: any) => [message.sequence, message.event.x])).toEqual([[1, 10], [2, 30]])
  })

  it('flushes preceding motion before a key or drag boundary', () => {
    connect()
    controller.handlePointer(10, 50, rect, true, 1)
    controller.handlePointerMove(20, 50, rect)
    controller.handlePointerMove(30, 50, rect)
    controller.handleKey('KeyA', true, false)
    controller.handlePointer(40, 50, rect, false, 1)
    vi.advanceTimersByTime(80)
    expect(messages().map((message: any) => [message.sequence, message.event.kind, message.event.x])).toEqual([
      [1, 'pointer', 10], [2, 'pointer', 20], [3, 'pointer', 30], [4, 'key', undefined], [5, 'pointer', 40],
    ])
  })

  it('does not consume sequences or queue input while the socket is unavailable', () => {
    connect()
    socket.readyState = 3
    controller.handlePointerMove(10, 50, rect)
    expect(controller.sequence).toBe(0)
    socket.readyState = 1
    controller.handlePointerMove(30, 50, rect)
    expect(messages().at(-1)).toMatchObject({ sequence: 1, event: { x: 30 } })
    vi.advanceTimersByTime(80)
    expect(messages()).toHaveLength(1)
  })

  it.each(['release', 'reset', 'generation', 'revocation', 'disconnect', 'close'])('discards pending motion on %s', (change) => {
    connect()
    controller.handlePointerMove(10, 50, rect)
    controller.handlePointerMove(20, 50, rect)
    if (change === 'release') controller.releaseControl()
    if (change === 'reset') controller.resetInput()
    if (change === 'generation') status(2, true)
    if (change === 'revocation') status(1, false)
    if (change === 'disconnect') socket.onclose()
    if (change === 'close') controller.close()
    vi.advanceTimersByTime(80)
    expect(messages().filter((message: any) => message.kind === 'input')).toHaveLength(1)
    if (change === 'generation') {
      controller.handlePointerMove(30, 50, rect)
      expect(messages().at(-1)).toMatchObject({ generation: 2, sequence: 1, event: { x: 30 } })
    }
  })
})
