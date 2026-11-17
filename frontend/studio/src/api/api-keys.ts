import { api } from './client'

export interface AutomationKey {
  id: string
  name: string
  member_id: string
  member_name: string
  profile_ids: string[] | null
  allowed_ips: string[]
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
}

export interface AutomationConnection {
  protocolVersion: 1
  baseURL: string
  token: string
}

export const apiListKeys = () => api.get<{ keys: AutomationKey[]; enabled: boolean }>('/api-keys')
export const apiCreateKey = (input: {
  name: string; profile_ids: string[] | null; allowed_ips: string[]; expires_at: string | null
}) => api.post<{ key: AutomationKey; connection: AutomationConnection }>('/api-keys', input)
export const apiRevokeKey = (id: string) => api.post(`/api-keys/${id}/revoke`)
