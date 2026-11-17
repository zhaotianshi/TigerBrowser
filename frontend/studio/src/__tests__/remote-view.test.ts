import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import RemoteView from '../views/RemoteView.vue'

const commands = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), get: vi.fn() }))
vi.mock('../api/profiles', () => ({ apiStartProfile: commands.start, apiStopProfile: commands.stop }))
vi.mock('../api/health', () => ({ apiGetCommand: commands.get }))

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { profileId: 'profile' } }) }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => ({
  isAuthenticated: true, isAdmin: true,
  session: { csrf: 'csrf', capabilities: { key_codes: ['KeyS', 'ShiftLeft', 'Escape'] } },
}) }))
vi.mock('../api/remote', () => ({ apiGetRemoteEntry: async () => ({
  available: true, profile: { id: 'profile', permissions: ['operate'], run: { id: 'run' } },
  control_path: '/team/v1/remote/run/control', display_path: '/team/v1/remote/run/display', transport: 'kasmvnc', protocol_version: 2,
}) }))

const renderer = createRenderer<any, any>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert: () => {}, remove: () => {}, setText: () => {}, setElementText: () => {},
  patchProp: () => {}, parentNode: () => null, nextSibling: () => null,
})
let app: ReturnType<typeof renderer.createApp>
let state: any
let socket: any
let document: EventTarget & { fullscreenElement: unknown; exitFullscreen: () => Promise<void> }

beforeEach(async () => {
  document = Object.assign(new EventTarget(), {
    fullscreenElement: null as unknown,
    async exitFullscreen() { document.fullscreenElement = null; document.dispatchEvent(new Event('fullscreenchange')) },
  })
  vi.stubGlobal('document', document)
  vi.stubGlobal('window', new EventTarget())
  vi.stubGlobal('location', { protocol: 'http:', host: 'studio.test' })
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
  app = renderer.createApp({ ...RemoteView, render: () => null, setup: (props: unknown, context: unknown) => {
    state = (RemoteView as any).setup(props, context)
    state.kasmRef.value = { querySelector: () => null }
    return state
  } })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  await new Promise<void>((resolve) => setImmediate(resolve))
  state.kasmRef.value = { querySelector: () => null }
  ;(state.controller.value as any).displayReady = true
  ;(state.controller.value as any).displaySocket = { close() {} }
  socket.onmessage({ data: JSON.stringify({ generation: 1, mine: true, width: 1920, height: 1080 }) })
  await nextTick()
})

afterEach(() => {
  app.unmount()
  vi.useRealTimers()
  vi.resetAllMocks()
  vi.unstubAllGlobals()
})

function messages() {
  return socket.send.mock.calls.map(([value]: [string]) => JSON.parse(value))
}

it('resets held modifiers when IME composition begins', () => {
  state.onKeyDown({ code: 'ShiftLeft', key: 'Shift', isComposing: false, preventDefault() {} })
  state.onCompositionStart()
  expect(messages().at(-1).kind).toBe('reset-input')
})

it('does not forward IME Process keys even when isComposing is false', () => {
  state.onKeyDown({ code: 'KeyS', key: 'Process', keyCode: 229, isComposing: false, preventDefault() {} })
  expect(messages()).toEqual([])
})

it('changes fullscreen presentation without changing the remote screen or sending input', async () => {
  const surface = { async requestFullscreen() {
    document.fullscreenElement = surface
    document.dispatchEvent(new Event('fullscreenchange'))
  } }
  state.viewportRef.value = surface
  await state.toggleFullscreen()
  expect(document.fullscreenElement).toBe(surface)
  const preventDefault = vi.fn()
  state.onKeyDown({ code: 'Escape', isComposing: false, preventDefault })
  expect(preventDefault).not.toHaveBeenCalled()
  await state.toggleFullscreen()
  expect(document.fullscreenElement).toBeNull()
  expect(state.controller.value.width).toBe(1920)
  expect(messages()).toEqual([])
})

it('uses scrolled canvas coordinates in original-size mode', () => {
  state.kasmRef.value = {
    focus() {}, setPointerCapture() {},
    querySelector: () => ({ width: 1920, height: 1080,
      getBoundingClientRect: () => ({ left: -500, top: -200, width: 1920, height: 1080 }),
    }),
  }
  state.onPointerDown({ clientX: 100, clientY: 50, pointerId: 1, button: 0 })
  expect(messages().at(-1).event).toEqual({ kind: 'pointer', x: 600, y: 250, button: 1, down: true })
})

it('releases the lease on window blur and only held input on surface cancellation', () => {
  state.onPointerCancel()
  expect(messages().at(-1).kind).toBe('reset-input')
  expect(state.controller.value.mine).toBe(true)
  window.dispatchEvent(new Event('blur'))
  expect(messages().at(-1).kind).toBe('release')
})

it('keeps a recoverable input error from closing a live viewer', () => {
  socket.onmessage({ data: JSON.stringify({ error: 'invalid_input' }) })
  expect(socket.close).not.toHaveBeenCalled()
  expect(state.controller.value.mine).toBe(true)
  expect(state.errorNotice.value).toContain('中文')
})

it('waits for the stopped command before starting another run', async () => {
  vi.useFakeTimers()
  commands.stop.mockResolvedValue({ id: 'stop', state: 'accepted' })
  commands.start.mockResolvedValue({ id: 'start', state: 'accepted' })
  let finish!: (value: unknown) => void
  commands.get.mockImplementation((id) => id === 'stop'
    ? new Promise((resolve) => { finish = resolve })
    : Promise.resolve({ id, state: 'succeeded' }))
  const restarting = state.onRestartAndReconnect()
  await vi.waitFor(() => expect(commands.stop).toHaveBeenCalledTimes(1))
  await vi.advanceTimersByTimeAsync(2000)
  expect(commands.start).not.toHaveBeenCalled()
  expect(commands.get).toHaveBeenCalledWith('stop')
  finish({ id: 'stop', state: 'succeeded' })
  await restarting
  expect(commands.start).toHaveBeenCalledTimes(1)
})
