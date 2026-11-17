import { describe, it, expect, vi } from 'vitest'
import { createRenderer, nextTick, reactive, ssrContextKey } from 'vue'
import App from '../App.vue'

const mocks = vi.hoisted(() => ({ connect: vi.fn(), disconnect: vi.fn() }))
const route = reactive({ meta: { remote: true, public: false } })
const auth = reactive({ isAuthenticated: false })
vi.mock('vue-router', () => ({ useRoute: () => route }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('../stores/events', () => ({ useEventsStore: () => mocks }))
vi.mock('../stores/desktop', () => ({ useDesktopStore: () => ({ available: false }) }))
vi.mock('../components/layout/Sidebar.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/layout/Header.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/layout/TransportWarning.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/common/Toast.vue', () => ({ default: { render: () => null } }))

const renderer = createRenderer<any, any>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert: () => {}, remove: () => {}, setText: () => {}, setElementText: () => {},
  patchProp: () => {}, parentNode: () => null, nextSibling: () => null,
})

describe('console event connection', () => {
  it.each(['remote', 'public'] as const)('connects when entering the console from %s without a new login', async (kind) => {
    vi.clearAllMocks()
    auth.isAuthenticated = false
    route.meta.remote = kind === 'remote'
    route.meta.public = kind === 'public'
    const app = renderer.createApp({ ...App, render: () => null })
    app.provide(ssrContextKey, { modules: new Set() })
    app.component('router-view', { render: () => null })
    app.mount({})
    try {
      auth.isAuthenticated = true
      await nextTick()
      expect(mocks.connect).not.toHaveBeenCalled()
      route.meta.remote = false
      route.meta.public = false
      await nextTick()
      expect(mocks.connect).toHaveBeenCalledTimes(1)
      route.meta.remote = true
      await nextTick()
      expect(mocks.disconnect).toHaveBeenCalled()
    } finally {
      app.unmount()
    }
  })
})
