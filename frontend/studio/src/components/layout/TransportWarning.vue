<script setup lang="ts">
import { computed } from 'vue'
import { AlertTriangle } from 'lucide-vue-next'

// Check-outs carry site logins and proxy passwords, so the warning cannot be dismissed.
const isHttp = computed(() => location.protocol === 'http:')
</script>

<template>
  <div v-if="isHttp" class="transport-banner" role="alert">
    <div class="banner-content">
      <AlertTriangle :size="16" class="banner-icon" />
      <span>
        当前连接使用未加密的 HTTP。密码、浏览器画面、文件以及本机运行时传输的网站登录状态和代理密码都可能被同一网络上的第三方窃听；仅在可信隔离私网内使用，切勿暴露至公网。
      </span>
    </div>
  </div>
</template>

<style scoped>
.transport-banner {
  background-color: rgba(255, 208, 116, 0.12);
  border-bottom: 1px solid rgba(255, 208, 116, 0.3);
  color: var(--color-warning);
  padding: 8px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--text-xs);
  flex-shrink: 0;
}

.banner-content {
  display: flex;
  align-items: center;
  gap: 10px;
  line-height: 1.6;
}

.banner-icon {
  flex-shrink: 0;
}
</style>
