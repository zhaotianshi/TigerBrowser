import { afterEach, expect, it, vi } from 'vitest'
import { waitForTask } from '../api/tasks'
import { apiGetTask } from '../api/health'
import type { TaskItem } from '../types/health'

vi.mock('../api/health', () => ({ apiGetTask: vi.fn() }))
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })

it('reports asynchronous import failure instead of success on submission', async () => {
  vi.mocked(apiGetTask).mockResolvedValue({ id: 'import', state: 'failed', error: 'operation_failed' } as any)
  await expect(waitForTask({ id: 'import', state: 'queued' } as any)).rejects.toThrow()
})

it('returns the completed import count after the running task finishes', async () => {
  vi.useFakeTimers()
  vi.mocked(apiGetTask).mockResolvedValueOnce({ id: 'import', state: 'running' } as any)
    .mockResolvedValueOnce({ id: 'import', state: 'succeeded', result: { imported: 3 } } as any)
  const result = waitForTask({ id: 'import', state: 'accepted' } as TaskItem<{ imported: number }>)
  await vi.advanceTimersByTimeAsync(1000)
  expect((await result).result?.imported).toBe(3)
})

it('stops querying a closed import dialog', async () => {
  const control = new AbortController()
  control.abort()
  await expect(waitForTask({ id: 'import', state: 'queued' } as any, control.signal)).rejects.toThrow()
  expect(apiGetTask).not.toHaveBeenCalled()
})
