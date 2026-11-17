<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { apiProbeProxy } from '../../api/proxies'
import { waitForTask } from '../../api/tasks'
import { useProxiesStore } from '../../stores/proxies'
import { generateRequestId } from '../../types/api'
import type { ProxyItem } from '../../types/proxy'
import { Activity, CheckCircle2, AlertCircle } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
  proxy: ProxyItem | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
}>()

const loading = ref(false)
const probeResult = ref<any>(null)
const error = ref('')
const proxiesStore = useProxiesStore()

let pending: AbortController | null = null
onBeforeUnmount(() => pending?.abort())
watch(
  () => props.modelValue,
  async (open) => {
    pending?.abort()
    if (!open || !props.proxy) return
    const control = new AbortController()
    pending = control
    loading.value = true
    probeResult.value = null
    error.value = ''
    try {
      const task = await waitForTask(await apiProbeProxy(props.proxy.id, generateRequestId()), control.signal)
      probeResult.value = task.result || {}
      await proxiesStore.fetchProxies()
    } catch (err: any) {
      if (!control.signal.aborted) error.value = err.message || '代理测试失败'
    } finally {
      if (pending === control) loading.value = false
    }
  },
)

</script>

<template>
  <Modal
    :model-value="modelValue"
    title="代理连接与出口探测"
    width="480px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-if="proxy" class="probe-dialog">
      <div class="target-info">
        <span class="target-name">{{ proxy.name }}</span>
        <span class="target-server font-mono">{{ proxy.server }}</span>
      </div>

      <div v-if="loading" class="probing-state">
        <Activity :size="24" class="probing-icon" />
        <p>执行机正在测试握手与出口 IP…</p>
      </div>

      <div v-else-if="probeResult" class="result-box">
        <div class="result-header">
          <CheckCircle2 :size="18" class="success-icon" />
          <span class="result-status">测试成功 · 节点可用</span>
        </div>

        <div class="metrics-grid">
          <div class="metric-row">
            <span class="metric-k">出口公网 IP:</span>
            <span class="metric-v font-mono">{{ probeResult.ip || '已连接' }}</span>
          </div>
          <div class="metric-row">
            <span class="metric-k">出口时区:</span>
            <span class="metric-v">{{ probeResult.timezone || '未知' }}</span>
          </div>
          <div class="metric-row">
            <span class="metric-k">往返延迟:</span>
            <span class="metric-v font-mono">{{ probeResult.latency_ms ? `${probeResult.latency_ms} ms` : '良好' }}</span>
          </div>
        </div>
      </div>

      <div v-else-if="error" class="error-box">
        <AlertCircle :size="18" class="error-icon" />
        <span>{{ error }}</span>
      </div>
    </div>

    <template #footer>
      <Button variant="primary" type="button" @click="emit('update:modelValue', false)">
        完成
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.probe-dialog {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.target-info {
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.target-name {
  font-weight: 600;
  color: var(--color-text);
}
.target-server {
  font-size: 11px;
  color: var(--color-subtle);
}

.probing-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 32px;
  color: var(--color-muted);
}

.probing-icon {
  color: var(--color-accent);
  animation: pulse 1s infinite;
}

.result-box {
  background-color: rgba(91, 226, 196, 0.08);
  border: 1px solid rgba(91, 226, 196, 0.3);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.result-header {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-accent);
  font-weight: 700;
  font-size: 14px;
}

.metrics-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px solid rgba(91, 226, 196, 0.2);
  padding-top: 10px;
}

.metric-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}

.metric-k {
  color: var(--color-muted);
}
.metric-v {
  color: var(--color-text);
  font-weight: 600;
}

.error-box {
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-md);
  padding: 14px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
</style>
