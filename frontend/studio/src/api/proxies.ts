import { api } from './client'
import type { ProxyItem, CreateProxyInput } from '../types/proxy'
import type { TaskItem } from '../types/health'

export async function apiParseProxy(link: string): Promise<CreateProxyInput> {
  const result = await api.post<{ proxy: CreateProxyInput }>('/proxies/parse', { link })
  return result.proxy
}

export async function apiListProxies(): Promise<ProxyItem[]> {
  const result = await api.get<{ proxies: ProxyItem[] }>('/proxies')
  return result.proxies
}

export async function apiCreateProxy(input: CreateProxyInput): Promise<void> {
  // POST /proxies returns HTTP 201 with no response body
  await api.post<undefined>('/proxies', input)
}

export async function apiUpdateProxy(id: string, input: CreateProxyInput): Promise<ProxyItem> {
  return api.post<ProxyItem>(`/proxies/${id}`, input)
}

export async function apiDeleteProxy(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/proxies/${id}/delete`, {})
}

export async function apiImportProxies(requestId: string, text: string): Promise<TaskItem> {
  return api.post<TaskItem>('/proxies/import', {
    request_id: requestId,
    text,
  })
}

export async function apiProbeProxy(id: string, requestId: string): Promise<TaskItem> {
  return api.post<TaskItem>(`/proxies/${id}/probe`, {
    request_id: requestId,
  })
}
