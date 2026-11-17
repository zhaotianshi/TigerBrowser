import { api } from './client'
import type { SessionInfo } from '../types/member'

export async function apiLogin(name: string, password: string): Promise<{
  member: any
  expires_at: string
  csrf: string
}> {
  return api.post<{ member: any; expires_at: string; csrf: string }>('/login', {
    name,
    password,
  })
}

export async function apiGetMe(): Promise<SessionInfo> {
  return api.get<SessionInfo>('/me')
}

export async function apiLogout(): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/logout', {})
}

export async function apiChangePassword(current: string, password: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/password', { current, password })
}
