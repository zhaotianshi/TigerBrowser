<script setup lang="ts">
import { computed } from 'vue'
import { useProfilesStore } from '../../stores/profiles'
import { useDesktopStore } from '../../stores/desktop'
import Badge from '../common/Badge.vue'
import Button from '../common/Button.vue'
import { openProfileWindow } from '../../utils/profile-window'
import type { ProfileSummary } from '../../types/profile'
import { ERROR_MESSAGES } from '../../types/api'
import {
  Monitor,
  Apple,
  Terminal,
  ExternalLink,
  Play,
  EyeOff,
  Square,
  Globe,
} from 'lucide-vue-next'

const props = defineProps<{
  onRowDoubleClick: (profile: ProfileSummary) => void
}>()

const profilesStore = useProfilesStore()
const desktopStore = useDesktopStore()

const LOCAL_TEXT: Record<string, string> = {
  starting: '本机启动中',
  running: '本机运行中',
  stopping: '本机关闭中',
  syncing: '正在下载到本机',
  pending_return: '等待签回服务器',
  revoked: '已被强制解锁，本机副本将停用',
}

// Local state merges with the server's: this computer knows its browser and lease, the server knows the holder.
function localText(profile: ProfileSummary): string {
  const state = desktopStore.state(profile.id)
  if (!state) return ''
  if (state.recovery_required) return '本机会话需要人工恢复，数据暂不签回'
  return LOCAL_TEXT[state.browser] || (state.lease ? LOCAL_TEXT[state.lease] || '已签出到本机' : '')
}

function localError(profile: ProfileSummary): string {
  const code = desktopStore.state(profile.id)?.sync_error
  return code ? ERROR_MESSAGES[code] || `同步失败 (${code})` : ''
}

const allFiltered = computed(() => profilesStore.filteredProfiles)
const selectedIds = computed(() => profilesStore.selectedIds)

const isAllSelected = computed(() => {
  if (allFiltered.value.length === 0) return false
  return allFiltered.value.every((p) => selectedIds.value.has(p.id))
})

const isIndeterminate = computed(() => {
  const count = allFiltered.value.filter((p) => selectedIds.value.has(p.id)).length
  return count > 0 && count < allFiltered.value.length
})

function onToggleAll(e: Event) {
  const checked = (e.target as HTMLInputElement).checked
  if (checked) {
    profilesStore.selectAll()
  } else {
    profilesStore.clearSelection()
  }
}

function onRowClick(e: MouseEvent, profile: ProfileSummary) {
  if (e.ctrlKey || e.metaKey) {
    profilesStore.toggleSelect(profile.id)
  } else if (e.shiftKey) {
    profilesStore.selectRange(profile.id, allFiltered.value)
  } else {
    profilesStore.selectOne(profile.id)
  }
}

function formatLastLaunch(timestamp: string | null): string {
  if (!timestamp) return '尚未启动'
  try {
    const d = new Date(timestamp)
    const m = (d.getMonth() + 1).toString().padStart(2, '0')
    const day = d.getDate().toString().padStart(2, '0')
    const h = d.getHours().toString().padStart(2, '0')
    const min = d.getMinutes().toString().padStart(2, '0')
    return `${m}-${day} ${h}:${min}`
  } catch {
    return timestamp
  }
}

</script>

