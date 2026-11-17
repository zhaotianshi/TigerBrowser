<script setup lang="ts">
import { ref, watch } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { useNotificationStore } from '../../stores/notification'
import { useProfilesStore } from '../../stores/profiles'
import { apiGrantMember, apiRevokeMember } from '../../api/team'
import type { Member } from '../../types/member'
import type { ProfilePermission } from '../../types/profile'

const props = defineProps<{
  modelValue: boolean
  member: Member | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'updated'): void
}>()

const notification = useNotificationStore()
const profilesStore = useProfilesStore()

const grantsMap = ref<Record<string, ProfilePermission[]>>({})
const saving = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (!open || !props.member) return
    const initial: Record<string, ProfilePermission[]> = {}
    if (props.member.grants) {
      for (const [pid, perms] of Object.entries(props.member.grants)) {
        initial[pid] = [...perms]
      }
    }
    grantsMap.value = initial
  },
)

function togglePerm(profileId: string, perm: ProfilePermission) {
  const cur = grantsMap.value[profileId] || []
  const next = [...cur]
  const idx = next.indexOf(perm)
  if (idx !== -1) {
    next.splice(idx, 1)
  } else {
    next.push(perm)
  }
  grantsMap.value[profileId] = next
}

async function onSave() {
  if (!props.member) return
  saving.value = true

  try {
    for (const profile of profilesStore.profiles) {
      const perms = grantsMap.value[profile.id] || []
      if (perms.length > 0) {
        await apiGrantMember(props.member.id, profile.id, perms)
      } else if (props.member.grants?.[profile.id]) {
        await apiRevokeMember(props.member.id, profile.id)
      }
    }
    notification.success(`已更新成员 "${props.member.display}" 的环境授权`)
    emit('updated')
    emit('update:modelValue', false)
  } catch (err: any) {
    notification.error(err.message || '更新授权失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="成员直接授权管理"
    width="640px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-if="member" class="grant-dialog">
      <p class="grant-desc">
        为 <strong>{{ member.display }}</strong>（{{ member.role === 'operator' ? '操作员' : '观察员' }}）配置环境授权。原生画面观看需要 <code>watch</code>，控制操作还需要 <code>operate</code>。
        <span v-if="member.role === 'viewer'" class="ceiling-note">
          （观察员角色上限不可分配操作权限）
        </span>
      </p>

      <div class="grants-list">
        <div v-for="profile in profilesStore.profiles" :key="profile.id" class="grant-row">
          <div class="profile-info">
            <span class="p-name">{{ profile.name }}</span>
            <span class="p-sub">{{ profile.os }} · {{ profile.group_name || '未分组' }}</span>
          </div>

          <div class="perm-options">
            <label class="perm-checkbox">
              <input
                type="checkbox"
                :checked="(grantsMap[profile.id] || []).includes('view')"
                @change="togglePerm(profile.id, 'view')"
              />
              <span>查看</span>
            </label>

            <label class="perm-checkbox">
              <input
                type="checkbox"
                :checked="(grantsMap[profile.id] || []).includes('watch')"
                @change="togglePerm(profile.id, 'watch')"
              />
              <span>观看画面</span>
            </label>

            <label class="perm-checkbox" :class="{ disabled: member.role === 'viewer' }">
              <input
                type="checkbox"
                :disabled="member.role === 'viewer'"
                :checked="(grantsMap[profile.id] || []).includes('operate')"
                @change="togglePerm(profile.id, 'operate')"
              />
              <span>控制操作</span>
            </label>

            <label class="perm-checkbox" :class="{ disabled: member.role === 'viewer' }"
              title="把网站登录状态和代理密码交给成员电脑，在成员电脑上运行此环境">
              <input
                type="checkbox"
                :disabled="member.role === 'viewer'"
                :checked="(grantsMap[profile.id] || []).includes('checkout')"
                @change="togglePerm(profile.id, 'checkout')"
              />
              <span>本机运行</span>
            </label>
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button variant="primary" type="button" :loading="saving" @click="onSave">
        保存授权
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.grant-dialog {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.grant-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.grant-desc code {
  color: var(--color-accent);
  background-color: var(--color-bg);
  padding: 1px 4px;
  border-radius: var(--radius-sm);
}

.ceiling-note {
  color: var(--color-warning);
}

.grants-list {
  max-height: 360px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg);
}

.grant-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-border-subtle);
}
.grant-row:last-child {
  border-bottom: none;
}

.profile-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.p-name {
  font-weight: 600;
  color: var(--color-text);
  font-size: 13px;
}
.p-sub {
  font-size: 11px;
  color: var(--color-subtle);
}

.perm-options {
  display: flex;
  align-items: center;
  gap: 16px;
}

.perm-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-muted);
  cursor: pointer;
}

.perm-checkbox.disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
