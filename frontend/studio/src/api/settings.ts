import { api } from './client'
import type {
  AvailableKernel, CachedKernel, KernelInventory, SettingsData, TaskItem, TimezoneCatalog,
} from '../types/health'

export async function apiGetTimezones(): Promise<TimezoneCatalog> {
  return api.get<TimezoneCatalog>('/timezones')
}

export async function apiGetSettings(): Promise<SettingsData> {
  return api.get<SettingsData>('/settings')
}

export async function apiSaveSettings(data: Partial<SettingsData>): Promise<SettingsData> {
  return api.post<SettingsData>('/settings', data)
}

export async function apiGetConfiguration(): Promise<any> {
  return api.get<any>('/configuration')
}

export async function apiImportConfiguration(requestId: string, configuration: any): Promise<TaskItem> {
  return api.post<TaskItem>('/configuration/import', {
    request_id: requestId,
    configuration,
  })
}

export async function apiGetKernels(): Promise<KernelInventory> {
  const result = await api.get<{ camoufox: any[]; cache: CachedKernel[] | null }>('/kernels')
  return {
    installed: result.camoufox.map((k) => ({
      id: k.id,
      kernel_id: k.kernel_id,
      version: `${k.version}-${k.build}`,
      digest: k.sha256,
      active: k.active,
    })),
    cache: result.cache,
  }
}

export async function apiKernelCatalog(requestId: string): Promise<TaskItem<{ kernels: AvailableKernel[] }>> {
  return api.post<TaskItem<{ kernels: AvailableKernel[] }>>('/kernels/catalog', { request_id: requestId })
}

export async function apiCacheKernel(requestId: string, kernelId: string): Promise<TaskItem> {
  return api.post<TaskItem>('/kernels/cache', { request_id: requestId, kernel_id: kernelId })
}

export async function apiRemoveKernel(requestId: string, kernelId: string): Promise<TaskItem> {
  return api.post<TaskItem>('/kernels/remove', { request_id: requestId, kernel_id: kernelId })
}

export async function apiActivateKernel(requestId: string, id: string): Promise<TaskItem> {
  return api.post<TaskItem>('/kernels/activate', {
    request_id: requestId,
    id,
  })
}
