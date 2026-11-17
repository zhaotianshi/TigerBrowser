<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    variant?: 'accent' | 'blue' | 'danger' | 'warning' | 'muted' | 'subtle'
    status?: 'running' | 'stopped' | 'starting' | 'stopping' | 'error' | 'deleting' | 'recovery_required'
    dot?: boolean
    label?: string
  }>(),
  {
    variant: 'muted',
    dot: false,
  },
)

const resolvedVariant = computed(() => {
  if (props.status) {
    switch (props.status) {
      case 'running':
        return 'accent'
      case 'starting':
      case 'stopping':
        return 'warning'
      case 'error':
      case 'recovery_required':
        return 'danger'
      case 'stopped':
      case 'deleting':
      default:
        return 'muted'
    }
  }
  return props.variant
})

const resolvedText = computed(() => {
  if (props.label) return props.label
  if (props.status) {
    switch (props.status) {
      case 'running':
        return '运行中'
      case 'starting':
        return '启动中'
      case 'stopping':
        return '停止中'
      case 'error':
        return '启动失败'
      case 'recovery_required':
        return '需人工恢复'
      case 'deleting':
        return '删除中'
      case 'stopped':
      default:
        return '未运行'
    }
  }
  return ''
})
</script>

<template>
  <span :class="['badge', `variant-${resolvedVariant}`]">
    <span v-if="status === 'starting' || status === 'stopping'" class="spinner" aria-hidden="true" />
    <span v-else-if="dot || status" class="dot" aria-hidden="true" />
    <slot>{{ resolvedText }}</slot>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
  user-select: none;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: currentColor;
}

.spinner {
  width: 10px;
  height: 10px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.variant-accent {
  background-color: var(--color-accent-bg);
  color: var(--color-accent);
  border: 1px solid rgba(91, 226, 196, 0.28);
}

.variant-blue {
  background-color: var(--color-blue-bg);
  color: var(--color-blue);
  border: 1px solid rgba(117, 171, 255, 0.28);
}

.variant-danger {
  background-color: var(--color-danger-bg);
  color: var(--color-danger);
  border: 1px solid rgba(255, 162, 169, 0.35);
}

.variant-warning {
  background-color: var(--color-warning-bg);
  color: var(--color-warning);
  border: 1px solid rgba(255, 208, 116, 0.28);
}

.variant-muted {
  background-color: rgba(173, 188, 208, 0.1);
  color: var(--color-muted);
  border: 1px solid rgba(173, 188, 208, 0.2);
}

.variant-subtle {
  background-color: transparent;
  color: var(--color-subtle);
  border: 1px solid transparent;
}
</style>
