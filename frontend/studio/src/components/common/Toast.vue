<script setup lang="ts">
import { useNotificationStore } from '../../stores/notification'
import { CheckCircle2, AlertTriangle, AlertCircle, Info, X } from 'lucide-vue-next'

const store = useNotificationStore()
</script>

<template>
  <div class="toast-container" role="status" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <div
        v-for="item in store.toasts"
        :key="item.id"
        :class="['toast-item', `toast-${item.type}`]"
      >
        <div class="toast-icon">
          <CheckCircle2 v-if="item.type === 'success'" :size="18" />
          <AlertCircle v-else-if="item.type === 'error'" :size="18" />
          <AlertTriangle v-else-if="item.type === 'warning'" :size="18" />
          <Info v-else :size="18" />
        </div>
        <div class="toast-content">{{ item.message }}</div>
        <button class="ghost toast-close" type="button" aria-label="关闭通知" @click="store.remove(item.id)">
          <X :size="14" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 2000;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 420px;
  pointer-events: none;
}

.toast-item {
  pointer-events: auto;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 16px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  color: var(--color-text);
  font-size: var(--text-sm);
  line-height: 1.4;
}

.toast-icon {
  margin-top: 1px;
}

.toast-success {
  border-color: rgba(91, 226, 196, 0.4);
}
.toast-success .toast-icon {
  color: var(--color-accent);
}

.toast-error {
  border-color: rgba(255, 162, 169, 0.5);
}
.toast-error .toast-icon {
  color: var(--color-danger);
}

.toast-warning {
  border-color: rgba(255, 208, 116, 0.5);
}
.toast-warning .toast-icon {
  color: var(--color-warning);
}

.toast-info {
  border-color: rgba(117, 171, 255, 0.4);
}
.toast-info .toast-icon {
  color: var(--color-blue);
}

.toast-content {
  flex: 1;
  word-break: break-word;
}

.toast-close {
  padding: 2px;
  color: var(--color-subtle);
  border: none;
  background: transparent;
}
.toast-close:hover {
  color: var(--color-text);
}

.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease;
}

.toast-enter-from {
  opacity: 0;
  transform: translateY(20px) scale(0.95);
}

.toast-leave-to {
  opacity: 0;
  transform: translateX(30px);
}
</style>
