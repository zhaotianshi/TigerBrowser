import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useProfilesStore } from '../stores/profiles'
import type { ProfileSummary } from '../types/profile'

const mockProfiles: ProfileSummary[] = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    name: '电商环境 A',
    group_name: '跨境运营',
    tags: ['Amazon', 'US'],
    os: 'windows',
    kernel: 'official/152.0.4-beta.31',
    locale: 'en-US',
    archived: false,
    revision: 1,
    last_launch_at: null,
    proxy: { id: 'p1', name: 'US Proxy', server: 'http://1.1.1.1:8080' },
    permissions: ['view', 'watch', 'operate'],
    run: {
      id: 'r1',
      state: 'running',
      mode: 'remote',
      error: null,
      updated_at: '2026-10-05T00:00:00Z',
    },
    checked_out: null,
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    name: '爬虫环境 B',
    group_name: '数据采集',
    tags: ['Scraper'],
    os: 'linux',
    kernel: 'official/152.0.4-beta.31',
    locale: 'zh-CN',
    archived: false,
    revision: 2,
    last_launch_at: '2026-10-04T12:00:00Z',
    proxy: null,
    permissions: ['view'],
    run: null,
    checked_out: null,
  },
  {
    id: '33333333-3333-4333-8333-333333333333',
    name: '归档环境 C',
    group_name: '跨境运营',
    tags: ['Old'],
    os: 'macos',
    kernel: 'official/152.0.4-beta.31',
    locale: 'ja-JP',
    archived: true,
    revision: 1,
    last_launch_at: null,
    proxy: null,
    permissions: ['view'],
    run: null,
    checked_out: null,
  },
]

describe('Profiles Store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('filters active and archived profiles properly', () => {
    const store = useProfilesStore()
    store.setProfiles(mockProfiles)

    // Default view: non-archived
    expect(store.filteredProfiles.length).toBe(2)
    expect(store.totalCount).toBe(2)
    expect(store.runningCount).toBe(1)
    expect(store.boundProxyCount).toBe(1)

    // Switch to archived
    store.showArchived = true
    expect(store.filteredProfiles.length).toBe(1)
    expect(store.filteredProfiles[0].name).toBe('归档环境 C')
  })

  it('filters by search keyword across name, group, tags and proxy', () => {
    const store = useProfilesStore()
    store.setProfiles(mockProfiles)

    store.searchQuery = 'Amazon'
    expect(store.filteredProfiles.length).toBe(1)
    expect(store.filteredProfiles[0].id).toBe('11111111-1111-4111-8111-111111111111')

    store.searchQuery = '采集'
    expect(store.filteredProfiles.length).toBe(1)
    expect(store.filteredProfiles[0].id).toBe('22222222-2222-4222-8222-222222222222')
  })

  it('handles single, toggle, and range selection', () => {
    const store = useProfilesStore()
    store.setProfiles(mockProfiles)

    store.selectOne('11111111-1111-4111-8111-111111111111')
    expect(store.selectedCount).toBe(1)
    expect(store.singleSelectedProfile?.name).toBe('电商环境 A')

    store.toggleSelect('22222222-2222-4222-8222-222222222222')
    expect(store.selectedCount).toBe(2)
    expect(store.singleSelectedProfile).toBeNull()

    store.clearSelection()
    expect(store.selectedCount).toBe(0)
  })
})
