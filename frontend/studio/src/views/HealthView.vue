<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { apiGetHealth } from '../api/health'
import { useNotificationStore } from '../stores/notification'
import Button from '../components/common/Button.vue'
import StatCard from '../components/common/StatCard.vue'
import Badge from '../components/common/Badge.vue'
import {
  HardDrive,
  RefreshCw,
  Monitor,
  AlertTriangle,
  PlayCircle,
  Clock,
  ListTodo,
} from 'lucide-vue-next'
import type { HealthData } from '../types/health'

const notification = useNotificationStore()

const loading = ref(false)
const health = ref<HealthData | null>(null)

async function fetchHealth() {
  loading.value = true
  try {
    health.value = await apiGetHealth()
  } catch (err: any) {
    notification.error(err.message || '获取服务健康状态失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchHealth()
})

function formatBytes(bytes?: number): string {
  if (!bytes) return '0 GB'
  const gb = bytes / (1024 * 1024 * 1024)
  return `${gb.toFixed(1)} GB`
}
</script>

<template>
  <div class="health-view">
    <div class="view-header">
      <div>
        <h1 class="view-title">服务与执行机状态</h1>
        <p class="view-desc">
          查看固定执行机运行时指标、并发容量、磁盘可用空间与服务屏障。
        </p>
      </div>

      <Button variant="default" :loading="loading" @click="fetchHealth">
        <RefreshCw :size="15" />
        <span>刷新状态</span>
      </Button>
    </div>

    <div v-if="health" class="health-content">
      <!-- Top Metrics Cards -->
      <div class="metrics-grid">
        <StatCard
          label="活动运行 / 并发容量"
          :value="`${health.active_runs} / ${health.max_running}`"
          subtext="当前活动浏览器会话数"
          :icon="PlayCircle"
          accent
        />

        <StatCard
          label="挂起命令队列"
          :value="health.pending_commands"
          subtext="等待执行或确认的异步启停命令"
          :icon="Clock"
        />

        <StatCard
          label="后台活动任务"
          :value="health.active_tasks"
          subtext="代理检测、订阅、内核等后台任务"
          :icon="ListTodo"
        />

        <StatCard
          label="可用磁盘空间"
          :value="formatBytes(health.disk_free_bytes)"
          subtext="工作区所在文件系统剩余空间"
          :icon="HardDrive"
        />
      </div>

      <!-- Execution Status & Barriers -->
      <div class="status-sections">
        <section class="section-card">
          <div class="section-head">
            <Monitor :size="18" class="head-icon" />
            <h3 class="head-title">执行机与图形状态</h3>
          </div>

          <div class="status-rows">
            <div class="status-row">
              <span class="row-label">执行运行时已注册 (runtime_registered)</span>
              <Badge
                :variant="health.runtime_registered ? 'accent' : 'danger'"
                :label="health.runtime_registered ? '已就绪' : '未注册'"
              />
            </div>

            <div class="status-row">
              <span class="row-label">原生远程图形画面 (remote_display)</span>
              <Badge
                :variant="health.remote_display ? 'accent' : 'muted'"
                :label="health.remote_display ? '启用 (Linux remote)' : '未启用'"
              />
            </div>

            <div class="status-row">
              <span class="row-label">服务维护状态 (maintenance)</span>
              <Badge
                :variant="health.maintenance ? 'warning' : 'accent'"
                :label="health.maintenance ? '维护中 (独占)' : '正常运行'"
              />
            </div>

            <div class="status-row">
              <span class="row-label">服务退出中 (closing)</span>
              <Badge
                :variant="health.closing ? 'warning' : 'accent'"
                :label="health.closing ? '正在关闭' : '正常'"
              />
            </div>
          </div>
        </section>

        <section class="section-card">
          <div class="section-head">
            <AlertTriangle :size="18" class="head-icon" />
            <h3 class="head-title">安全与恢复屏障</h3>
          </div>

          <div class="status-rows">
            <div class="status-row">
              <span class="row-label">执行阻塞屏障 (execution_blocked)</span>
              <Badge
                :variant="health.execution_blocked ? 'danger' : 'accent'"
                :label="health.execution_blocked ? '已阻塞' : '无阻塞'"
              />
            </div>

            <div class="status-row">
              <span class="row-label">需要人工故障恢复 (recovery_required)</span>
              <Badge
                :variant="health.recovery_required ? 'danger' : 'accent'"
                :label="health.recovery_required ? '需要人工恢复' : '状态健康'"
              />
            </div>
          </div>

          <div class="note-box">
            <p>
              说明：依据 <code>ci/tribal-rules.yml</code> 规范，单机团队服务不返回虚构的 CPU/内存百分比或分布式节点探针。展示指标全部来自实际执行机与数据库状态。
            </p>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.health-view {
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

.health-content {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.status-sections {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}

.section-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-head {
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--color-border-subtle);
  padding-bottom: 12px;
}

.head-icon {
  color: var(--color-accent);
}

.head-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
}

.status-rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm);
}

.row-label {
  font-size: 13px;
  color: var(--color-text);
  font-weight: 500;
}

.note-box {
  margin-top: auto;
  background-color: rgba(173, 188, 208, 0.06);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  font-size: 11px;
  color: var(--color-subtle);
  line-height: 1.5;
}
.note-box code {
  color: var(--color-accent);
}
</style>
