<script setup lang="ts">
import { computed, ref } from 'vue'
import { useProfilesStore } from '../../stores/profiles'
import { useAuthStore } from '../../stores/auth'
import { useNotificationStore } from '../../stores/notification'
import Button from '../common/Button.vue'
import {
  Play,
  Square,
  Edit,
  Copy,
  Archive,
  ArchiveRestore,
  Trash2,
  Info,
  ExternalLink,
  Cookie,
  EyeOff,
} from 'lucide-vue-next'
import {
  apiCloneProfile,
  apiUpdateProfile,
  apiBatchProfiles,
  apiReleaseCheckout,
} from '../../api/profiles'
import { useDesktopStore } from '../../stores/desktop'
import { Laptop, Unlock } from 'lucide-vue-next'
import { generateRequestId } from '../../types/api'
import { openProfileWindow } from '../../utils/profile-window'

const props = defineProps<{
  onEdit: () => void
  onDetails: () => void
  onDelete: () => void
  onCookies: () => void
}>()

const profilesStore = useProfilesStore()
const authStore = useAuthStore()
const notification = useNotificationStore()
const desktopStore = useDesktopStore()
const starting = ref(false)

const selected = computed(() => profilesStore.selectedProfiles)
const single = computed(() => profilesStore.singleSelectedProfile)
const count = computed(() => profilesStore.selectedCount)

// Permissions & State Checks
const isArchivedView = computed(() => profilesStore.showArchived)
const canStart = computed(() => count.value > 0 && selected.value.every((p) => !profilesStore.startDisabledReason(p)))
const canHeadless = computed(() => count.value > 0 && selected.value.every((p) => !profilesStore.startDisabledReason(p, true)))
const pending = computed(() => selected.value.some((p) => profilesStore.pendingActions[p.id]))
const canStop = computed(() => (
  !pending.value
  && selected.value.every((p) => authStore.isAdmin || p.permissions.includes('operate'))
  && selected.value.some((p) => !profilesStore.stopDisabledReason(p))
))

const allStopped = computed(() => {
  if (count.value === 0) return false
  return selected.value.every(
    (p) => (!p.run || p.run.state === 'stopped' || p.run.state === 'error') && !p.checked_out,
  )
})

// Local runs exist only in the desktop client and need the separate "本机运行" grant.
const localState = computed(() => (single.value ? desktopStore.state(single.value.id) : undefined))
const localRunning = computed(() => ['starting', 'running', 'stopping'].includes(localState.value?.browser || ''))
const canLaunchLocal = computed(() => {
  const profile = single.value
  if (!desktopStore.available || !profile || profile.archived || localRunning.value) return false
  if (!authStore.isAdmin && !profile.permissions.includes('checkout')) return false
  if (profile.run && !['stopped', 'error'].includes(profile.run.state)) return false
  return !profile.checked_out || (profile.checked_out.mine && !!localState.value?.lease)
})
const canRelease = computed(() => authStore.isAdmin && !!single.value?.checked_out && !localState.value?.lease)

async function onLaunchLocal() {
  if (!single.value || !canLaunchLocal.value || starting.value) return
  starting.value = true
  try {
    await desktopStore.launch(single.value)
    notification.success(`环境 "${single.value.name}" 正在本机准备启动`)
    await profilesStore.fetchProfiles()
  } catch (err: any) {
    notification.error(err.message)
  } finally {
    starting.value = false
  }
}

async function onStopLocal() {
  if (!single.value || !localRunning.value) return
  try {
    await desktopStore.stop(single.value.id)
    notification.success('本机浏览器正在关闭，关闭后自动签回')
  } catch (err: any) {
    notification.error(err.message)
  }
}

async function onRelease() {
  const profile = single.value
  if (!profile?.checked_out || !canRelease.value) return
  const holder = `${profile.checked_out.member_name} 的 ${profile.checked_out.device_name}`
  if (!window.confirm(`强制解锁会让 ${holder} 上未签回的数据作废，服务器保留最后一次签回的数据。确定继续？`)) return
  try {
    await apiReleaseCheckout(profile.id, profile.checked_out.id)
    notification.success('已强制解锁，原设备上的副本已停用')
    await profilesStore.fetchProfiles()
  } catch (err: any) {
    notification.error(err.message)
  }
}

async function onClone() {
  if (!single.value || !authStore.isAdmin || !allStopped.value) return
  try {
    await apiCloneProfile(single.value.id, single.value.revision)
    notification.success(`环境已复制，新环境将生成独立指纹`)
    await profilesStore.fetchProfiles()
  } catch (err: any) {
    notification.error(err.message)
  }
}

async function onToggleArchive() {
  if (!authStore.isAdmin || !allStopped.value || count.value === 0) return
  const newArchived = !isArchivedView.value

  try {
    if (count.value === 1 && single.value) {
      await apiUpdateProfile(
        single.value.id,
        { archived: newArchived },
        single.value.revision,
      )
      notification.success(newArchived ? '环境已归档' : '环境已恢复')
    } else {
      const items = selected.value.map((p) => ({
        profile_id: p.id,
        request_id: generateRequestId(),
        revision: p.revision,
      }))
      profilesStore.reportBatch(await apiBatchProfiles(newArchived ? 'archive' : 'restore', items), selected.value, newArchived ? '归档' : '恢复')
    }
    await profilesStore.fetchProfiles()
  } catch (err: any) {
    notification.error(err.message)
  }
}

