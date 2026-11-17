<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useNotificationStore } from '../../stores/notification'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { ShieldAlert, ShieldCheck, KeyRound, LogOut } from 'lucide-vue-next'

const router = useRouter()
const authStore = useAuthStore()
const notification = useNotificationStore()

const showPasswordModal = ref(false)
const showSecurityModal = ref(false)

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passwordLoading = ref(false)
const passwordError = ref('')

const isSecure = computed(() => {
  return location.protocol === 'https:' || authStore.capabilities?.secure_transport
})

const roleText = computed(() => {
  switch (authStore.member?.role) {
    case 'admin':
      return '管理员'
    case 'operator':
      return '操作员'
    case 'viewer':
      return '观察员'
    default:
      return '成员'
  }
})

async function onLogout() {
  await authStore.logout()
  notification.info('已退出登录')
  router.push('/login')
}

async function onSubmitPassword() {
  passwordError.value = ''
  if (!currentPassword.value) {
    passwordError.value = '请输入当前密码'
    return
  }
  if (!newPassword.value || newPassword.value.length < 12) {
    passwordError.value = '新密码长度至少需要 12 个字符'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }

  passwordLoading.value = true
  try {
    await authStore.changePassword(currentPassword.value, newPassword.value)
    notification.success('密码修改成功，请重新登录')
    showPasswordModal.value = false
    router.push('/login')
  } catch (err: any) {
    passwordError.value = err.message || '修改密码失败'
  } finally {
    passwordLoading.value = false
  }
}
</script>

<template>
  <header class="app-header">
    <div class="header-left">
      <button
        class="ghost security-badge"
        :class="{ 'is-insecure': !isSecure }"
        type="button"
        @click="showSecurityModal = true"
      >
        <ShieldCheck v-if="isSecure" :size="15" class="icon-secure" />
        <ShieldAlert v-else :size="15" class="icon-insecure" />
        <span>{{ isSecure ? 'HTTPS 加密传输' : 'HTTP 未加密' }}</span>
      </button>
    </div>

    <div class="header-right">
      <div v-if="authStore.member" class="user-badge">
        <span class="user-display">{{ authStore.member.display }}</span>
        <span class="user-role">{{ roleText }}</span>
      </div>

      <button class="ghost action-btn" type="button" @click="showPasswordModal = true">
        <KeyRound :size="15" />
        <span>修改密码</span>
      </button>

      <button class="ghost action-btn logout-btn" type="button" @click="onLogout">
        <LogOut :size="15" />
        <span>退出</span>
      </button>
    </div>

    <!-- Change Password Modal -->
    <Modal v-model="showPasswordModal" title="修改自己的密码" width="460px">
      <form class="password-form" @submit.prevent="onSubmitPassword">
        <p class="form-desc">
          密码修改后，当前成员在所有设备上的登录将立即失效。正在运行的浏览器任务不会中断。
        </p>

        <div v-if="passwordError" class="form-error">
          {{ passwordError }}
        </div>

        <div class="form-group">
          <label>当前密码</label>
          <input
            v-model="currentPassword"
            type="password"
            autocomplete="current-password"
            placeholder="填写当前登录密码"
            required
          />
        </div>

        <div class="form-group">
          <label>新密码（至少 12 个字符）</label>
          <input
            v-model="newPassword"
            type="password"
            autocomplete="new-password"
            minlength="12"
            placeholder="填写新密码"
            required
          />
        </div>

        <div class="form-group">
          <label>确认新密码</label>
          <input
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            minlength="12"
            placeholder="再次填写新密码"
            required
          />
        </div>

        <div class="form-actions">
          <Button variant="ghost" type="button" @click="showPasswordModal = false">
            取消
          </Button>
          <Button variant="primary" type="submit" :loading="passwordLoading">
            保存并重新登录
          </Button>
        </div>
      </form>
    </Modal>

    <!-- Security Info Modal -->
    <Modal v-model="showSecurityModal" title="网络范围与传输安全状态" width="520px">
      <div class="security-info">
        <div class="security-status-box" :class="{ 'is-insecure': !isSecure }">
          <div class="status-title">
            <component :is="isSecure ? ShieldCheck : ShieldAlert" :size="20" />
            <span>{{ isSecure ? '传输保密性：已启用 TLS/HTTPS' : '传输风险警告：当前为明文 HTTP' }}</span>
          </div>
          <p class="status-text">
            {{
              isSecure
                ? '所有 API 通信、WebSocket 状态与原生远程画面均受到 TLS 加密保护。'
                : '当前连接使用未加密的 HTTP。密码、Cookie、浏览器画面和传输文件可能被同一局域网上的抓包设备窃听。仅在可信隔离私网环境使用，切勿暴露到公网。'
            }}
          </p>
        </div>

        <div class="security-details">
          <div class="detail-row">
            <span class="detail-label">私网限制 (private_network_only)</span>
            <span class="detail-value">
              {{ authStore.capabilities?.private_network_only ? '已启用 (仅限回环/RFC1918/ULA)' : '未限制' }}
            </span>
          </div>
          <div class="detail-row">
            <span class="detail-label">客户端白名单 (client_allowlist)</span>
            <span class="detail-value">
              {{ authStore.capabilities?.client_allowlist ? '已启用 IP/CIDR 白名单' : '未启用' }}
            </span>
          </div>
          <div class="detail-row">
            <span class="detail-label">执行模式 (browser_mode)</span>
            <span class="detail-value">{{ authStore.capabilities?.browser_mode || '未知' }}</span>
          </div>
        </div>
      </div>
    </Modal>
  </header>
</template>

<style scoped>
.app-header {
  min-height: 68px;
  background-color: var(--color-sidebar);
  border-bottom: 1px solid var(--color-border);
  padding: 10px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  user-select: none;
}

.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.security-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: 600;
  border: 1px solid rgba(91, 226, 196, 0.3);
  color: var(--color-accent);
}

.security-badge.is-insecure {
  border-color: rgba(255, 208, 116, 0.4);
  color: var(--color-warning);
  background-color: var(--color-warning-bg);
}

.icon-secure {
  color: var(--color-accent);
}
.icon-insecure {
  color: var(--color-warning);
}

.user-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  background-color: var(--color-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
}

.user-display {
  font-weight: 600;
  color: var(--color-text);
}

.user-role {
  color: var(--color-accent);
  font-size: var(--text-xs);
}

.action-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: var(--text-sm);
}

.logout-btn:hover {
  color: var(--color-danger);
}

.password-form {
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

.security-info {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.security-status-box {
  padding: 14px 16px;
  border-radius: var(--radius-md);
  border: 1px solid rgba(91, 226, 196, 0.3);
  background-color: var(--color-accent-bg);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.security-status-box.is-insecure {
  border-color: rgba(255, 208, 116, 0.4);
  background-color: var(--color-warning-bg);
}

.status-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 13px;
  color: var(--color-text);
}

.status-text {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.security-details {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.detail-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--color-border-subtle);
}
.detail-row:last-child {
  border-bottom: none;
}

.detail-label {
  color: var(--color-muted);
}
.detail-value {
  color: var(--color-text);
  font-weight: 500;
}

@media (max-width: 760px) {
  .app-header { padding: 10px 16px; flex-wrap: wrap; gap: 8px; }
  .header-right { gap: 4px; flex-wrap: wrap; }
  .action-btn { padding: 8px; }
  .user-badge { padding: 6px 10px; }
}
</style>
