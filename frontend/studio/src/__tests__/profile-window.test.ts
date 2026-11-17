import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, ssrContextKey } from 'vue'
import ProfileToolbar from '../components/profiles/ProfileToolbar.vue'
import { openProfileWindow } from '../utils/profile-window'

const warning = vi.hoisted(() => vi.fn())

vi.mock('../stores/profiles', () => ({ useProfilesStore: () => ({
  singleSelectedProfile: { id: 'profile' }, selectedProfiles: [], selectedCount: 1,
}) }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => ({ isAdmin: true }) }))
vi.mock('../stores/notification', () => ({ useNotificationStore: () => ({ warning }) }))
vi.mock('../stores/desktop', () => ({ useDesktopStore: () => ({ available: false, state: () => undefined }) }))

afterEach(() => vi.unstubAllGlobals())

it('opens the remote window during the gesture and severs its opener', () => {
  const opened = { opener: {} }
  const open = vi.fn(() => opened)
  vi.stubGlobal('window', { location: { origin: 'https://studio.test', pathname: '/' }, open })
  const renderer = createRenderer<any, any>({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null,
  })
  let state: any
  const app = renderer.createApp({ ...ProfileToolbar, render: () => null,
    setup: (props: unknown, context: unknown) => {
      state = (ProfileToolbar as any).setup(props, context)
      return state
    },
  }, { onEdit() {}, onDetails() {}, onDelete() {}, onCookies() {} })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  try {
    state.onOpenRemote()
    expect(open).toHaveBeenCalledWith('https://studio.test/#remote=profile', '_blank')
    expect(opened.opener).toBeNull()
  } finally {
    app.unmount()
  }
})

it('uses the same safe window opener for row actions and reports a blocked popup', () => {
  const opened = { opener: {} }
  const open = vi.fn().mockReturnValueOnce(opened).mockReturnValueOnce(null)
  vi.stubGlobal('window', { location: { origin: 'https://studio.test', pathname: '/' }, open })
  openProfileWindow('row-profile')
  expect(open).toHaveBeenCalledWith('https://studio.test/#remote=row-profile', '_blank')
  expect(opened.opener).toBeNull()
  openProfileWindow('blocked-profile')
  expect(warning).toHaveBeenCalledOnce()
})
