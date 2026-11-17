<script setup lang="ts">
import { ref, watch } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import Badge from '../common/Badge.vue'
import { useAuthStore } from '../../stores/auth'
import { useNotificationStore } from '../../stores/notification'
import { apiGetProfile, apiGetIdentity, apiRebuildIdentity } from '../../api/profiles'
import type { ProfileDetail } from '../../types/profile'
import { RefreshCw } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
  profileId: string | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'rebuilt'): void
}>()

const authStore = useAuthStore()
const notification = useNotificationStore()

const detail = ref<ProfileDetail | null>(null)
const identityData = ref<any>(null)
const loading = ref(false)
const rebuilding = ref(false)
const activeTab = ref<'meta' | 'identity'>('meta')

watch(
  () => props.modelValue,
  async (open) => {
    if (!open || !props.profileId) return
    loading.value = true
    activeTab.value = 'meta'
    try {
      detail.value = await apiGetProfile(props.profileId)
      if (authStore.isAdmin) {
        try {
          identityData.value = await apiGetIdentity(props.profileId)
        } catch {
          identityData.value = null
        }
      }
    } catch (err: any) {
      notification.error(err.message || '加载环境详情失败')
    } finally {
      loading.value = false
    }
  },
)

async function onRebuildIdentity() {
  if (!detail.value || !authStore.isAdmin) return
  if (detail.value.run?.state === 'running' || detail.value.run?.state === 'starting') {
    notification.warning('请先停止环境，再重建指纹。')
    return
  }

  const confirmed = confirm(
    '确定要显式重建该环境的指纹身份？\n下次启动将生成一套全新的指纹组合；已保存的 Cookie、历史和网站数据将完整保留。',
  )
  if (!confirmed) return

  rebuilding.value = true
  try {
    await apiRebuildIdentity(detail.value.id, detail.value.revision)
    notification.success('指纹已成功重建')
    identityData.value = await apiGetIdentity(detail.value.id)
    detail.value = await apiGetProfile(detail.value.id)
    emit('rebuilt')
  } catch (err: any) {
    notification.error(err.message || '重建指纹失败')
  } finally {
    rebuilding.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="环境详情与指纹检查"
    width="680px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-if="detail" class="details-layout">
      <!-- Tabs Nav -->
      <div class="tab-nav">
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'meta' }"
          @click="activeTab = 'meta'"
        >
          基本与运行状态
        </button>
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'identity' }"
          @click="activeTab = 'identity'"
        >
          持久指纹身份
        </button>
      </div>

      <!-- Tab 1: Meta & Runtime -->
      <div v-show="activeTab === 'meta'" class="meta-section">
        <div class="meta-grid">
          <div class="meta-item">
            <span class="meta-label">环境名称</span>
            <span class="meta-val font-bold">{{ detail.name }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">运行状态</span>
            <span class="meta-val">
              <Badge :status="detail.run?.state || 'stopped'" />
            </span>
          </div>

          <div class="meta-item">
            <span class="meta-label">环境 ID</span>
            <span class="meta-val font-mono">{{ detail.id }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">修订版本 (Revision)</span>
            <span class="meta-val font-mono">v{{ detail.revision }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">操作系统 / 语言</span>
            <span class="meta-val">{{ detail.os }} / {{ detail.locale }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">时区</span>
            <span class="meta-val">{{ detail.timezone || '自动 (跟随代理出口)' }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">当前运行 ID</span>
            <span class="meta-val font-mono">{{ detail.run?.id || '无活动运行' }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">最后启动时间</span>
            <span class="meta-val">{{ detail.last_launch_at || '尚未启动' }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">创建时间</span>
            <span class="meta-val">{{ detail.created_at }}</span>
          </div>

          <div class="meta-item">
            <span class="meta-label">更新时间</span>
            <span class="meta-val">{{ detail.updated_at }}</span>
          </div>
        </div>

        <div v-if="detail.notes" class="notes-box">
          <span class="notes-label">备注备忘：</span>
          <p class="notes-content">{{ detail.notes }}</p>
        </div>
      </div>

      <!-- Tab 2: Identity -->
      <div v-show="activeTab === 'identity'" class="identity-section">
        <div class="identity-header">
          <div>
            <h4 class="identity-title">持久指纹参数</h4>
            <p class="identity-sub">
              {{ detail.identity_present ? '已固定生成专属设备指纹' : '尚未启动，指纹将在首次启动时确定' }}
            </p>
          </div>

          <Button
            v-if="authStore.isAdmin"
            variant="default"
            size="compact"
            :loading="rebuilding"
            :disabled="detail.run?.state === 'running'"
            title="显式重建身份，保留网站数据"
            @click="onRebuildIdentity"
          >
            <RefreshCw :size="13" />
            <span>重建指纹</span>
          </Button>
        </div>

        <div v-if="identityData" class="identity-raw-box">
          <div class="identity-info-grid">
            <div class="info-row">
              <span class="info-k">浏览器版本:</span>
              <span class="info-v">{{ identityData.browser_version || '未记录' }}</span>
            </div>
            <div class="info-row">
              <span class="info-k">屏幕分辨率:</span>
              <span class="info-v">{{ identityData.config?.screen?.width }} × {{ identityData.config?.screen?.height }}</span>
            </div>
            <div class="info-row">
              <span class="info-k">User Agent:</span>
              <span class="info-v font-mono">{{ identityData.config?.navigator?.userAgent || '跟随内核' }}</span>
            </div>
            <div class="info-row">
              <span class="info-k">硬件并发:</span>
              <span class="info-v">{{ identityData.config?.navigator?.hardwareConcurrency || '8' }} 核</span>
            </div>
          </div>

          <div class="code-view">
            <pre><code>{{ JSON.stringify(identityData, null, 2) }}</code></pre>
          </div>
        </div>

        <div v-else class="empty-identity">
          <p>暂无持久指纹数据或当前角色不可读取指纹详情。</p>
        </div>
      </div>
    </div>

    <template #footer>
      <Button variant="primary" type="button" @click="emit('update:modelValue', false)">
        确定
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.details-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tab-nav {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 8px;
}

.tab-btn {
  background: transparent;
  border: none;
  color: var(--color-muted);
  font-size: 13px;
  font-weight: 500;
  padding: 8px 14px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.tab-btn.active {
  color: var(--color-accent);
  background-color: rgba(91, 226, 196, 0.1);
  font-weight: 600;
}

.meta-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.meta-item {
  background-color: var(--color-bg);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.meta-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-subtle);
  text-transform: uppercase;
}

.meta-val {
  font-size: 13px;
  color: var(--color-text);
  word-break: break-all;
}

.font-mono {
  font-family: monospace;
  font-size: 12px;
}
.font-bold {
  font-weight: 600;
}

.notes-box {
  margin-top: 12px;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
}
.notes-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-subtle);
}
.notes-content {
  font-size: 13px;
  color: var(--color-text);
  margin-top: 4px;
  white-space: pre-wrap;
}

.identity-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.identity-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
}

.identity-sub {
  font-size: 12px;
  color: var(--color-subtle);
  margin-top: 2px;
}

.identity-info-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 12px;
}

.info-row {
  display: flex;
  font-size: 12px;
  gap: 8px;
}
.info-k {
  color: var(--color-muted);
  width: 90px;
}
.info-v {
  color: var(--color-text);
}

.code-view {
  max-height: 220px;
  overflow: auto;
  background-color: #080d16;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 12px;
}
.code-view pre {
  font-family: monospace;
  font-size: 11px;
  color: #a5d6ff;
}

.empty-identity {
  padding: 32px;
  text-align: center;
  color: var(--color-subtle);
}
</style>
