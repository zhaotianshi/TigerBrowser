import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiGetMe, apiLogin, apiLogout, apiChangePassword } from '../api/auth'
import { api } from '../api/client'
import type { SessionInfo, Member, Capabilities } from '../types/member'

export const useAuthStore = defineStore('auth', () => {
  const session = ref<SessionInfo | null>(null)
  const loading = ref(false)
  const initialized = ref(false)
  let generation = 0

  const member = computed<Member | null>(() => session.value?.member ?? null)
  const capabilities = computed<Capabilities | null>(() => session.value?.capabilities ?? null)
  const isAuthenticated = computed(() => !!session.value)
  const role = computed(() => session.value?.member.role)
  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'operator' || isAdmin.value)
  const isViewer = computed(() => !!session.value)

  function invalidate() {
    generation++
    session.value = null
    initialized.value = true
    api.setCsrf('')
    loading.value = false
  }

  api.onUnauthorized(invalidate)

  async function checkSession(): Promise<boolean> {
    const current = ++generation
    loading.value = true
    try {
      const me = await apiGetMe()
      if (current !== generation) return false
      session.value = me
      api.setCsrf(me.csrf)
      initialized.value = true
      return true
    } catch {
      // A transport outage cannot revoke a session; HTTP 401 invalidates it above.
      if (current === generation) initialized.value = true
      return false
    } finally {
      if (current === generation) loading.value = false
    }
  }

  async function login(name: string, pass: string): Promise<void> {
    invalidate()
    const current = generation
    loading.value = true
    try {
      const login = await apiLogin(name, pass)
      if (current !== generation) return
      api.setCsrf(login.csrf)
      const me = await apiGetMe()
      if (current !== generation) return
      session.value = me
      api.setCsrf(me.csrf)
      initialized.value = true
    } finally {
      if (current === generation) loading.value = false
    }
  }

  async function logout(): Promise<void> {
    const current = ++generation
    try {
      await apiLogout()
    } finally {
      if (current === generation) invalidate()
    }
  }

  async function changePassword(current: string, next: string): Promise<void> {
    const attempt = ++generation
    await apiChangePassword(current, next)
    if (attempt === generation) invalidate()
  }

  return {
    session,
    member,
    capabilities,
    loading,
    initialized,
    isAuthenticated,
    role,
    isAdmin,
    isOperator,
    isViewer,
    checkSession,
    login,
    logout,
    changePassword,
  }
})
