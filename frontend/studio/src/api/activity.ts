import { api } from './client'
import type { ActivityItem } from '../types/health'

export async function apiGetActivity(): Promise<ActivityItem[]> {
  const result = await api.get<{ events: ActivityItem[] }>('/activity')
  return result.events
}
