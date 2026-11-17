<script setup lang="ts">
import { ref } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { useNotificationStore } from '../../stores/notification'
import { useProfilesStore } from '../../stores/profiles'
import { apiDeleteProfile } from '../../api/profiles'
import { generateRequestId } from '../../types/api'
import type { ProfileSummary } from '../../types/profile'

const props = defineProps<{
  modelValue: boolean
  profiles: ProfileSummary[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'deleted'): void
}>()

const notification = useNotificationStore()
const profilesStore = useProfilesStore()

const removeData = ref(true)
const deleting = ref(false)

async function onConfirmDelete() {
  deleting.value = true
  let successCount = 0
  const errors: string[] = []

  for (const p of props.profiles) {
    try {
      await apiDeleteProfile(p.id, generateRequestId(), p.revision, removeData.value)
      successCount++
    } catch (err: any) {
      errors.push(`${p.name}: ${err.message}`)
    }
  }

  deleting.value = false
  emit('update:modelValue', false)

  if (successCount > 0) {
    notification.success(`已提交 ${successCount} 个环境的删除任务`)
    emit('deleted')
    await profilesStore.fetchProfiles()
  }

  if (errors.length > 0) {
    notification.error(errors.join('; '))
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="删除浏览器环境"
    width="500px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="delete-dialog-content">
      <p class="warning-text">
        确定要永久删除所选的 {{ profiles.length }} 个环境？此操作无法撤销。
      </p>

      <div class="names-list">
        <div v-for="p in profiles.slice(0, 5)" :key="p.id" class="name-item">
          • {{ p.name }}
        </div>
        <div v-if="profiles.length > 5" class="more-item">
          以及另外 {{ profiles.length - 5 }} 个环境…
        </div>
      </div>

      <div class="checkbox-option">
        <label class="check-label">
          <input v-model="removeData" type="checkbox" />
          <span>同时删除浏览器数据（Cookie、历史记录、扩展及网站存储）</span>
        </label>
        <p class="option-hint">
          取消勾选可将数据保留在执行机磁盘上，以便未来相同环境 ID 恢复。
        </p>
      </div>
    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button variant="danger" type="button" :loading="deleting" @click="onConfirmDelete">
        确认删除
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.delete-dialog-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.warning-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-danger);
  line-height: 1.5;
}

.names-list {
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  max-height: 140px;
  overflow-y: auto;
  font-size: 12px;
  color: var(--color-muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.name-item {
  color: var(--color-text);
}
.more-item {
  color: var(--color-subtle);
  font-style: italic;
  margin-top: 4px;
}

.checkbox-option {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 8px;
}

.check-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  cursor: pointer;
}

.option-hint {
  font-size: 11px;
  color: var(--color-subtle);
  margin-left: 23px;
}
</style>
