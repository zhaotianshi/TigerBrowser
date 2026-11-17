<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useProfilesStore } from '../stores/profiles'
import { useProxiesStore } from '../stores/proxies'
import { useAuthStore } from '../stores/auth'
import StatCard from '../components/common/StatCard.vue'
import Button from '../components/common/Button.vue'
import ProfileToolbar from '../components/profiles/ProfileToolbar.vue'
import ProfileTable from '../components/profiles/ProfileTable.vue'
import ProfileEditorModal from '../components/profiles/ProfileEditorModal.vue'
import ProfileDetailsModal from '../components/profiles/ProfileDetailsModal.vue'
import DeleteProfileModal from '../components/profiles/DeleteProfileModal.vue'
import CookieModal from '../components/profiles/CookieModal.vue'
import {
  Globe,
  PlayCircle,
  Radio,
  Plus,
  RefreshCw,
  Search,
} from 'lucide-vue-next'
import type { ProfileSummary } from '../types/profile'

const profilesStore = useProfilesStore()
const proxiesStore = useProxiesStore()
const authStore = useAuthStore()

// Modals
const showEditorModal = ref(false)
const editingProfileId = ref<string | null>(null)

const showDetailsModal = ref(false)
const detailsProfileId = ref<string | null>(null)

const showDeleteModal = ref(false)
const showCookieModal = ref(false)

onMounted(() => {
  profilesStore.fetchProfiles()
  proxiesStore.fetchProxies()
})

function openCreateModal() {
  editingProfileId.value = null
  showEditorModal.value = true
}

function openEditModal() {
  const single = profilesStore.singleSelectedProfile
  if (single) {
    editingProfileId.value = single.id
    showEditorModal.value = true
  }
}

function openDetailsModal(profile?: ProfileSummary) {
  const target = profile || profilesStore.singleSelectedProfile
  if (target) {
    detailsProfileId.value = target.id
    showDetailsModal.value = true
  }
}

function openDeleteModal() {
  if (profilesStore.selectedProfiles.length > 0) {
    showDeleteModal.value = true
  }
}

function openCookieModal() {
  if (profilesStore.singleSelectedProfile) {
    showCookieModal.value = true
  }
}
</script>

<template>
  <div class="profiles-view">
    <!-- Header -->
    <div class="view-header">
      <div>
        <p class="view-kicker">工作区 / 环境管理</p>
        <h1 class="view-title">浏览器环境</h1>
        <p class="view-desc">
          独立环境，固定身份。在此管理浏览器、代理与运行状态。
        </p>
      </div>

      <div class="header-actions">
        <Button
          v-if="authStore.isAdmin"
          variant="primary"
          @click="openCreateModal"
        >
          <Plus :size="16" />
          <span>创建环境</span>
        </Button>

        <Button
          variant="default"
          :loading="profilesStore.loading"
          title="刷新环境列表"
          @click="profilesStore.fetchProfiles()"
        >
          <RefreshCw :size="15" />
          <span>刷新</span>
        </Button>
      </div>
    </div>

    <!-- Metric Cards -->
    <div class="metrics-row">
      <StatCard
        label="全部环境"
        :value="profilesStore.totalCount"
        subtext="工作区保存的环境总数"
        :icon="Globe"
      />
      <StatCard
        label="正在运行"
        :value="profilesStore.runningCount"
        subtext="当前活动实例数"
        :icon="PlayCircle"
        accent
      />
      <StatCard
        label="已绑定代理"
        :value="profilesStore.boundProxyCount"
        subtext="已关联代理资源的环境数"
        :icon="Radio"
      />
    </div>

    <section class="profiles-panel" aria-labelledby="profiles-heading">
      <div class="panel-heading">
        <div class="panel-title">
          <h2 id="profiles-heading">环境列表</h2>
          <span class="count-pill">{{ profilesStore.filteredProfiles.length }}</span>
        </div>
        <span class="panel-hint">常用操作可在每行直接执行</span>
      </div>
      <div class="filters-bar">
        <div class="search-box">
          <Search :size="18" class="search-icon" />
          <input
            v-model="profilesStore.searchQuery"
            type="search"
            aria-label="搜索浏览器环境"
            placeholder="按名称、分组、标签或代理搜索…"
          />
        </div>

        <div class="filter-controls">
          <select v-model="profilesStore.groupFilter" class="group-select" aria-label="按分组筛选">
            <option :value="null">全部分组</option>
            <option
              v-for="grp in profilesStore.availableGroups"
              :key="grp"
              :value="grp"
            >
              {{ grp }}
            </option>
          </select>

          <label class="archived-check">
            <input v-model="profilesStore.showArchived" type="checkbox" />
            <span>显示归档环境</span>
          </label>
        </div>
      </div>

      <ProfileToolbar
        :on-edit="openEditModal"
        :on-details="openDetailsModal"
        :on-delete="openDeleteModal"
        :on-cookies="openCookieModal"
      />

      <ProfileTable :on-row-double-click="openDetailsModal" />
      <div class="table-footer">
        <span>共 {{ profilesStore.filteredProfiles.length }} 个{{ profilesStore.showArchived ? '归档' : '' }}环境</span>
        <span>点击名称查看详情 · 勾选环境进行批量操作</span>
      </div>
    </section>

    <!-- Modals -->
    <ProfileEditorModal
      v-model="showEditorModal"
      :profile-id="editingProfileId"
    />

    <ProfileDetailsModal
      v-model="showDetailsModal"
      :profile-id="detailsProfileId"
    />

    <DeleteProfileModal
      v-model="showDeleteModal"
      :profiles="profilesStore.selectedProfiles"
    />

    <CookieModal
      v-model="showCookieModal"
      :profile-id="profilesStore.singleSelectedProfile?.id || null"
      :profile-name="profilesStore.singleSelectedProfile?.name"
    />
  </div>
