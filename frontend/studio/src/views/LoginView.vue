<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'
import Button from '../components/common/Button.vue'
import { Lock, AlertTriangle } from 'lucide-vue-next'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const notification = useNotificationStore()

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

const isHttp = computed(() => location.protocol === 'http:')

async function onSubmit() {
  error.value = ''
  if (!username.value.trim() || !password.value) {
    error.value = '请填写用户名和密码'
    return
  }

  loading.value = true
  try {
    await authStore.login(username.value.trim(), password.value)
    notification.success(`欢迎回来，${authStore.member?.display || username.value}`)
    const redirect = (route.query.redirect as string) || '/profiles'
    router.push(redirect)
  } catch (err: any) {
    error.value = err.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-container">
      <!-- Brand -->
      <div class="login-brand">
        <div class="brand-logo" aria-hidden="true">T</div>
        <h1 class="brand-title">TigerBrowser</h1>
        <p class="brand-subtitle">团队控制台 · 执行机环境工作区</p>
      </div>

      <!-- Plaintext HTTP warning if applicable -->
      <div v-if="isHttp" class="http-warning" role="alert">
        <AlertTriangle :size="16" class="warn-icon" />
        <span>HTTP 未加密 · 仅在受信任的局域网内使用</span>
      </div>

      <!-- Login Form Card -->
      <div class="login-card">
        <h2 class="card-title">成员身份登录</h2>
        <p class="card-desc">浏览器数据均隔离在执行机本地，使用管理员分配的账号访问。</p>

        <form class="login-form" @submit.prevent="onSubmit">
          <div v-if="error" class="login-error" role="alert">
            {{ error }}
          </div>

          <div class="form-group">
            <label for="username">用户名</label>
            <input
              id="username"
              v-model="username"
              type="text"
              autocomplete="username"
              placeholder="输入团队用户名"
              maxlength="64"
              required
            />
          </div>

          <div class="form-group">
            <label for="password">密码</label>
            <input
              id="password"
              v-model="password"
              type="password"
              autocomplete="current-password"
              placeholder="输入登录密码"
              maxlength="1024"
              required
            />
          </div>

          <Button variant="primary" type="submit" :loading="loading" class="login-submit">
            <Lock :size="15" />
            <span>登录控制台</span>
          </Button>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  width: 100vw;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: var(--color-bg);
  padding: 20px;
  user-select: none;
}

.login-container {
  width: 100%;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.login-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.brand-logo {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background-color: var(--color-accent);
  color: #092A29;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 850;
  font-size: 30px;
  box-shadow: 0 4px 16px rgba(91, 226, 196, 0.35);
  margin-bottom: 12px;
}

.brand-title {
  font-size: 24px;
  font-weight: 800;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

.brand-subtitle {
  font-size: 13px;
  color: var(--color-muted);
  margin-top: 4px;
}

.http-warning {
  display: flex;
  align-items: center;
  gap: 8px;
  background-color: var(--color-warning-bg);
  border: 1px solid rgba(255, 208, 116, 0.35);
  color: var(--color-warning);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  font-size: 12px;
}

.login-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 28px 24px;
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text);
}

.card-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.login-error {
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
  font-weight: 600;
  color: var(--color-muted);
}

.login-submit {
  width: 100%;
  margin-top: 4px;
  height: 38px;
}
</style>
