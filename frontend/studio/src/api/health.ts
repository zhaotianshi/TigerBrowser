import { api } from './client'
import type { HealthData, CommandItem, TaskItem } from '../types/health'

export async function apiGetHealth(): Promise<HealthData> {
  return api.get<HealthData>('/health')
}

export async function apiGetCommand(commandId: string): Promise<CommandItem> {
  return api.get<CommandItem>(`/commands/${commandId}`)
}

export async function apiGetTask<Result = any>(taskId: string): Promise<TaskItem<Result>> {
  return api.get<TaskItem<Result>>(`/tasks/${taskId}`)
}
