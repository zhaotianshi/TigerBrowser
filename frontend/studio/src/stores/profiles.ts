import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiListProfiles, apiStartProfile, apiStopProfile, apiBatchProfiles } from '../api/profiles'
import { useAuthStore } from './auth'
import { useNotificationStore } from './notification'
import { ERROR_MESSAGES, generateRequestId } from '../types/api'
import type { BatchProfileResult, ProfileSummary } from '../types/profile'

export const useProfilesStore = defineStore('profiles', () => {
  const profiles = ref<ProfileSummary[]>([])
  const loading = ref(false)
  const searchQuery = ref('')
  const groupFilter = ref<string | null>(null)
  const showArchived = ref(false)
  const selectedIds = ref<Set<string>>(new Set())
  const pendingActions = ref<Record<string, 'start' | 'headless_start' | 'stop'>>({})

  function canOperate(profile: ProfileSummary): boolean {
    return useAuthStore().isAdmin || profile.permissions.includes('operate')
  }

  function startDisabledReason(profile: ProfileSummary, headless = false): string {
    if (pendingActions.value[profile.id]) return '请求正在提交，请稍候'
    if (!canOperate(profile)) return '没有此环境的操作权限'
    if (profile.archived) return '请先恢复归档环境'
    if (profile.checked_out) return '环境已签出，请先在成员设备上关闭并签回'
    if (profile.run && !['stopped', 'error'].includes(profile.run.state)) return '请等待当前运行结束或完成恢复'
    const capabilities = useAuthStore().capabilities
    const mode = headless ? 'headless' : capabilities?.browser_mode
    if (!mode || !capabilities?.launch_modes.includes(mode)) return '服务器未启用此启动模式'
    return ''
  }

  function stopDisabledReason(profile: ProfileSummary): string {
    if (pendingActions.value[profile.id]) return '请求正在提交，请稍候'
    if (!canOperate(profile)) return '没有此环境的操作权限'
    if (profile.checked_out) return '请在持有环境的设备上停止本机浏览器'
    if (!profile.run?.id || !['running', 'starting'].includes(profile.run.state)) return '当前没有可停止的运行'
    return ''
  }

  function reportBatch(results: BatchProfileResult[], targets: ProfileSummary[], action: string) {
    const notification = useNotificationStore()
    const failed = results.filter((item) => item.error || item.command?.error || item.task?.error)
    const accepted = results.length - failed.length
    if (accepted) notification.success(`已提交 ${accepted} 个环境的${action}请求`)
    for (const item of failed) {
      const name = targets.find((profile) => profile.id === item.profile_id)?.name || '环境'
      const code = item.error || item.command?.error || item.task?.error || 'operation_failed'
      notification.error(`${name}：${ERROR_MESSAGES[code] || ERROR_MESSAGES.operation_failed}`)
    }
  }

  async function startProfiles(targets: ProfileSummary[], headless = false) {
    if (!targets.length || targets.some((profile) => startDisabledReason(profile, headless))) return
    await runAction([...targets], headless ? 'headless_start' : 'start')
  }

  async function stopProfiles(targets: ProfileSummary[]) {
    if (targets.some((profile) => !canOperate(profile) || pendingActions.value[profile.id])) return
    const active = targets.filter((profile) => !stopDisabledReason(profile))
    if (active.length) await runAction(active, 'stop')
  }

  async function runAction(targets: ProfileSummary[], action: 'start' | 'headless_start' | 'stop') {
    const notification = useNotificationStore()
    const label = action === 'stop' ? '停止' : '启动'
    const items = targets.map((profile) => ({
      profile_id: profile.id,
      request_id: generateRequestId(),
      ...(action === 'stop' ? { run_id: profile.run!.id } : {}),
    }))
    for (const profile of targets) pendingActions.value[profile.id] = action
    try {
      if (targets.length === 1) {
        const item = items[0]
        if (action === 'stop') {
          await apiStopProfile(item.profile_id, item.request_id, item.run_id!)
        } else {
          const mode = action === 'headless_start' ? 'headless' : useAuthStore().capabilities!.browser_mode
          await apiStartProfile(item.profile_id, item.request_id, mode)
        }
        notification.success(`环境 "${targets[0].name}" 已请求${label}`)
      } else {
        reportBatch(await apiBatchProfiles(action, items), targets, label)
      }
      await fetchProfiles()
    } catch (err: any) {
      notification.error(err.message)
    } finally {
      for (const profile of targets) delete pendingActions.value[profile.id]
    }
  }

  // Dynamic list of unique groups from existing profiles
  const availableGroups = computed(() => {
    const set = new Set<string>()
    for (const p of profiles.value) {
      if (p.group_name && p.group_name.trim()) {
        set.add(p.group_name.trim())
      }
    }
    return Array.from(set).sort()
  })

  // Filtered profiles based on search, group, and archive state
  const filteredProfiles = computed(() => {
    const q = searchQuery.value.trim().toLowerCase()
    const group = groupFilter.value
    const archived = showArchived.value

    return profiles.value.filter((p) => {
      if (p.archived !== archived) return false
      if (group && p.group_name !== group) return false

      if (q) {
        const text = [
          p.name,
          p.group_name || '',
          p.os || '',
          p.locale || '',
          p.tags?.join(' ') || '',
          p.proxy?.name || '',
          p.proxy?.server || '',
        ]
          .join(' ')
          .toLowerCase()
        if (!text.includes(q)) return false
      }
      return true
    })
  })

  // Metrics
  const totalCount = computed(() => profiles.value.filter((p) => !p.archived).length)
  const runningCount = computed(
    () =>
      profiles.value.filter(
        (p) => !p.archived && (p.run?.state === 'running' || p.run?.state === 'starting'),
      ).length,
  )
  const boundProxyCount = computed(
    () => profiles.value.filter((p) => !p.archived && !!p.proxy).length,
  )

  // Selected profiles
  const selectedProfiles = computed(() =>
    profiles.value.filter((p) => selectedIds.value.has(p.id)),
  )

  const selectedCount = computed(() => selectedIds.value.size)
  const singleSelectedProfile = computed(() =>
    selectedCount.value === 1 ? selectedProfiles.value[0] : null,
  )

  async function fetchProfiles() {
    loading.value = true
    try {
      const list = await apiListProfiles()
      setProfiles(list)
    } finally {
      loading.value = false
    }
  }

  function setProfiles(newProfiles: ProfileSummary[]) {
    profiles.value = newProfiles
    // Clean up selectedIds that no longer exist
    const currentIdSet = new Set(newProfiles.map((p) => p.id))
    const valid = new Set<string>()
    for (const id of selectedIds.value) {
      if (currentIdSet.has(id)) valid.add(id)
    }
    selectedIds.value = valid
  }

  function selectOne(id: string) {
    selectedIds.value = new Set([id])
  }

  function toggleSelect(id: string) {
    const next = new Set(selectedIds.value)
    if (next.has(id)) {
      next.delete(id)
    } else {
      next.add(id)
    }
    selectedIds.value = next
  }

  function selectRange(targetId: string, orderedList: ProfileSummary[]) {
    if (selectedIds.value.size === 0) {
      selectOne(targetId)
      return
    }

    const firstSelectedId = Array.from(selectedIds.value)[0]
    const idx1 = orderedList.findIndex((p) => p.id === firstSelectedId)
    const idx2 = orderedList.findIndex((p) => p.id === targetId)

    if (idx1 === -1 || idx2 === -1) {
      selectOne(targetId)
      return
    }

    const start = Math.min(idx1, idx2)
    const end = Math.max(idx1, idx2)
    const next = new Set<string>()
    for (let i = start; i <= end; i++) {
      next.add(orderedList[i].id)
    }
    selectedIds.value = next
  }

  function selectAll() {
    const next = new Set<string>()
    for (const p of filteredProfiles.value) {
      next.add(p.id)
    }
    selectedIds.value = next
  }

  function clearSelection() {
    selectedIds.value = new Set()
  }

  return {
    profiles,
    loading,
    searchQuery,
    groupFilter,
    showArchived,
    selectedIds,
    pendingActions,
    startDisabledReason,
    stopDisabledReason,
    startProfiles,
    stopProfiles,
    reportBatch,
    availableGroups,
    filteredProfiles,
    totalCount,
    runningCount,
    boundProxyCount,
    selectedProfiles,
    selectedCount,
    singleSelectedProfile,
    fetchProfiles,
    setProfiles,
    selectOne,
    toggleSelect,
    selectRange,
    selectAll,
    clearSelection,
  }
})
