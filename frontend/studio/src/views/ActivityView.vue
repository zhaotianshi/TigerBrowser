<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { apiGetActivity } from '../api/activity'
import { useProfilesStore } from '../stores/profiles'
import Button from '../components/common/Button.vue'
import { RefreshCw, Clock } from 'lucide-vue-next'
import type { ActivityItem } from '../types/health'

const profilesStore = useProfilesStore()

const activities = ref<ActivityItem[]>([])
const loading = ref(false)

const EVENT_NAMES: Record<string, string> = {
  'profile.created': '创建环境',
  'profile.updated': '更新环境',
  'profile.cloned': '复制环境',
  'profile.deleted': '删除环境',
  'profile.identity_updated': '更新指纹',
  'proxy.created': '添加代理',
  'proxy.updated': '更新代理',
  'proxy.deleted': '删除代理',
  'proxy.check': '检查代理',
  'browser.launched': '启动浏览器',
  'browser.start': '会话就绪',
  'browser.stop': '停止浏览器',
  'browser.error': '浏览器异常',
  'cookies.import': '导入 Cookie',
  'cookies.export': '导出 Cookie',
  'configuration.imported': '导入配置',
  'settings.updated': '更新设置',
}

async function fetchActivity() {
  loading.value = true
  try {
    activities.value = await apiGetActivity()
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchActivity()
  if (profilesStore.profiles.length === 0) {
    profilesStore.fetchProfiles()
  }
})

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    const m = (d.getMonth() + 1).toString().padStart(2, '0')
    const day = d.getDate().toString().padStart(2, '0')
    const h = d.getHours().toString().padStart(2, '0')
    const min = d.getMinutes().toString().padStart(2, '0')
    const sec = d.getSeconds().toString().padStart(2, '0')
    return `${m}-${day} ${h}:${min}:${sec}`
  } catch {
    return iso
  }
}

function getTargetName(profileId?: string): string {
  if (!profileId) return '工作区'
  const match = profilesStore.profiles.find((p) => p.id === profileId)
  return match?.name || profileId.slice(0, 8)
}

function formatDetail(detail: any): string {
  if (!detail || detail === 'success') return '成功'
  if (typeof detail === 'object') {
    return JSON.stringify(detail)
  }
  return String(detail)
}
</script>

<template>
  <div class="activity-view">
    <div class="view-header">
      <div>
        <h1 class="view-title">活动记录</h1>
        <p class="view-desc">
          最近 200 条环境生命周期与工作区操作审计，展示当前可见范围内的真实执行记录。
        </p>
      </div>

      <Button variant="default" :loading="loading" @click="fetchActivity">
        <RefreshCw :size="15" />
        <span>刷新</span>
      </Button>
    </div>

    <div class="table-container">
      <table class="activity-table">
        <thead>
          <tr>
            <th class="th-time">时间</th>
            <th class="th-action">操作类别</th>
            <th class="th-target">关联环境 / 目标</th>
            <th class="th-detail">详情与参数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(act, idx) in activities" :key="act.id || idx">
            <td class="td-time font-mono">{{ formatTime(act.created_at) }}</td>
            <td class="td-action">
              <span class="action-tag">
                {{ EVENT_NAMES[act.action] || act.action }}
              </span>
            </td>
            <td class="td-target">
              <span class="target-name">{{ getTargetName(act.profile_id) }}</span>
            </td>
            <td class="td-detail">
              <span class="detail-text" :title="formatDetail(act.detail)">
                {{ formatDetail(act.detail) }}
              </span>
            </td>
          </tr>

          <tr v-if="activities.length === 0">
            <td colspan="4" class="empty-cell">
              <div class="empty-state">
                <Clock :size="28" />
                <p>暂无活动记录</p>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.activity-view {
  padding: 30px 32px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  overflow-y: auto;
  height: 100%;
}

.view-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}

.view-title {
  font-size: 28px;
  font-weight: 750;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

.view-desc {
  font-size: 13px;
  color: var(--color-muted);
  margin-top: 4px;
}

.table-container {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.activity-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}

thead {
  background-color: #192A3F;
  border-bottom: 1px solid var(--color-border);
}

th {
  padding: 12px 18px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
  white-space: nowrap;
}

tbody tr {
  border-bottom: 1px solid var(--color-border-subtle);
  height: 52px;
}

tbody tr:nth-child(even) {
  background-color: var(--color-table-alt);
}

tbody tr:hover {
  background-color: var(--color-raised);
}

td {
  padding: 10px 18px;
  vertical-align: middle;
}

.td-time {
  color: var(--color-muted);
  font-size: 12px;
  width: 150px;
}

.action-tag {
  display: inline-block;
  padding: 3px 8px;
  background-color: var(--color-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 600;
  color: var(--color-accent);
}

.target-name {
  color: var(--color-text);
  font-weight: 500;
}

.detail-text {
  color: var(--color-subtle);
  font-size: 12px;
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
}

.empty-cell {
  text-align: center;
  padding: 56px !important;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--color-subtle);
}
</style>