function onOpenRemote() {
  if (!single.value) return
  openProfileWindow(single.value.id)
}
</script>

<template>
  <div class="profile-toolbar" :class="{ 'has-selection': count > 0 }" aria-label="环境管理操作">
    <div class="toolbar-left">
      <span class="selection-indicator">
        {{ count > 0 ? `已选择 ${count} 个环境` : '勾选环境后管理' }}
      </span>
      <Button v-if="count > 0" variant="ghost" size="compact" @click="profilesStore.clearSelection()">取消选择</Button>

      <div class="action-buttons">
        <!-- Start -->
        <Button
          v-if="count > 1"
          variant="primary"
          size="compact"
          :disabled="!canStart || starting"
          title="启动所选环境"
          :loading="selected.some((p) => profilesStore.pendingActions[p.id] === 'start')"
          @click="profilesStore.startProfiles(selected)"
        >
          <Play :size="14" />
          <span>批量启动</span>
        </Button>

        <!-- Headless Start -->
        <Button
          v-if="count > 1"
          variant="default"
          size="compact"
          :disabled="!canHeadless || starting"
          title="在后台无头启动（不开启远程图形）"
          :loading="selected.some((p) => profilesStore.pendingActions[p.id] === 'headless_start')"
          @click="profilesStore.startProfiles(selected, true)"
        >
          <EyeOff :size="14" />
          <span>批量无头启动</span>
        </Button>

        <!-- Stop -->
        <Button
          v-if="count > 1"
          variant="default"
          size="compact"
          :disabled="!canStop || starting"
          title="停止所选运行中的环境"
          :loading="selected.some((p) => profilesStore.pendingActions[p.id] === 'stop')"
          @click="profilesStore.stopProfiles(selected)"
        >
          <Square :size="14" />
          <span>批量停止</span>
        </Button>

        <template v-if="desktopStore.available">
          <div class="toolbar-divider" />
          <Button
            variant="primary"
            size="compact"
            :disabled="!canLaunchLocal"
            title="在本电脑上运行此环境；关闭浏览器后自动签回服务器"
            :loading="starting"
            @click="onLaunchLocal"
          >
            <Laptop :size="14" />
            <span>本机启动</span>
          </Button>
          <Button
            variant="default"
            size="compact"
            :disabled="!localRunning"
            title="关闭本机浏览器并签回"
            @click="onStopLocal"
          >
            <Square :size="14" />
            <span>停止本机</span>
          </Button>
        </template>

        <Button
          v-if="canRelease"
          variant="danger"
          size="compact"
          title="环境所在设备丢失或长期失联时，由管理员强制结束本机运行"
          @click="onRelease"
        >
          <Unlock :size="14" />
          <span>强制解锁</span>
        </Button>

        <div v-if="count > 1 || desktopStore.available || canRelease" class="toolbar-divider" />

        <!-- Open Remote Window -->
        <Button
          variant="default"
          size="compact"
          :disabled="count !== 1 || isArchivedView"
          title="在独立窗口中打开原生浏览器操作画面"
          @click="onOpenRemote"
        >
          <ExternalLink :size="14" />
          <span>远程窗口</span>
        </Button>

        <!-- Edit -->
        <Button
          variant="default"
          size="compact"
          :disabled="count !== 1 || !authStore.isAdmin || !allStopped || isArchivedView"
          title="编辑环境配置（仅管理员，停止状态可用）"
          @click="props.onEdit"
        >
          <Edit :size="14" />
          <span>编辑</span>
        </Button>

        <!-- Clone -->
        <Button
          variant="default"
          size="compact"
          :disabled="count !== 1 || !authStore.isAdmin || !allStopped || isArchivedView"
          title="复制环境配置"
          @click="onClone"
        >
          <Copy :size="14" />
          <span>复制</span>
        </Button>

        <!-- Archive / Restore -->
        <Button
          variant="default"
          size="compact"
          :disabled="count === 0 || !authStore.isAdmin || !allStopped"
          :title="isArchivedView ? '恢复所选环境' : '归档所选环境'"
          @click="onToggleArchive"
        >
          <component :is="isArchivedView ? ArchiveRestore : Archive" :size="14" />
          <span>{{ isArchivedView ? '恢复' : '归档' }}</span>
        </Button>

        <!-- Delete -->
        <Button
          variant="danger"
          size="compact"
          :disabled="count === 0 || !authStore.isAdmin || !allStopped"
          title="永久删除所选环境"
          @click="props.onDelete"
        >
          <Trash2 :size="14" />
          <span>删除</span>
        </Button>

        <div class="toolbar-divider" />

        <!-- Details -->
        <Button
          variant="ghost"
          size="compact"
          :disabled="count !== 1"
          title="查看环境指纹及完整元数据"
          @click="props.onDetails"
        >
          <Info :size="14" />
          <span>详情</span>
        </Button>

        <!-- Cookies -->
        <Button
          v-if="authStore.isAdmin"
          variant="ghost"
          size="compact"
          :disabled="count !== 1 || single?.run?.state !== 'running'"
          title="导入或导出当前运行中浏览器的 Cookie"
          @click="props.onCookies"
        >
          <Cookie :size="14" />
          <span>Cookie</span>
        </Button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.profile-toolbar {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  user-select: none;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.selection-indicator {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-muted);
  min-width: 90px;
}

.action-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.toolbar-divider {
  width: 1px;
  height: 18px;
  background-color: var(--color-border);
  margin: 0 4px;
}

</style>
