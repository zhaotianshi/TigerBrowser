import { describe, it, expect, vi } from 'vitest'
import { generateRequestId, ERROR_MESSAGES } from '../types/api'

describe('API Types & Helpers', () => {
  it('generates valid RFC4122 v4 UUID without crypto.randomUUID requirement', () => {
    vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) })
    try {
      const id = generateRequestId()
      expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('generates unique request IDs', () => {
    const set = new Set()
    for (let i = 0; i < 100; i++) {
      set.add(generateRequestId())
    }
    expect(set.size).toBe(100)
  })

  it('maps known server reason codes properly', () => {
    expect(ERROR_MESSAGES.session_expired).toBeDefined()
    expect(ERROR_MESSAGES.revision_conflict).toBeDefined()
    expect(ERROR_MESSAGES.private_network_required).toBeDefined()
    expect(ERROR_MESSAGES.client_not_allowed).toBeDefined()
    expect(ERROR_MESSAGES.profile_busy).toBeDefined()
  })
})
