<script setup lang="ts">
import { ref } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { useNotificationStore } from '../../stores/notification'
import { apiResetMemberPassword } from '../../api/team'
import type { Member } from '../../types/member'

const props = defineProps<{
  modelValue: boolean
  member: Member | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'reset'): void
}>()

const notification = useNotificationStore()

const newPassword = ref('')
const loading = ref(false)
const error = ref('')

async function onSubmit() {
  if (!props.member) return
  if (!newPassword.value || newPassword.value.length < 12) {
    error.value = '密码长度至少需要 12 个字符'
    return
  }

  loading.value = true
  error.value = ''
  try {
    await apiResetMemberPassword(props.member.id, newPassword.value)
    notification.success(`已成功重置成员 "${props.member.display}" 的密码`)
    emit('reset')
    emit('update:modelValue', false)
    newPassword.value = ''
  } catch (err: any) {
    error.value = err.message || '重置密码失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="重置成员密码"
    width="440px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <form class="reset-form" @submit.prevent="onSubmit">
      <p class="form-desc">
        重置后，成员 <strong>{{ member?.display }}</strong> 的所有活动登录会话将立即注销失效。
      </p>

      <div v-if="error" class="form-error">
        {{ error }}
      </div>

      <div class="form-group">
        <label>新密码（至少 12 个字符）</label>
        <input
          v-model="newPassword"
          type="password"
          minlength="12"
          placeholder="输入新密码"
          required
        />
      </div>

      <div class="form-actions">
        <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
          取消
        </Button>
        <Button variant="primary" type="submit" :loading="loading">
          确认重置
        </Button>
      </div>
    </form>
  </Modal>
</template>

<style scoped>
.reset-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.form-error {
  padding: 8px 12px;
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: 12px;
  color: var(--color-muted);
  font-weight: 500;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 8px;
}
</style>