<template>
  <div class="table-container" tabindex="0" role="region" aria-label="浏览器环境列表">
    <table class="profile-table" aria-label="浏览器环境">
      <thead>
        <tr>
          <th class="th-select">
            <input
              type="checkbox"
              aria-label="选择全部可见环境"
              :checked="isAllSelected"
              :indeterminate="isIndeterminate"
              @change="onToggleAll"
            />
          </th>
          <th class="th-name">环境名称</th>
          <th class="th-status">状态</th>
          <th class="th-os">系统 / 语言</th>
          <th class="th-proxy">代理</th>
          <th class="th-group">分组 / 标签</th>
          <th class="th-time">上次启动</th>
          <th class="th-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="profile in allFiltered"
          :key="profile.id"
          :class="{
            'is-selected': selectedIds.has(profile.id),
            'is-running': profile.run?.state === 'running',
          }"
          @click="onRowClick($event, profile)"
          @dblclick="props.onRowDoubleClick(profile)"
        >
          <td class="td-select" @click.stop>
            <input
              type="checkbox"
              :aria-label="`选择 ${profile.name}`"
              :checked="selectedIds.has(profile.id)"
              @change="profilesStore.toggleSelect(profile.id)"
            />
          </td>

          <td class="td-name">
            <div class="name-wrapper">
              <button class="ghost profile-name" :title="profile.name" @click.stop="props.onRowDoubleClick(profile)">{{ profile.name }}</button>
            </div>
          </td>

          <td class="td-status" data-label="状态">
            <Badge
              :status="profile.run?.state === 'stopped' && profile.run.error ? 'error' : profile.run?.state || 'stopped'"
              :label="profile.run?.mode === 'headless' ? `无头 · ${profile.run.error && profile.run.state === 'stopped' ? '启动失败' : profile.run.state === 'running' ? '运行中' : profile.run.state === 'starting' ? '启动中' : '未运行'}` : undefined"
              :title="profile.run?.error ? ERROR_MESSAGES[profile.run.error] || ERROR_MESSAGES.browser_failed : undefined"
            />
            <div v-if="profile.run?.state === 'stopped' && profile.run.error" class="run-error">
              {{ ERROR_MESSAGES[profile.run.error] || ERROR_MESSAGES.browser_failed }}
            </div>
            <div v-if="localText(profile)" class="local-state">{{ localText(profile) }}</div>
            <div
              v-else-if="profile.checked_out"
              class="holder"
              :title="`最后在线 ${formatLastLaunch(profile.checked_out.heartbeat_at)}`"
            >
              {{ profile.checked_out.mine ? '已签出到我的另一台设备' : '正在成员电脑运行' }}：{{ profile.checked_out.member_name }} · {{ profile.checked_out.device_name }}
            </div>
            <div v-if="localError(profile)" class="run-error">{{ localError(profile) }}</div>
          </td>

          <td class="td-os" data-label="系统 / 语言">
            <div class="os-info">
              <Monitor v-if="profile.os === 'windows'" :size="14" class="os-icon" />
              <Apple v-else-if="profile.os === 'macos'" :size="14" class="os-icon" />
              <Terminal v-else :size="14" class="os-icon" />
              <div class="os-text"><span>{{ profile.os === 'macos' ? 'macOS' : profile.os === 'windows' ? 'Windows' : 'Linux' }}</span><span class="locale-text">{{ profile.locale }}</span></div>
            </div>
          </td>

          <td class="td-proxy" data-label="代理">
            <span v-if="profile.proxy" class="proxy-text" :title="profile.proxy.server || profile.proxy.name">
              {{ profile.proxy.name }}
            </span>
            <span v-else class="direct-text">直连</span>
          </td>

          <td class="td-group" data-label="分组 / 标签">
            <div class="group-tags">
              <span v-if="profile.group_name" class="group-pill">{{ profile.group_name }}</span>
              <span v-for="tag in profile.tags.slice(0, 2)" :key="tag" class="tag-pill">
                {{ tag }}
              </span>
              <span v-if="profile.tags.length > 2" class="tag-more">
                +{{ profile.tags.length - 2 }}
              </span>
              <span v-if="!profile.group_name && profile.tags.length === 0" class="subtle-text">
                未分组
              </span>
            </div>
          </td>

          <td class="td-time" data-label="上次启动">
            <span class="time-text">{{ formatLastLaunch(profile.last_launch_at) }}</span>
          </td>

          <td class="td-actions" @click.stop @dblclick.stop>
            <div class="row-actions">
              <Button
                class="row-start"
                size="compact"
                :aria-label="`启动 ${profile.name}`"
                :title="profilesStore.startDisabledReason(profile) || '启动此环境'"
                :disabled="!!profilesStore.startDisabledReason(profile)"
                :loading="profilesStore.pendingActions[profile.id] === 'start'"
                @click="profilesStore.startProfiles([profile])"
              ><Play :size="15" /><span>启动</span></Button>
              <Button
                size="compact"
                :aria-label="`无头启动 ${profile.name}`"
                :title="profilesStore.startDisabledReason(profile, true) || '在后台无头启动，不开启远程图形'"
                :disabled="!!profilesStore.startDisabledReason(profile, true)"
                :loading="profilesStore.pendingActions[profile.id] === 'headless_start'"
                @click="profilesStore.startProfiles([profile], true)"
              ><EyeOff :size="15" /><span>无头启动</span></Button>
              <Button
                variant="danger"
                size="compact"
                :aria-label="`停止 ${profile.name}`"
                :title="profilesStore.stopDisabledReason(profile) || '停止此环境的当前运行'"
                :disabled="!!profilesStore.stopDisabledReason(profile)"
                :loading="profilesStore.pendingActions[profile.id] === 'stop'"
                @click="profilesStore.stopProfiles([profile])"
              ><Square :size="14" /><span>停止</span></Button>
              <Button
                v-if="profile.run?.state === 'running' && profile.run.mode === 'remote'"
                variant="ghost"
                size="compact"
                class="quick-btn"
                title="打开操作窗口"
                :aria-label="`打开 ${profile.name} 的操作窗口`"
                @click="openProfileWindow(profile.id)"
              ><ExternalLink :size="17" /></Button>
            </div>
          </td>
        </tr>

        <tr v-if="allFiltered.length === 0">
          <td colspan="8" class="empty-cell">
            <div class="empty-state">
              <Globe :size="32" aria-hidden="true" />
              <p>没有匹配的浏览器环境</p>
              <span>调整搜索条件，或创建一个新环境。</span>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-container {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: auto;
  box-shadow: var(--shadow-sm);
}

