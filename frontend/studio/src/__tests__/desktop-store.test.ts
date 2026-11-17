import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { ProfileSummary } from '../types/profile'

const mocks = vi.hoisted(() => ({
  attach: vi.fn(), launch: vi.fn(), states: vi.fn(), onChange: vi.fn(),
  checkout: vi.fn(), cancel: vi.fn(), fetch: vi.fn(), error: vi.fn(), info: vi.fn(),
}))
vi.mock('../desktop/bridge', () => ({
  desktopAvailable: () => true,
  desktop: { attach: mocks.attach, launch: mocks.launch, states: mocks.states, onChange: mocks.onChange },
}))
vi.mock('../api/profiles', () => ({ apiCheckoutProfile: mocks.checkout, apiCancelCheckout: mocks.cancel }))
vi.mock('../stores/profiles', () => ({ useProfilesStore: () => ({ fetchProfiles: mocks.fetch }) }))
vi.mock('../stores/notification', () => ({ useNotificationStore: () => ({ error: mocks.error, info: mocks.info }) }))

import { useDesktopStore } from '../stores/desktop'

const PROFILE = { id: '11111111-1111-4111-8111-111111111111', name: 'A' } as ProfileSummary
const DEVICE = { device_id: '22222222-2222-4222-8222-222222222222', device_name: 'Laptop', platform: 'win-x86_64' }
const LEASE = { checkout: { id: '33333333-3333-4333-8333-333333333333', profile_id: PROFILE.id }, token: 't'.repeat(43) }

beforeEach(() => {
  setActivePinia(createPinia())
  mocks.attach.mockResolvedValue(DEVICE)
  mocks.states.mockResolvedValue({})
  mocks.checkout.mockResolvedValue(LEASE)
  mocks.cancel.mockResolvedValue({})
})
afterEach(() => vi.resetAllMocks())

it('checks out with this computer identity and hands the lease to the bridge', async () => {
  const store = useDesktopStore()
  await store.attach('44444444-4444-4444-8444-444444444444')
  await store.launch(PROFILE)
  expect(mocks.checkout).toHaveBeenCalledWith(PROFILE.id, expect.objectContaining(DEVICE))
  expect(mocks.checkout.mock.calls[0][1].request_id).toMatch(/^[0-9a-f-]{36}$/)
  expect(mocks.launch).toHaveBeenCalledWith(PROFILE.id, LEASE)
  expect(mocks.onChange).toHaveBeenCalledTimes(1)
})

it('continues a lease this computer holds without asking the server for another', async () => {
  mocks.states.mockResolvedValue({ [PROFILE.id]: { browser: 'stopped', lease: 'pending_return' } })
  const store = useDesktopStore()
  await store.attach('44444444-4444-4444-8444-444444444444')
  await store.launch(PROFILE)
  expect(mocks.checkout).not.toHaveBeenCalled()
  expect(mocks.launch).toHaveBeenCalledWith(PROFILE.id)
})

it('gives an undelivered lease back so the server keeps its data', async () => {
  mocks.launch.mockRejectedValue(new Error('origin_denied'))
  const store = useDesktopStore()
  await store.attach('44444444-4444-4444-8444-444444444444')
  await expect(store.launch(PROFILE)).rejects.toThrow('origin_denied')
  expect(mocks.cancel).toHaveBeenCalledWith(LEASE.checkout.id, LEASE.token)
})

it('refuses to launch before the local cache is attached', async () => {
  mocks.attach.mockRejectedValue(new Error('另一个客户端进程正在使用'))
  const store = useDesktopStore()
  await store.attach('44444444-4444-4444-8444-444444444444')
  await expect(store.launch(PROFILE)).rejects.toThrow('另一个客户端进程正在使用')
  expect(mocks.checkout).not.toHaveBeenCalled()
})

it('merges pushed local state, reports failures and refreshes on lease changes', () => {
  const store = useDesktopStore()
  store.received(PROFILE.id, { browser: 'running', lease: 'ready', sync_error: null })
  expect(store.state(PROFILE.id)?.browser).toBe('running')
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
  store.received(PROFILE.id, { browser: 'stopped', lease: 'ready', error_code: 'kernel_not_cached' })
  expect(mocks.error).toHaveBeenCalledWith('服务器尚未缓存该内核的此平台安装包。')
  expect(mocks.fetch).toHaveBeenCalledTimes(1)
  store.received(PROFILE.id, { browser: 'stopped', lease: null })
  expect(store.state(PROFILE.id)).toBeUndefined()
  expect(mocks.fetch).toHaveBeenCalledTimes(2)
  store.received('', { message: '某个启动页未能加载' })
  expect(mocks.info).toHaveBeenCalledWith('某个启动页未能加载')
})
