import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { apiBatchProfiles, apiStartProfile } from '../api/profiles'

afterEach(() => vi.restoreAllMocks())

describe('Profile launch requests', () => {
  it('starts remote execution without a display selector', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'command' })
    await apiStartProfile('profile', 'request', 'remote')
    expect(post).toHaveBeenCalledWith('/profiles/profile/start', {
      request_id: 'request', mode: 'remote',
    })
  })

  it('retains headless and legacy launch payloads without adding a display backend', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'command' })
    await apiStartProfile('profile', 'headless-request', 'headless')
    await apiStartProfile('profile', 'legacy-request')
    expect(post.mock.calls).toEqual([
      ['/profiles/profile/start', { request_id: 'headless-request', mode: 'headless' }],
      ['/profiles/profile/start', { request_id: 'legacy-request' }],
    ])
  })

  it('preserves independent request IDs for batch launches', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ items: [] })
    const items = [
      { profile_id: 'first', request_id: 'request-1' },
      { profile_id: 'second', request_id: 'request-2' },
    ]
    await apiBatchProfiles('start', items)
    expect(post).toHaveBeenCalledWith('/profiles/batch', { action: 'start', items })
  })
})