</template>

<style scoped>
.profiles-view {
  padding: 28px 32px 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  overflow-y: auto;
  height: 100%;
  background: radial-gradient(ellipse at 90% 0%, var(--color-blue-bg), transparent 45%);
}

.profiles-view > * {
  flex-shrink: 0;
}

.view-kicker {
  font-size: var(--text-xs);
  color: var(--color-subtle);
  margin-bottom: 6px;
}

.view-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.view-title {
  font-size: 2rem;
  font-weight: 750;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

.view-desc {
  font-size: var(--text-sm);
  color: var(--color-muted);
  margin-top: 8px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 26px;
  flex-shrink: 0;
}

.metrics-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.filters-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 0 22px 20px;
}

.search-box {
  position: relative;
  flex: 1;
  max-width: 440px;
  min-width: 240px;
}

.search-icon {
  position: absolute;
  left: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-subtle);
}

.search-box input {
  width: 100%;
  padding-left: 42px;
  min-height: 44px;
}

.filter-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.group-select {
  min-width: 150px;
  min-height: 44px;
}

.archived-check {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  font-size: var(--text-sm);
  color: var(--color-muted);
  cursor: pointer;
  user-select: none;
}

.profiles-panel {
  min-width: 0;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.panel-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 22px;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-title h2 {
  font-size: 1.125rem;
  font-weight: 650;
}

.count-pill {
  padding: 2px 10px;
  border-radius: var(--radius-sm);
  background: var(--color-raised);
  color: var(--color-muted);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}

.panel-hint,
.table-footer {
  color: var(--color-subtle);
  font-size: var(--text-xs);
}

.table-footer {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 16px 22px;
  border-top: 1px solid var(--color-border-subtle);
}

.profiles-panel :deep(.table-container) {
  border-radius: 0;
  border-width: 1px 0 0;
  box-shadow: none;
}

.profiles-panel :deep(.profile-toolbar) {
  margin: 0 22px 20px;
  background: var(--color-table-alt);
}

.profiles-panel :deep(.profile-toolbar.has-selection) {
  background: var(--color-accent-bg);
  border-color: var(--color-accent-border);
}

@media (max-width: 1100px) {
  .profiles-view { padding: 24px; }
  .view-header { flex-wrap: wrap; gap: 16px; }
  .header-actions { padding-top: 0; }
}

@media (max-width: 760px) {
  .profiles-view { padding: 20px 16px; gap: 20px; }
  .view-title { font-size: 1.75rem; }
  .metrics-row { grid-template-columns: 1fr; gap: 12px; }
  .panel-hint { display: none; }
  .panel-heading { padding: 18px 16px; }
  .filters-bar { padding: 0 16px 16px; }
  .search-box { max-width: none; min-width: 0; flex-basis: 100%; }
  .filter-controls { gap: 8px; }
  .profiles-panel :deep(.profile-toolbar) { margin: 0 16px 16px; }
}
</style>
