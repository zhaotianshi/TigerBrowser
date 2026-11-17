<script setup lang="ts">
import { computed } from 'vue'
import { useProxiesStore } from '../../stores/proxies'
import { useProfilesStore } from '../../stores/profiles'
import type { ProxyItem } from '../../types/proxy'
import { Radio, Edit, Trash2, Activity } from 'lucide-vue-next'

const props = defineProps<{
  onEdit: (proxy: ProxyItem) => void
  onDelete: (proxy: ProxyItem) => void
  onProbe: (proxy: ProxyItem) => void
}>()

const proxiesStore = useProxiesStore()
const profilesStore = useProfilesStore()

const list = computed(() => proxiesStore.filteredProxies)

function getLinkedCount(proxyId: string): number {
  return profilesStore.profiles.filter((p) => p.proxy?.id === proxyId).length
}

function getProtocol(server: string): string {
  const parts = server.split('://')
  return parts.length > 1 ? parts[0].toUpperCase() : 'HTTP'
}
</script>

<template>
  <div class="proxy-table-container">
    <table class="proxy-table">
      <thead>
        <tr>
          <th>代理名称</th>
          <th>协议</th>
          <th>服务器地址</th>
          <th>认证凭据</th>
          <th>已绑定环境</th>
          <th class="th-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="proxy in list" :key="proxy.id">
          <td class="td-name">
            <div class="name-box">
              <Radio :size="15" class="proxy-icon" />
              <span class="name-text">{{ proxy.name }}</span>
            </div>
          </td>

          <td class="td-proto">
            <span class="proto-tag">{{ getProtocol(proxy.server) }}</span>
          </td>

          <td class="td-server">
            <span class="server-text font-mono">{{ proxy.server }}</span>
          </td>

          <td class="td-auth">
            <span v-if="proxy.has_password" class="auth-saved">已保存凭据</span>
            <span v-else class="auth-none">无密码</span>
          </td>

          <td class="td-linked">
            <span class="linked-count">{{ getLinkedCount(proxy.id) }} 个环境</span>
          </td>

          <td class="td-actions">
            <button
              class="ghost icon-btn"
              title="网络测试与探测"
              type="button"
              @click="props.onProbe(proxy)"
            >
              <Activity :size="14" />
            </button>
            <button
              class="ghost icon-btn"
              title="编辑代理"
              type="button"
              @click="props.onEdit(proxy)"
            >
              <Edit :size="14" />
            </button>
            <button
              class="ghost icon-btn btn-danger"
              title="删除代理"
              type="button"
              @click="props.onDelete(proxy)"
            >
              <Trash2 :size="14" />
            </button>
          </td>
        </tr>

        <tr v-if="list.length === 0">
          <td colspan="6" class="empty-cell">
            <div class="empty-state">
              <p>暂无代理资源。点击上方“添加代理”或“批量导入”添加。</p>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.proxy-table-container {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.proxy-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
  user-select: none;
}

thead {
  background-color: #192A3F;
  border-bottom: 1px solid var(--color-border);
}

th {
  padding: 12px 16px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
  white-space: nowrap;
}

tbody tr {
  border-bottom: 1px solid var(--color-border-subtle);
  height: 54px;
  transition: background-color 0.1s ease;
}

tbody tr:nth-child(even) {
  background-color: var(--color-table-alt);
}

tbody tr:hover {
  background-color: var(--color-raised);
}

td {
  padding: 10px 16px;
  vertical-align: middle;
}

.name-box {
  display: flex;
  align-items: center;
  gap: 8px;
}

.proxy-icon {
  color: var(--color-accent);
}

.name-text {
  font-weight: 600;
  color: var(--color-text);
}

.proto-tag {
  display: inline-block;
  padding: 2px 6px;
  background-color: var(--color-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 600;
  color: var(--color-blue);
}

.server-text {
  color: var(--color-muted);
  font-size: 12px;
}

.auth-saved {
  color: var(--color-accent);
  font-size: 12px;
}

.auth-none {
  color: var(--color-subtle);
  font-size: 12px;
}

.linked-count {
  color: var(--color-muted);
  font-size: 12px;
}

.td-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.icon-btn {
  padding: 6px;
  border-radius: var(--radius-sm);
  color: var(--color-muted);
}
.icon-btn:hover {
  color: var(--color-text);
}
.icon-btn.btn-danger:hover {
  color: var(--color-danger);
}

.empty-cell {
  text-align: center;
  padding: 48px !important;
  color: var(--color-subtle);
}
</style>
