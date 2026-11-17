<script setup lang="ts">
import { downloadBlob } from '../../utils/download'
import { ref } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { useNotificationStore } from '../../stores/notification'
import { apiGetCookies, apiSetCookies } from '../../api/profiles'
import { Download, Upload } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
  profileId: string | null
  profileName?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
}>()

const notification = useNotificationStore()

const exporting = ref(false)
const importing = ref(false)
const cookieJson = ref('')
const error = ref('')

async function onExport() {
  if (!props.profileId) return
  exporting.value = true
  error.value = ''
  try {
    const cookies = await apiGetCookies(props.profileId)
    const jsonStr = JSON.stringify(cookies, null, 2)
    const blob = new Blob([jsonStr], { type: 'application/json' })
    downloadBlob(blob, `cookies-${props.profileName || props.profileId}.json`)
    notification.success(`已导出 ${cookies.length} 条 Cookie`)
  } catch (err: any) {
    error.value = err.message || '导出 Cookie 失败'
  } finally {
    exporting.value = false
  }
}

async function onImport() {
  if (!props.profileId) return
  error.value = ''

  let parsed: any
  try {
    parsed = JSON.parse(cookieJson.value)
    if (!Array.isArray(parsed)) {
      throw new Error('Cookie 数据必须是 JSON 数组')
    }
  } catch (err: any) {
    error.value = `JSON 解析错误: ${err.message}`
    return
  }

  importing.value = true
  try {
    await apiSetCookies(props.profileId, parsed)
    notification.success(`成功导入 ${parsed.length} 条 Cookie`)
    emit('update:modelValue', false)
  } catch (err: any) {
    error.value = err.message || '导入 Cookie 失败'
  } finally {
    importing.value = false
  }
}

function onFileSelect(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    cookieJson.value = String(reader.result || '')
  }
  reader.readAsText(file)
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="Cookie 导入与导出"
    width="540px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="cookie-dialog">
      <p class="dialog-desc">
        仅对当前处于运行中（running）状态的环境生效。浏览器会实时写入并校验 Cookie 格式。
      </p>

      <div v-if="error" class="dialog-error">
        {{ error }}
      </div>

      <div class="export-box">
        <div>
          <h4 class="box-title">导出当前会话 Cookie</h4>
          <p class="box-desc">将浏览器当前已保存的全部 Cookie 保存为标准 JSON 文件。</p>
        </div>
        <Button variant="default" size="compact" :loading="exporting" @click="onExport">
          <Download :size="14" />
          <span>导出 JSON</span>
        </Button>
      </div>

      <div class="import-section">
        <h4 class="box-title">导入 Cookie 到当前会话</h4>
        <div class="file-picker">
          <input type="file" accept=".json" @change="onFileSelect" />
        </div>
        <textarea
          v-model="cookieJson"
          rows="6"
          placeholder="或者在此粘贴 Cookie JSON 数组，例如：&#10;[{ &quot;name&quot;: &quot;session_id&quot;, &quot;value&quot;: &quot;xyz&quot;, &quot;domain&quot;: &quot;.example.com&quot; }]"
        />
      </div>
    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button
        variant="primary"
        type="button"
        :loading="importing"
        :disabled="!cookieJson.trim()"
        @click="onImport"
      >
        <Upload :size="14" />
        <span>确认导入</span>
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.cookie-dialog {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dialog-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.dialog-error {
  padding: 8px 12px;
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.export-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
}

.box-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.box-desc {
  font-size: 11px;
  color: var(--color-subtle);
  margin-top: 2px;
}

.import-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.file-picker input {
  font-size: 12px;
  color: var(--color-muted);
}
</style>
