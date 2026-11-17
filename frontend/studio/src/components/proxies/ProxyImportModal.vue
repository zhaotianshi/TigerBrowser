<script setup lang="ts">
import { ref } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { useNotificationStore } from '../../stores/notification'
import { useProxiesStore } from '../../stores/proxies'
import { apiImportProxies } from '../../api/proxies'
import { waitForTask } from '../../api/tasks'
import { generateRequestId } from '../../types/api'

defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'imported'): void
}>()

const notification = useNotificationStore()
const proxiesStore = useProxiesStore()

const proxyText = ref('')
const loading = ref(false)
const error = ref('')

async function onImport() {
  const text = proxyText.value.trim()
  if (!text) {
    error.value = '请粘贴要导入的代理文本'
    return
  }

  loading.value = true
  error.value = ''

  try {
    const result = await waitForTask(await apiImportProxies(generateRequestId(), text))
    notification.success(`已导入 ${result.result?.imported || 0} 个代理资源`)
    emit('imported')
    emit('update:modelValue', false)
    proxyText.value = ''
    await proxiesStore.fetchProxies()
  } catch (err: any) {
    error.value = err.message || '导入代理失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="批量导入代理"
    width="580px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="import-dialog">
      <p class="dialog-desc">
        每行填写一个 HTTP、HTTPS 或 SOCKS5 代理地址。
      </p>

      <div v-if="error" class="dialog-error">
        {{ error }}
      </div>

      <textarea
        v-model="proxyText"
        rows="8"
        placeholder="http://proxy.example:8080&#10;socks5://proxy.example:1080"
      />
    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button variant="primary" type="button" :loading="loading" @click="onImport">
        开始导入
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.import-dialog {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.dialog-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.dialog-desc code {
  background-color: var(--color-bg);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  color: var(--color-accent);
}

.dialog-error {
  padding: 8px 12px;
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-sm);
  font-size: 12px;
}
</style>
