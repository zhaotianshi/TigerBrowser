import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { useProfilesStore } from '../stores/profiles'
import ProfileTable from '../components/profiles/ProfileTable.vue'
import ProfileToolbar from '../components/profiles/ProfileToolbar.vue'
import type { ProfileSummary } from '../types/profile'

const mocks = vi.hoisted(() => ({
  auth: { isAdmin: false, capabilities: { browser_mode: 'remote', launch_modes: ['remote', 'headless'] } },
  start: vi.fn(), stop: vi.fn(), batch: vi.fn(), list: vi.fn(),
  success: vi.fn(), error: vi.fn(), warning: vi.fn(),
}))
vi.mock('../stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('../stores/desktop', () => ({ useDesktopStore: () => ({ state: () => undefined }) }))
vi.mock('../stores/notification', () => ({ useNotificationStore: () => mocks }))
vi.mock('../api/profiles', () => ({
  apiListProfiles: mocks.list, apiStartProfile: mocks.start,
  apiStopProfile: mocks.stop, apiBatchProfiles: mocks.batch,
}))

function profile(id: string, overrides: Partial<ProfileSummary> = {}): ProfileSummary {
  return {
    id, name: id, group_name: '', tags: [], os: 'linux', locale: 'zh-CN',
    kernel: 'official/152.0.4-beta.31', archived: false, revision: 1,
    last_launch_at: null, proxy: null, permissions: ['view', 'watch', 'operate'],
    run: null, checked_out: null, ...overrides,
  }
}

const running = { id: 'run-1', state: 'running' as const, mode: 'remote', error: null, updated_at: '' }

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mocks.auth.isAdmin = false
  mocks.auth.capabilities = { browser_mode: 'remote', launch_modes: ['remote', 'headless'] }
  mocks.list.mockResolvedValue([])
})

describe('shared row and batch profile actions', () => {
  it.each([false, true])('starts only the supplied row, independently of selection (headless=%s)', async (headless) => {
    const store = useProfilesStore()
    const target = profile('target')
    store.setProfiles([target, profile('selected')])
    store.selectOne('selected')
    mocks.list.mockResolvedValue(store.profiles)
    await store.startProfiles([target], headless)
    expect(mocks.start).toHaveBeenCalledWith('target', expect.any(String), headless ? 'headless' : 'remote')
    expect(mocks.batch).not.toHaveBeenCalled()
    expect([...store.selectedIds]).toEqual(['selected'])
  })

  it('stops the exact run of the supplied row and refreshes its state', async () => {
    const store = useProfilesStore()
    await store.stopProfiles([profile('target', { run: running })])
    expect(mocks.stop).toHaveBeenCalledWith('target', expect.any(String), 'run-1')
    expect(mocks.list).toHaveBeenCalledOnce()
  })

  it.each([
    { permissions: ['view'] },
    { archived: true },
    { run: running },
    { run: { ...running, state: 'starting' } },
    { run: { ...running, state: 'stopping' } },
    { run: { ...running, state: 'recovery_required' } },
    { checked_out: { id: 'lease', mine: true } },
  ] as Partial<ProfileSummary>[])('refuses an unavailable launch: %j', async (overrides) => {
    const store = useProfilesStore()
    const target = profile('target', overrides)
    expect(store.startDisabledReason(target)).not.toBe('')
    await store.startProfiles([target])
    await store.startProfiles([target], true)
    expect(mocks.start).not.toHaveBeenCalled()
  })

  it('requires an advertised launch mode even for administrators', async () => {
    mocks.auth.isAdmin = true
    mocks.auth.capabilities.launch_modes = []
    const store = useProfilesStore()
    await store.startProfiles([profile('target')])
    await store.startProfiles([profile('target')], true)
    expect(mocks.start).not.toHaveBeenCalled()
  })

  it('blocks duplicate row and toolbar requests while the same profile is pending', async () => {
    const store = useProfilesStore()
    const target = profile('target')
    let resolve!: () => void
    mocks.start.mockReturnValue(new Promise<void>((done) => { resolve = done }))
    const first = store.startProfiles([target])
    expect(store.pendingActions.target).toBe('start')
    await store.startProfiles([target], true)
    await store.startProfiles([target, profile('other')])
    expect(mocks.start).toHaveBeenCalledOnce()
    expect(mocks.batch).not.toHaveBeenCalled()
    resolve()
    await first
    expect(store.pendingActions.target).toBeUndefined()
  })

  it('reports failed requests and releases their pending state without retrying', async () => {
    mocks.start.mockRejectedValue(new Error('服务不可用'))
    const store = useProfilesStore()
    await store.startProfiles([profile('target')])
    expect(mocks.error).toHaveBeenCalledWith('服务不可用')
    expect(mocks.start).toHaveBeenCalledOnce()
    expect(store.pendingActions.target).toBeUndefined()
  })

  it('blocks duplicate stops until the current request and refresh complete', async () => {
    const store = useProfilesStore()
    const target = profile('target', { run: running })
    let resolve!: () => void
    mocks.stop.mockReturnValue(new Promise<void>((done) => { resolve = done }))
    const first = store.stopProfiles([target])
    expect(store.pendingActions.target).toBe('stop')
    await store.stopProfiles([target])
    expect(mocks.stop).toHaveBeenCalledOnce()
    resolve()
    await first
    expect(store.pendingActions.target).toBeUndefined()
  })

  it('allows an administrator to launch an otherwise ungranted, stopped profile', async () => {
    mocks.auth.isAdmin = true
    await useProfilesStore().startProfiles([profile('target', { permissions: [] })])
    expect(mocks.start).toHaveBeenCalledOnce()
  })

  it('honors advertised launch modes independently of the default browser mode', async () => {
    mocks.auth.capabilities = { browser_mode: 'remote', launch_modes: ['headless'] }
    const store = useProfilesStore()
    const target = profile('target')
    expect(store.startDisabledReason(target)).not.toBe('')
    expect(store.startDisabledReason(target, true)).toBe('')
    await store.startProfiles([target], true)
    expect(mocks.start).toHaveBeenCalledWith('target', expect.any(String), 'headless')
  })

  it('keeps independent batch IDs and the original names when selection changes', async () => {
    const store = useProfilesStore()
    let resolve!: (value: unknown) => void
    mocks.batch.mockReturnValue(new Promise((done) => { resolve = done }))
    const request = store.startProfiles([profile('first'), profile('second')], true)
    store.clearSelection()
    resolve([{ profile_id: 'first', command: {} }, { profile_id: 'second', error: 'operation_failed' }])
    await request
    const [action, items] = mocks.batch.mock.calls[0]
    expect(action).toBe('headless_start')
    expect(items.map((item: any) => item.profile_id)).toEqual(['first', 'second'])
    expect(new Set(items.map((item: any) => item.request_id)).size).toBe(2)
    expect(mocks.success).toHaveBeenCalledWith('已提交 1 个环境的启动请求')
    expect(mocks.error.mock.calls[0][0]).toContain('second：')
  })

  it('stops only active runs in a mixed batch and retains their run IDs', async () => {
    mocks.batch.mockResolvedValue([])
    const store = useProfilesStore()
    await store.stopProfiles([
      profile('first', { run: running }), profile('idle'),
      profile('second', { run: { ...running, id: 'run-2', state: 'starting' } }),
    ])
    expect(mocks.batch).toHaveBeenCalledWith('stop', [
      expect.objectContaining({ profile_id: 'first', run_id: 'run-1' }),
      expect.objectContaining({ profile_id: 'second', run_id: 'run-2' }),
    ])
  })

  it.each(['stopped', 'stopping', 'recovery_required'] as const)('does not stop a %s run', async (state) => {
    const store = useProfilesStore()
    await store.stopProfiles([profile('target', { run: { ...running, state } })])
    expect(mocks.stop).not.toHaveBeenCalled()
  })

  it('does not stop without operate permission', async () => {
    const store = useProfilesStore()
    await store.stopProfiles([profile('target', { permissions: ['watch'], run: running })])
    expect(mocks.stop).not.toHaveBeenCalled()
  })
})

