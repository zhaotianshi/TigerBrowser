<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{
  label: string
  value: number | string
  subtext?: string
  icon?: Component
  accent?: boolean
}>()
</script>

<template>
  <div class="stat-card" :class="{ 'is-accent': accent }">
    <div class="stat-header">
      <span class="stat-label">{{ label }}</span>
      <div v-if="icon" class="stat-icon">
        <component :is="icon" :size="21" aria-hidden="true" />
      </div>
    </div>
    <div class="stat-value">{{ value }}</div>
    <div v-if="subtext" class="stat-subtext">{{ subtext }}</div>
  </div>
</template>

<style scoped>
.stat-card {
  position: relative;
  background: linear-gradient(120deg, var(--color-raised), var(--color-surface) 70%);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035), var(--shadow-sm);
}

.stat-card.is-accent {
  background: linear-gradient(120deg, var(--color-selected), var(--color-surface) 80%);
  border-color: var(--color-accent-border);
}

.stat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-right: 54px;
}

.stat-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-muted);
}

.stat-icon {
  position: absolute;
  right: 24px;
  top: 24px;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-md);
  color: var(--color-blue);
  background: var(--color-blue-bg);
}

.is-accent .stat-icon {
  color: var(--color-accent);
  background: var(--color-accent-bg);
}

.is-accent .stat-value {
  color: var(--color-accent);
}

.stat-value {
  font-size: 2.125rem;
  font-weight: 700;
  color: var(--color-text);
  line-height: 1.1;
  font-feature-settings: 'tnum';
}

.stat-subtext {
  font-size: var(--text-xs);
  color: var(--color-subtle);
}

@media (max-width: 760px) {
  .stat-card { padding: 16px 20px; gap: 6px; }
  .stat-icon { top: 18px; right: 20px; }
  .stat-value { font-size: 1.75rem; }
}
</style>
