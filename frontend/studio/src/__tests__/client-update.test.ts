import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { compatible, newerVersion } from '../api/client-update'

const mocks = vi.hoisted(() => ({ capabilities: vi.fn(), update: vi.fn(), onUpdate: vi.fn(), install: vi.fn() }))
vi.mock('../desktop/bridge', () => ({
  desktopAvailable: () => true,
  desktop: { capabilities: mocks.capabilities, onUpdate: mocks.onUpdate, installUpdate: mocks.install },
}))
vi.mock('../api/client-update', async original => ({
  ...(await original<typeof import('../api/client-update')>()),
  apiClientUpdate: mocks.update,
}))

import { useDesktopStore } from '../stores/desktop'

const PACKAGE = {
  platform: 'win-x86_64', version: '0.6.0', file: 'TigerBrowser-0.6.0-win-x86_64.exe',
  sha256: 'a'.repeat(64), size: 10, minimum_protocol: 1, protocol: 1,
}

beforeEach(() => {
  setActivePinia(createPinia())
  mocks.capabilities.mockResolvedValue({
    protocol: 1, minimum_protocol: 1, platform: 'win-x86_64', version: '0.5.7', packaged: true,
  })
  mocks.update.mockResolvedValue({ protocol: 1, minimum_protocol: 1, packages: [PACKAGE] })
})
afterEach(() => vi.resetAllMocks())

it('compares dotted versions numerically', () => {
  expect(newerVersion('0.10.0', '0.9.9')).toBe(true)
  expect(newerVersion('0.5.7', '0.5.7')).toBe(false)
  expect(newerVersion('0.5', '0.5.1')).toBe(false)
})

it('accepts a protocol range only when each side still speaks the other', () => {
  expect(compatible({ protocol: 1, minimum_protocol: 1 }, { protocol: 2, minimum_protocol: 1 })).toBe(true)
  expect(compatible({ protocol: 1, minimum_protocol: 1 }, { protocol: 3, minimum_protocol: 2 })).toBe(false)
  expect(compatible({ protocol: 3, minimum_protocol: 2 }, { protocol: 1, minimum_protocol: 1 })).toBe(false)
})

it('offers a newer installer for this platform and listens for update progress once', async () => {
  const store = useDesktopStore()
  await store.checkUpdate()
  await store.checkUpdate()
  expect(store.compatibleClient).toBe(true)
  expect(store.newerPackage).toEqual(PACKAGE)
  expect(store.packaged).toBe(true)
  expect(mocks.onUpdate).toHaveBeenCalledTimes(1)
  mocks.onUpdate.mock.calls[0][0]({ state: 'error', error: 'local_runs_active' })
  expect(store.updateState).toEqual({ state: 'error', error: 'local_runs_active' })
})

it('blocks an incompatible client and ignores other platforms', async () => {
  mocks.update.mockResolvedValue({ protocol: 3, minimum_protocol: 2, packages: [{ ...PACKAGE, platform: 'mac-arm64' }] })
  const store = useDesktopStore()
  await store.checkUpdate()
  expect(store.compatibleClient).toBe(false)
  expect(store.newerPackage).toBeNull()
})