it('renders labelled row actions without requiring a selected profile', async () => {
  const pinia = createPinia()
  const store = useProfilesStore(pinia)
  store.setProfiles([profile('idle'), profile('active', { run: running })])
  const html = await renderToString(createSSRApp(ProfileTable, { onRowDoubleClick() {} }).use(pinia))
  expect(html).toContain('操作</th>')
  expect(html).toContain('aria-label="启动 idle"')
  expect(html).toContain('aria-label="无头启动 idle"')
  expect(html).toContain('aria-label="停止 active"')
  expect(store.selectedCount).toBe(0)
})

describe('persistent profile management toolbar', () => {
  async function renderToolbar(targets: ProfileSummary[] = []) {
    const pinia = createPinia()
    const store = useProfilesStore(pinia)
    store.setProfiles(targets)
    for (const target of targets) store.toggleSelect(target.id)
    return renderToString(createSSRApp(ProfileToolbar, {
      onEdit() {}, onDetails() {}, onDelete() {}, onCookies() {},
    }).use(pinia))
  }

  it('keeps management actions visible and disabled before a selection', async () => {
    mocks.auth.isAdmin = true
    const html = await renderToolbar()
    const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) || []
    for (const label of ['远程窗口', '编辑', '复制', '归档', '删除', '详情', 'Cookie']) {
      expect(buttons.some((button) => new RegExp(`<span\\b[^>]*>${label}</span>`).test(button)), `${label} should remain visible`).toBe(true)
    }
    expect(buttons.length).toBeGreaterThanOrEqual(7)
    expect(buttons.every((button) => /\bdisabled\b/.test(button))).toBe(true)
    expect(html).toContain('勾选环境后管理')
    expect(html).not.toContain('批量启动')
  })

  it('retains the Cookie permission boundary for non-administrators', async () => {
    const html = await renderToolbar()
    expect(html).toMatch(/<span\b[^>]*>详情<\/span>/)
    expect(html).not.toMatch(/<span\b[^>]*>Cookie<\/span>/)
  })

  it('enables single-profile management and shows batch controls only for multiple profiles', async () => {
    mocks.auth.isAdmin = true
    const single = await renderToolbar([profile('first')])
    const edit = (single.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) || [])
      .find((button) => /<span\b[^>]*>编辑<\/span>/.test(button))
    expect(edit).toBeDefined()
    expect(edit).not.toMatch(/\bdisabled\b/)
    expect(single).not.toContain('批量启动')
    const multiple = await renderToolbar([profile('first'), profile('second')])
    expect(multiple).toContain('批量启动')
    expect(multiple).toContain('批量无头启动')
    expect(multiple).toContain('批量停止')
  })
})
