import { api } from './client'
import type { AdminData, Member, MemberGroup } from '../types/member'
import type { ProfilePermission } from '../types/profile'

export async function apiGetAdmin(): Promise<AdminData> {
  return api.get<AdminData>('/admin')
}

export async function apiCreateMember(data: {
  name: string
  display: string
  role: 'operator' | 'viewer'
  password: string
}): Promise<Member> {
  return api.post<Member>('/members', {
    action: 'create',
    ...data,
  })
}

export async function apiSetMemberActive(id: string, active: boolean): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/members', {
    action: 'active',
    id,
    active,
  })
}

export async function apiResetMemberPassword(id: string, password: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/members', {
    action: 'password',
    id,
    password,
  })
}

export async function apiGrantMember(
  id: string,
  profileId: string,
  permissions: ProfilePermission[],
): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/members', {
    action: 'grant',
    id,
    profile_id: profileId,
    permissions,
  })
}

export async function apiRevokeMember(id: string, profileId: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/members', {
    action: 'revoke',
    id,
    profile_id: profileId,
  })
}

export async function apiCreateGroup(name: string): Promise<MemberGroup> {
  return api.post<MemberGroup>('/groups', {
    action: 'create',
    name,
  })
}

export async function apiSetGroupMembers(
  id: string,
  revision: number,
  members: string[],
): Promise<MemberGroup> {
  return api.post<MemberGroup>('/groups', {
    action: 'members',
    id,
    revision,
    members,
  })
}

export async function apiSetGroupGrants(
  id: string,
  revision: number,
  changes: Record<string, ProfilePermission[]>,
): Promise<MemberGroup> {
  return api.post<MemberGroup>('/groups', {
    action: 'grants',
    id,
    revision,
    changes,
  })
}

export async function apiDeleteGroup(id: string, revision: number): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/groups', {
    action: 'delete',
    id,
    revision,
  })
}
