import { api } from './client'
import type {
  ProfileSummary,
  ProfileDetail,
  CreateProfileInput,
  BatchProfileItem,
  BatchProfileResult,
} from '../types/profile'
import type { CommandItem, TaskItem } from '../types/health'
import type { CheckoutLease } from '../desktop/bridge'

export async function apiListProfiles(): Promise<ProfileSummary[]> {
  const result = await api.get<{ profiles: ProfileSummary[] }>('/profiles')
  return result.profiles
}

export async function apiGetProfile(id: string): Promise<ProfileDetail> {
  return api.get<ProfileDetail>(`/profiles/${id}`)
}

export async function apiCreateProfile(input: CreateProfileInput): Promise<ProfileSummary> {
  // POST /profiles returns profile metadata (id, name, group_name, tags, os, archived, revision)
  return api.post<ProfileSummary>('/profiles', input)
}

export async function apiUpdateProfile(
  id: string,
  data: Partial<CreateProfileInput> & { archived?: boolean },
  revision: number,
): Promise<ProfileSummary> {
  return api.post<ProfileSummary>(`/profiles/${id}`, {
    data,
    revision,
  })
}

export async function apiCloneProfile(id: string, revision: number): Promise<ProfileSummary> {
  return api.post<ProfileSummary>(`/profiles/${id}/clone`, { revision })
}

export async function apiDeleteProfile(
  id: string,
  requestId: string,
  revision: number,
  removeData: boolean,
): Promise<TaskItem> {
  return api.post<TaskItem>(`/profiles/${id}/delete`, {
    request_id: requestId,
    revision,
    remove_data: removeData,
  })
}

export async function apiStartProfile(
  id: string,
  requestId: string,
  mode?: string,
): Promise<CommandItem> {
  const payload: any = { request_id: requestId }
  if (mode) payload.mode = mode
  return api.post<CommandItem>(`/profiles/${id}/start`, payload)
}

export async function apiStopProfile(
  id: string,
  requestId: string,
  runId: string,
): Promise<CommandItem> {
  return api.post<CommandItem>(`/profiles/${id}/stop`, {
    request_id: requestId,
    run_id: runId,
  })
}

export async function apiCheckoutProfile(
  id: string,
  request: { request_id: string; device_id: string; device_name: string; platform: string },
): Promise<CheckoutLease> {
  return api.post<CheckoutLease>(`/profiles/${id}/checkout`, request)
}

export async function apiCancelCheckout(checkoutId: string, token: string): Promise<unknown> {
  return api.request(`/checkouts/${checkoutId}/cancel`, {
    method: 'POST',
    body: {},
    headers: { Authorization: `Bearer ${token}` },
  })
}

export async function apiReleaseCheckout(id: string, checkoutId: string): Promise<unknown> {
  return api.post(`/profiles/${id}/release`, { checkout_id: checkoutId })
}

export async function apiBatchProfiles(
  action: 'start' | 'headless_start' | 'stop' | 'archive' | 'restore',
  items: BatchProfileItem[],
): Promise<BatchProfileResult[]> {
  const result = await api.post<{ items: BatchProfileResult[] }>('/profiles/batch', {
    action,
    items,
  })
  return result.items
}

export async function apiGetIdentity(id: string): Promise<any> {
  return api.get<any>(`/profiles/${id}/identity`)
}

export async function apiRebuildIdentity(id: string, revision: number): Promise<any> {
  return api.post<any>(`/profiles/${id}/identity`, {
    action: 'rebuild',
    revision,
  })
}

export async function apiGetCookies(id: string): Promise<any[]> {
  const result = await api.get<{ cookies: any[] }>(`/profiles/${id}/cookies`)
  return result.cookies
}

export async function apiSetCookies(id: string, cookies: any[]): Promise<any> {
  return api.post<any>(`/profiles/${id}/cookies`, { cookies })
}
