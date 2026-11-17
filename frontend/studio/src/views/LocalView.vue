<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { desktop, type AutomationStatus, type LocalSettings, type LocalState, type StaleCopy } from '../desktop/bridge'
import { useDesktopStore } from '../stores/desktop'
import { useNotificationStore } from '../stores/notification'
import { useProfilesStore } from '../stores/profiles'
import Button from '../components/common/Button.vue'
import { ERROR_MESSAGES } from '../types/api'
import { FolderOpen, Laptop, RefreshCw, Save, XCircle } from 'lucide-vue-next'

const desktopStore = useDesktopStore()
const profilesStore = useProfilesStore()
const notification = useNotificationStore()

const settings = ref<LocalSettings | null>(null)
const pending = ref<Record<string, LocalState>>({})
const stale = ref<StaleCopy[]>([])
const automation = ref<AutomationStatus | null>(null)
const loading = ref(false)
const saving = ref(false)

function profileName(profileId: string): string {
  return profilesStore.profiles.find(item => item.id === profileId)?.name || profileId
}

const LEASE_TEXT: Record<string, string> = {
  syncing: '下载未完成',
  ready: '已签出到本机',
  pending_return: '等待签回',
  revoked: '已被强制解锁',
}

async function load() {
  loading.value = true
  try {
    const [current, waiting, status] = await Promise.all([
      desktop.settings(), desktop.pending(), desktop.automation()])
    settings.value = current
    automation.value = status
    pending.value = waiting.profiles
    stale.value = waiting.stale
  } catch (cause: any) {
    notification.error(cause.message)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!settings.value) return
  saving.value = true
  try {
    settings.value = await desktop.saveSettings({
      max_running: Number(settings.value.max_running),
      automation_enabled: settings.value.automation_enabled,
    })
    notification.success('本机设置已保存，只对本电脑之后启动的环境生效。')
    // The bridge restarts automation asynchronously after saving.
    setTimeout(() => { void desktop.automation().then(status => { automation.value = status }) }, 1000)
  } catch (cause: any) {
    notification.error(cause.message)
  } finally {
    saving.value = false
  }
}

async function cancel(profileId: string) {
  if (!window.confirm('放弃这次签出？本机从未运行过此环境，服务器上的数据保持不变。')) return
  try {
    await desktopStore.cancel(profileId)
    await load()
  } catch (cause: any) {
    notification.error(cause.message)
  }
}

async function openFolder(profileId: string) {
  try {
    await desktopStore.openFolder(profileId)
  } catch (cause: any) {
    notification.error(cause.message)
  }
}

onMounted(load)
</script>

<template>
  <div class="local-view">
    <section class="settings-card">
      <header class="card-header">
        <Laptop :size="18" />
        <h2 class="card-title">本机设置</h2>
      </header>
      <p class="hint">这些设置只保存在本电脑上该服务器的本机缓存中，不上传服务器。</p>
      <div v-if="settings" class="form-grid">
        <label class="form-label" for="local-max-running">本机最多同时运行</label>
        <input id="local-max-running" v-model.number="settings.max_running" type="number" min="1" max="50" />
        <label class="form-label" for="local-automation">本地自动化接口</label>
        <label class="toggle">
          <input id="local-automation" v-model="settings.automation_enabled" type="checkbox" />
          <span>只监听 127.0.0.1，按当前登录成员的"本机运行"授权</span>
        </label>
      </div>
      <p v-if="automation?.enabled" class="hint">连接文件：<code>{{ automation.connection_file }}</code></p>
      <p v-else-if="automation?.error" class="error">
        {{ ERROR_MESSAGES[automation.error] || automation.error }}
      </p>
      <Button variant="primary" size="compact" :disabled="!settings" :loading="saving" @click="save">
        <Save :size="14" />
        <span>保存本机设置</span>
      </Button>
    </section>

    <section class="settings-card">
      <header class="card-header">
        <RefreshCw :size="18" />
        <h2 class="card-title">待同步的环境</h2>
        <Button variant="ghost" size="compact" :loading="loading" @click="load">刷新</Button>
      </header>
      <p v-if="Object.keys(pending).length === 0" class="hint">没有等待下载或签回的环境。</p>
      <ul v-else class="item-list">
        <li v-for="(state, profileId) in pending" :key="profileId" class="item">
          <div>
            <strong>{{ profileName(String(profileId)) }}</strong>
            <span class="state">{{ LEASE_TEXT[state.lease || ''] || state.lease }}</span>
            <div v-if="state.sync_error" class="error">{{ ERROR_MESSAGES[state.sync_error] || state.sync_error }}</div>
          </div>
          <div class="item-actions">
            <Button variant="ghost" size="compact" @click="openFolder(String(profileId))">
              <FolderOpen :size="14" />
              <span>打开数据目录</span>
            </Button>
            <Button v-if="state.lease === 'syncing'" variant="ghost" size="compact" @click="cancel(String(profileId))">
              <XCircle :size="14" />
              <span>放弃签出</span>
            </Button>
          </div>
        </li>
      </ul>
    </section>

    <section v-if="stale.length" class="settings-card">
      <header class="card-header">
        <XCircle :size="18" />
        <h2 class="card-title">失效副本</h2>
      </header>
      <p class="hint">管理员强制解锁后，本机未签回的数据保留在本机缓存的 stale 目录中，不能再签回或启动。需要时请联系管理员。</p>
      <ul class="item-list">
        <li v-for="copy in stale" :key="copy.checkout_id" class="item">
          <strong>{{ profileName(copy.profile_id) }}</strong>
          <span class="state">{{ copy.revoked_at }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.local-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px;
  overflow: auto;
  height: 100%;
}

.settings-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-title {
  font-size: 15px;
  margin: 0;
  flex: 1;
}

.hint {
  color: var(--color-muted);
  font-size: 12px;
  margin: 0;
}

.form-grid {
  display: grid;
  grid-template-columns: 180px minmax(0, 320px);
  gap: 10px 16px;
  align-items: center;
}

.form-label {
  font-size: 13px;
  color: var(--color-muted);
}

.toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-muted);
}

.item-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--color-border-subtle);
  padding-bottom: 8px;
}

.item-actions {
  display: flex;
  gap: 8px;
}

.state {
  margin-left: 10px;
  color: var(--color-muted);
  font-size: 12px;
}

.error {
  color: var(--color-danger);
  font-size: 12px;
  margin-top: 4px;
}
</style>
