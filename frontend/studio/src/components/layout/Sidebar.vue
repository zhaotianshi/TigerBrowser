<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useEventsStore } from '../../stores/events'
import {
  Globe,
  Radio,
  Clock,
  Sliders,
  Users,
  Activity,
  Lock,
  KeyRound,
  Laptop,
  Download,
} from 'lucide-vue-next'
import { desktopAvailable } from '../../desktop/bridge'

const route = useRoute()
const authStore = useAuthStore()
const eventsStore = useEventsStore()
// Desktop-only pages are absent, not disabled, in an ordinary browser.
const desktop = desktopAvailable()

interface NavItem {
  name: string
  path: string
  label: string
  icon: any
  adminOnly?: boolean
  operatorOnly?: boolean
  desktopOnly?: boolean
}

const navItems: NavItem[] = [
  { name: 'profiles', path: '/profiles', label: '浏览器环境', icon: Globe },
  { name: 'proxies', path: '/proxies', label: '代理资源', icon: Radio, adminOnly: true },
  { name: 'activity', path: '/activity', label: '活动记录', icon: Clock },
  { name: 'api-keys', path: '/api-keys', label: 'API 密钥', icon: KeyRound, operatorOnly: true },
  { name: 'local', path: '/local', label: '本机', icon: Laptop, desktopOnly: true },
  { name: 'downloads', path: '/downloads', label: '客户端下载', icon: Download },
  { name: 'settings', path: '/settings', label: '工作区设置', icon: Sliders, adminOnly: true },
  { name: 'team', path: '/team', label: '团队管理', icon: Users, adminOnly: true },
  { name: 'status', path: '/status', label: '服务状态', icon: Activity, adminOnly: true },
]

const connectionText = computed(() => {
  switch (eventsStore.state) {
    case 'connected':
      return '实时同步 · 正常'
    case 'connecting':
      return '实时同步 · 连接中…'
    case 'error':
    case 'disconnected':
    default:
      return '实时同步 · 已断开'
  }
})
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand">
      <div class="brand-logo">C</div>
      <div class="brand-info">
        <span class="brand-name">TigerBrowser</span>
        <span class="brand-sub">工作区</span>
      </div>
    </div>

    <nav class="sidebar-nav" aria-label="工作区导航">
      <template v-for="item in navItems.filter(entry => !entry.desktopOnly || desktop)" :key="item.path">
        <router-link
          v-if="(!item.adminOnly || authStore.isAdmin) && (!item.operatorOnly || authStore.isOperator)"
          :to="item.path"
          :title="item.label"
          :aria-label="item.label"
          class="nav-item"
          :class="{ active: route.path.startsWith(item.path) }"
        >
          <component :is="item.icon" :size="18" class="nav-icon" />
          <span class="nav-label">{{ item.label }}</span>
        </router-link>

        <div
          v-else
          class="nav-item is-disabled"
          :title="item.operatorOnly ? '此功能仅管理员和操作员可用' : '此功能仅管理员可用'"
        >
          <component :is="item.icon" :size="18" class="nav-icon" />
          <span class="nav-label">{{ item.label }}</span>
          <Lock :size="13" class="lock-icon" />
        </div>
      </template>
    </nav>

    <div class="sidebar-footer">
      <div class="connection-status" :class="`state-${eventsStore.state}`">
        <span class="status-dot" />
        <span class="status-text">{{ connectionText }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  height: 100%;
  background-color: var(--color-sidebar);
  border-right: 1px solid var(--color-border-subtle);
  display: flex;
  flex-direction: column;
  user-select: none;
  flex-shrink: 0;
}

.sidebar-brand {
  padding: 28px 22px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.brand-logo {
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background-color: var(--color-accent);
  color: #092A29;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 850;
  font-size: 20px;
  box-shadow: 0 2px 8px rgba(91, 226, 196, 0.25);
}

.brand-info {
  display: flex;
  flex-direction: column;
}

.brand-name {
  font-size: 19px;
  font-weight: 750;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

.brand-sub {
  font-size: var(--text-xs);
  color: var(--color-muted);
}

.sidebar-nav {
  flex: 1;
  padding: 24px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 14px;
  min-height: 48px;
  border-radius: var(--radius-md);
  color: var(--color-muted);
  text-decoration: none;
  font-size: 15px;
  font-weight: 500;
  transition: background-color 0.18s ease, color 0.18s ease;
}

.nav-item:hover:not(.is-disabled) {
  background-color: var(--color-raised);
  color: var(--color-text);
}

.nav-item.active {
  background: linear-gradient(90deg, var(--color-selected), var(--color-accent-bg));
  color: var(--color-accent);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--color-accent);
}

.nav-item.is-disabled {
  opacity: 0.45;
  cursor: not-allowed;
  color: var(--color-subtle);
}

.lock-icon {
  margin-left: auto;
  opacity: 0.7;
}

.sidebar-footer {
  padding: 20px;
  border-top: 1px solid var(--color-border-subtle);
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-xs);
  color: var(--color-subtle);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--color-subtle);
}

.connection-status.state-connected .status-dot {
  background-color: var(--color-accent);
  box-shadow: 0 0 6px rgba(91, 226, 196, 0.6);
}
.connection-status.state-connected .status-text {
  color: var(--color-muted);
}

.connection-status.state-connecting .status-dot {
  background-color: var(--color-warning);
  animation: pulse 1.2s infinite;
}

.connection-status.state-error .status-dot,
.connection-status.state-disconnected .status-dot {
  background-color: var(--color-danger);
}

@media (min-width: 761px) and (max-width: 1100px) {
  .sidebar { width: 76px; }
  .sidebar-brand { padding: 24px 18px; }
  .brand-info, .nav-label, .lock-icon, .status-text { display: none; }
  .sidebar-nav { padding: 20px 12px; }
  .nav-item { justify-content: center; padding: 14px; }
  .connection-status { justify-content: center; }
}

@media (max-width: 760px) {
  .sidebar { width: 100%; height: auto; border-right: 0; border-bottom: 1px solid var(--color-border); }
  .sidebar-brand, .sidebar-footer { display: none; }
  .sidebar-nav { flex-direction: row; padding: 8px; gap: 6px; overflow-x: auto; }
  .nav-item { flex-shrink: 0; min-height: 44px; padding: 10px 12px; font-size: var(--text-sm); }
  .nav-item.active { box-shadow: inset 0 -2px 0 var(--color-accent); }
}
</style>