.profile-table {
  width: 100%;
  min-width: 1120px;
  border-collapse: collapse;
  text-align: left;
  font-size: var(--text-base);
  user-select: none;
}

thead {
  background-color: var(--color-table-head);
  border-bottom: 1px solid var(--color-border);
}

th {
  padding: 16px 14px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-muted);
  white-space: nowrap;
}

th.th-select,
td.td-select {
  width: 44px;
  text-align: center;
  padding-left: 16px;
  padding-right: 8px;
}

tbody tr {
  border-bottom: 1px solid var(--color-border-subtle);
  height: 76px;
  background-color: var(--color-surface);
  transition: background-color 0.18s ease;
  cursor: pointer;
}

tbody tr:nth-child(even) {
  background-color: var(--color-table-alt);
}

tbody tr:hover {
  background-color: var(--color-raised);
}

tbody tr.is-selected {
  background-color: var(--color-selected);
}

tbody tr:last-child { border-bottom: 0; }

td {
  padding: 14px;
  vertical-align: middle;
}

.name-wrapper {
  display: flex;
  align-items: center;
  gap: 8px;
}

.profile-name {
  display: block;
  padding: 4px 0;
  min-height: 44px;
  border: 0;
  text-align: left;
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

button.profile-name:hover { background: transparent; color: var(--color-accent); }

.os-info {
  display: flex;
  align-items: center;
  gap: 9px;
  color: var(--color-muted);
}

.run-error {
  margin-top: 5px;
  max-width: 320px;
  color: var(--color-danger);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.local-state,
.holder {
  margin-top: 5px;
  max-width: 320px;
  font-size: var(--text-xs);
  line-height: 1.5;
}

.local-state {
  color: var(--color-accent);
}

.holder {
  color: var(--color-warning);
}

.os-icon {
  color: var(--color-subtle);
  flex-shrink: 0;
}

.os-text { display: grid; gap: 2px; }
.locale-text { font-size: var(--text-xs); color: var(--color-subtle); }

.proxy-text {
  display: block;
  max-width: 150px;
  overflow-wrap: anywhere;
  color: var(--color-blue);
  font-weight: 500;
}

.direct-text {
  color: var(--color-subtle);
}

.group-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.group-pill {
  padding: 2px 7px;
  background-color: var(--color-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--color-text);
  font-weight: 500;
}

.tag-pill {
  padding: 2px 6px;
  background-color: rgba(173, 188, 208, 0.08);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--color-muted);
}

.tag-more {
  font-size: var(--text-xs);
  color: var(--color-subtle);
}

.subtle-text {
  color: var(--color-subtle);
  font-size: var(--text-xs);
}

.time-text {
  color: var(--color-muted);
  font-feature-settings: 'tnum';
  font-size: var(--text-xs);
  white-space: nowrap;
}

.th-actions, .td-actions {
  position: sticky;
  right: 0;
  z-index: 1;
  background: inherit;
  border-left: 1px solid var(--color-border-subtle);
}

.th-actions { background: var(--color-table-head); }
.row-actions { display: flex; align-items: center; gap: 8px; min-width: 310px; }
.row-actions button { min-height: 44px; white-space: nowrap; }
.row-start:not(:disabled) {
  background: var(--color-accent-bg);
  border-color: var(--color-accent-border);
  color: var(--color-accent);
}
.row-start:hover:not(:disabled) { background: var(--color-selected); border-color: var(--color-accent); }
.row-actions .quick-btn { padding: 8px; min-width: 40px; color: var(--color-accent); }

.empty-cell {
  text-align: center;
  padding: 48px !important;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: var(--color-subtle);
}

.empty-state p { color: var(--color-text); font-size: var(--text-base); }
.empty-state span { font-size: var(--text-sm); }

@media (max-width: 900px) {
  .th-actions, .td-actions { position: static; }
}

@media (max-width: 760px) {
  .profile-table { min-width: 0; }
  thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  tbody { display: block; }
  tbody tr {
    display: grid;
    grid-template-columns: 26px minmax(0, 1fr);
    gap: 10px 8px;
    height: auto;
    padding: 16px 12px;
  }
  td {
    display: block;
    grid-column: 2;
    padding: 0;
  }
  td.td-select {
    grid-column: 1;
    width: auto;
    padding: 12px 0;
  }
  .profile-name { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  td[data-label]::before {
    content: attr(data-label);
    display: block;
    margin-bottom: 4px;
    color: var(--color-subtle);
    font-size: var(--text-xs);
  }
  .td-os, .td-proxy, .td-group, .td-time {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
  }
  .os-text { display: flex; align-items: baseline; gap: 8px; }
  .proxy-text { max-width: 100%; }
  .td-actions, .empty-cell {
    grid-column: 1 / -1;
    border: 0;
  }
  .row-actions { flex-wrap: wrap; min-width: 0; margin-top: 6px; gap: 6px; }
  .row-actions button { padding: 7px 9px; font-size: var(--text-xs); }
}
</style>
