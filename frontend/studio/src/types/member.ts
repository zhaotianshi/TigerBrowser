import type { Role } from './api'
import type { ProfilePermission } from './profile'

export interface Member {
  id: string
  name: string
  display: string
  role: Role
  active: boolean
  permission_version: number
  grants?: Record<string, ProfilePermission[]>
}

export interface Capabilities {
  browser_mode: 'disabled' | 'headless' | 'desktop' | 'remote'
  remote_display: boolean
  control_handover: boolean
  file_transfer: boolean
  launch_modes: string[]
  team_automation: boolean
  online_maintenance: boolean
  secure_transport: boolean
  private_network_only: boolean
  client_allowlist: boolean
  key_codes: string[]
}

export interface SessionInfo {
  member: Member
  expires_at: string
  csrf: string
  // Stable for one server; the desktop client keys its local cache by it.
  workspace_id: string
  capabilities: Capabilities
}

export interface MemberGroup {
  id: string
  name: string
  revision: number
  members: string[]
  grants: Record<string, ProfilePermission[]>
  created_at?: string
  updated_at?: string
}

export interface AuditEvent {
  id?: string
  action: string
  actor?: string
  profile_id?: string
  detail: any
  created_at: string
}

export interface AdminData {
  members: Member[]
  groups: MemberGroup[]
  audit: AuditEvent[]
}
