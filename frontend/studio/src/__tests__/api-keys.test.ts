import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, reactive, ssrContextKey } from 'vue'
import ApiKeysView from '../views/ApiKeysView.vue'

const mocks = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), revoke: vi.fn(), download: vi.fn(), auth: {} as any }))
vi.mock('../api/api-keys', () => ({ apiListKeys: mocks.list, apiCreateKey: mocks.create, apiRevokeKey: mocks.revoke }))
vi.mock('../api/profiles', () => ({ apiListProfiles: async () => [] }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('../stores/notification', () => ({ useNotificationStore: () => ({ success: vi.fn() }) }))
vi.mock('../utils/download', () => ({ downloadBlob: mocks.download }))
const renderer = createRenderer<any, any>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
let app: ReturnType<typeof renderer.createApp>
const connection = { protocolVersion: 1, baseURL: 'http://team.test:8767', token: 'one-time-test-credential' }
beforeEach(() => {
  mocks.auth = reactive({ session: { member: { id: 'operator' } }, isOperator: true })
  mocks.list.mockResolvedValue({ keys: [], enabled: true })
  mocks.create.mockResolvedValue({ key: { id: 'key' }, connection })
})
afterEach(() => { app?.unmount(); vi.resetAllMocks() })
async function mount() {
  let state: any
  app = renderer.createApp({ ...ApiKeysView, render: () => null,
    setup: (props: unknown, context: unknown) => {
      state = (ApiKeysView as any).setup(props, context)
      return state
    },
  })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  await new Promise<void>(resolve => setImmediate(resolve))
  return state
}
it('exports the unchanged protocol connection and clears the one-time credential on logout', async () => {
  const state = await mount()
  state.name.value = 'Worker'
  state.ips.value = '192.0.2.1\n192.0.2.0/24'
  await state.create()
  expect(mocks.create).toHaveBeenCalledWith({ name: 'Worker', profile_ids: null, expires_at: null,
    allowed_ips: ['192.0.2.1', '192.0.2.0/24'] })
  state.download()
  expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), 'automation.json')
  expect(JSON.parse(await mocks.download.mock.calls[0][0].text())).toEqual(connection)
  mocks.auth.session = null
  expect(state.connection.value).toBeNull()
  expect(state.keys.value).toEqual([])
})
it('discards a creation response arriving after logout', async () => {
  let finish!: (value: any) => void
  mocks.create.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const state = await mount()
  const pending = state.create()
  mocks.auth.session = null
  finish({ key: { id: 'key' }, connection })
  await pending
  expect(state.connection.value).toBeNull()
  expect(state.keys.value).toEqual([])
})
it('discards a creation response after the view is closed', async () => {
  let finish!: (value: any) => void
  mocks.create.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const state = await mount()
  const pending = state.create()
  app.unmount()
  finish({ key: { id: 'key' }, connection })
  await pending
  expect(state.connection.value).toBeNull()
})
