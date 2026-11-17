<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  apiGetAdmin,
  apiCreateMember,
  apiSetMemberActive,
  apiCreateGroup,
  apiDeleteGroup,
  apiSetGroupMembers,
  apiSetGroupGrants,
} from '../api/team'
import { useNotificationStore } from '../stores/notification'
import { useProfilesStore } from '../stores/profiles'
import Button from '../components/common/Button.vue'
import Badge from '../components/common/Badge.vue'
import Modal from '../components/common/Modal.vue'
import MemberGrantModal from '../components/team/MemberGrantModal.vue'
import ResetPasswordModal from '../components/team/ResetPasswordModal.vue'
import type { Member, MemberGroup, AdminData } from '../types/member'
import {
  UserPlus,
  Users,
  Shield,
  KeyRound,
  Trash2,
  RefreshCw,
  FolderPlus,
} from 'lucide-vue-next'

const notification = useNotificationStore()
const profilesStore = useProfilesStore()

const activeTab = ref<'members' | 'groups' | 'audit'>('members')
const loading = ref(false)

const adminData = ref<AdminData>({
  members: [],
  groups: [],
  audit: [],
})

// Modals
const showAddMemberModal = ref(false)
const newUsername = ref('')
const newDisplay = ref('')
const newRole = ref<'operator' | 'viewer'>('operator')
const newPassword = ref('')
const addMemberLoading = ref(false)
const addMemberError = ref('')

const showResetPasswordModal = ref(false)
const targetResetMember = ref<Member | null>(null)

const showMemberGrantModal = ref(false)
const targetGrantMember = ref<Member | null>(null)

// Group modals
const showCreateGroupModal = ref(false)
const newGroupName = ref('')
const createGroupLoading = ref(false)

const showGroupMembersModal = ref(false)
const editingGroup = ref<MemberGroup | null>(null)
const selectedGroupMemberIds = ref<Set<string>>(new Set())
const groupMembersSaving = ref(false)

const showGroupGrantModal = ref(false)
const editingGrantGroup = ref<MemberGroup | null>(null)
const groupGrantsMap = ref<Record<string, any>>({})
const groupGrantsSaving = ref(false)

async function loadData() {
  loading.value = true
  try {
    adminData.value = await apiGetAdmin()
  } catch (err: any) {
    notification.error(err.message || '加载团队数据失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadData()
  if (profilesStore.profiles.length === 0) {
    profilesStore.fetchProfiles()
  }
})

// Member actions
async function onToggleActive(member: Member) {
  const next = !member.active
  try {
    await apiSetMemberActive(member.id, next)
    notification.success(`成员 "${member.display}" 已${next ? '启用' : '停用'}`)
    await loadData()
  } catch (err: any) {
    notification.error(err.message || '操作失败')
  }
}

function openResetPassword(member: Member) {
  targetResetMember.value = member
  showResetPasswordModal.value = true
}

function openMemberGrants(member: Member) {
  targetGrantMember.value = member
  showMemberGrantModal.value = true
}

async function onSubmitAddMember() {
  addMemberError.value = ''
  if (!newUsername.value.trim() || !newDisplay.value.trim()) {
    addMemberError.value = '请填写完整的用户名和显示名'
    return
  }
  if (!newPassword.value || newPassword.value.length < 12) {
    addMemberError.value = '初始密码长度至少需要 12 个字符'
    return
  }

  addMemberLoading.value = true
  try {
    await apiCreateMember({
      name: newUsername.value.trim(),
      display: newDisplay.value.trim(),
      role: newRole.value,
      password: newPassword.value,
    })
    notification.success('成员创建成功')
    showAddMemberModal.value = false
    newUsername.value = ''
    newDisplay.value = ''
    newPassword.value = ''
    await loadData()
  } catch (err: any) {
    addMemberError.value = err.message || '创建成员失败'
  } finally {
    addMemberLoading.value = false
  }
}

// Group actions
async function onSubmitCreateGroup() {
  if (!newGroupName.value.trim()) return
  createGroupLoading.value = true
  try {
    await apiCreateGroup(newGroupName.value.trim())
    notification.success('成员组创建成功')
    showCreateGroupModal.value = false
    newGroupName.value = ''
    await loadData()
  } catch (err: any) {
    notification.error(err.message || '创建成员组失败')
  } finally {
    createGroupLoading.value = false
  }
}

async function onDeleteGroup(group: MemberGroup) {
  const confirmed = confirm(`确定要删除成员组 "${group.name}"？`)
  if (!confirmed) return
  try {
    await apiDeleteGroup(group.id, group.revision)
    notification.success('成员组已删除')
    await loadData()
  } catch (err: any) {
    notification.error(err.message || '删除成员组失败')
  }
}

function openGroupMembers(group: MemberGroup) {
  editingGroup.value = group
  selectedGroupMemberIds.value = new Set(group.members || [])
  showGroupMembersModal.value = true
}

function toggleGroupMember(memberId: string) {
  if (selectedGroupMemberIds.value.has(memberId)) {
    selectedGroupMemberIds.value.delete(memberId)
  } else {
    selectedGroupMemberIds.value.add(memberId)
  }
}

async function onSaveGroupMembers() {
  if (!editingGroup.value) return
  groupMembersSaving.value = true
  try {
    await apiSetGroupMembers(
      editingGroup.value.id,
      editingGroup.value.revision,
      Array.from(selectedGroupMemberIds.value),
    )
    notification.success('组成员已更新')
    showGroupMembersModal.value = false
    await loadData()
  } catch (err: any) {
    notification.error(err.message || '更新组成员失败')
  } finally {
    groupMembersSaving.value = false
  }
}

function openGroupGrants(group: MemberGroup) {
  editingGrantGroup.value = group
  groupGrantsMap.value = { ...(group.grants || {}) }
  showGroupGrantModal.value = true
}

function toggleGroupPerm(profileId: string, perm: string) {
  const cur = groupGrantsMap.value[profileId] || []
  const next = [...cur]
  const idx = next.indexOf(perm)
  if (idx !== -1) next.splice(idx, 1)
  else next.push(perm)
  groupGrantsMap.value[profileId] = next
}

async function onSaveGroupGrants() {
  if (!editingGrantGroup.value) return
  groupGrantsSaving.value = true
  try {
    await apiSetGroupGrants(
      editingGrantGroup.value.id,
      editingGrantGroup.value.revision,
      groupGrantsMap.value,
    )
    notification.success('组环境授权已更新')
    showGroupGrantModal.value = false
    await loadData()
  } catch (err: any) {
    notification.error(err.message || '更新组授权失败')
  } finally {
    groupGrantsSaving.value = false
  }
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    const m = (d.getMonth() + 1).toString().padStart(2, '0')
    const day = d.getDate().toString().padStart(2, '0')
    const h = d.getHours().toString().padStart(2, '0')
    const min = d.getMinutes().toString().padStart(2, '0')
    return `${m}-${day} ${h}:${min}`
  } catch {
    return iso
  }
}
</script>

<template>
  <div class="team-view">
    <div class="view-header">
      <div>
        <h1 class="view-title">团队管理</h1>
        <p class="view-desc">
          配置团队成员、成员角色权限、环境授权组与审计日志。
        </p>
      </div>

      <div class="header-actions">
        <Button variant="default" :loading="loading" @click="loadData">
          <RefreshCw :size="15" />
          <span>刷新</span>
        </Button>
      </div>
    </div>

    <!-- Tab navigation -->
    <div class="tab-nav">
      <button
        class="tab-btn"
        :class="{ active: activeTab === 'members' }"
        type="button"
        @click="activeTab = 'members'"
      >
        <Users :size="16" />
        <span>成员列表 ({{ adminData.members.length }})</span>
      </button>

      <button
        class="tab-btn"
        :class="{ active: activeTab === 'groups' }"
        type="button"
        @click="activeTab = 'groups'"
      >
        <Shield :size="16" />
        <span>成员组 ({{ adminData.groups.length }})</span>
      </button>

      <button
        class="tab-btn"
        :class="{ active: activeTab === 'audit' }"
        type="button"
        @click="activeTab = 'audit'"
      >
        <span>管理审计 ({{ adminData.audit.length }})</span>
      </button>
    </div>

    <!-- Tab 1: Members -->
    <div v-show="activeTab === 'members'" class="tab-panel">
      <div class="panel-header">
        <h3 class="panel-title">工作区成员</h3>
        <Button variant="primary" size="compact" @click="showAddMemberModal = true">
          <UserPlus :size="14" />
          <span>新增成员</span>
        </Button>
      </div>

      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>显示名称</th>
              <th>角色</th>
              <th>状态</th>
              <th>授权版本</th>
              <th class="th-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in adminData.members" :key="m.id">
              <td class="font-bold">{{ m.name }}</td>
              <td>{{ m.display }}</td>
              <td>
                <Badge
                  :variant="m.role === 'admin' ? 'accent' : m.role === 'operator' ? 'blue' : 'muted'"
                  :label="m.role === 'admin' ? '管理员' : m.role === 'operator' ? '操作员' : '观察员'"
                />
              </td>
              <td>
                <span :class="m.active ? 'status-active' : 'status-disabled'">
                  {{ m.active ? '正常' : '已停用' }}
                </span>
              </td>
              <td class="font-mono">v{{ m.permission_version }}</td>
              <td class="td-actions">
                <template v-if="m.role !== 'admin'">
                  <Button variant="ghost" size="compact" @click="openMemberGrants(m)">
                    <Shield :size="13" />
                    <span>直接授权</span>
                  </Button>
                  <Button variant="ghost" size="compact" @click="openResetPassword(m)">
                    <KeyRound :size="13" />
                    <span>重置密码</span>
                  </Button>
                  <Button
                    variant="ghost"
                    size="compact"
                    :class="{ 'btn-danger': m.active }"
                    @click="onToggleActive(m)"
                  >
                    <span>{{ m.active ? '停用' : '启用' }}</span>
                  </Button>
                </template>
                <span v-else class="admin-tag">主管理员</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Tab 2: Groups -->
    <div v-show="activeTab === 'groups'" class="tab-panel">
      <div class="panel-header">
        <h3 class="panel-title">成员组与组授权</h3>
        <Button variant="primary" size="compact" @click="showCreateGroupModal = true">
          <FolderPlus :size="14" />
          <span>创建成员组</span>
        </Button>
      </div>

      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>组名称</th>
              <th>成员数量</th>
              <th>修订版本</th>
              <th class="th-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in adminData.groups" :key="g.id">
              <td class="font-bold">{{ g.name }}</td>
              <td>{{ g.members?.length || 0 }} 名成员</td>
              <td class="font-mono">v{{ g.revision }}</td>
              <td class="td-actions">
                <Button variant="ghost" size="compact" @click="openGroupMembers(g)">
                  <span>组成员</span>
                </Button>
                <Button variant="ghost" size="compact" @click="openGroupGrants(g)">
                  <Shield :size="13" />
                  <span>组授权</span>
                </Button>
                <Button variant="ghost" size="compact" class="btn-danger" @click="onDeleteGroup(g)">
                  <Trash2 :size="13" />
                  <span>删除</span>
                </Button>
              </td>
            </tr>

            <tr v-if="adminData.groups.length === 0">
              <td colspan="4" class="empty-cell">暂无成员组</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Tab 3: Admin Audit -->
    <div v-show="activeTab === 'audit'" class="tab-panel">
      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>操作</th>
              <th>操作者</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(log, idx) in adminData.audit" :key="log.id || idx">
              <td class="font-mono">{{ formatTime(log.created_at) }}</td>
              <td class="font-bold">{{ log.action }}</td>
              <td>{{ log.actor || '管理员' }}</td>
              <td class="font-mono text-subtle">{{ JSON.stringify(log.detail) }}</td>
            </tr>
            <tr v-if="adminData.audit.length === 0">
              <td colspan="4" class="empty-cell">暂无管理审计记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add Member Modal -->
    <Modal v-model="showAddMemberModal" title="新增团队成员" width="460px">
      <form class="member-form" @submit.prevent="onSubmitAddMember">
        <div v-if="addMemberError" class="form-error">{{ addMemberError }}</div>

        <div class="form-row">
          <label>用户名 (登录账号)</label>
          <input v-model="newUsername" type="text" maxlength="64" required />
        </div>

        <div class="form-row">
          <label>显示名称</label>
          <input v-model="newDisplay" type="text" maxlength="128" required />
        </div>

        <div class="form-row">
          <label>角色分配</label>
          <select v-model="newRole">
            <option value="operator">操作员 (可被授予操作权限)</option>
            <option value="viewer">观察员 (仅允许观看画面)</option>
          </select>
        </div>

        <div class="form-row">
          <label>初始密码（至少 12 个字符）</label>
          <input
            v-model="newPassword"
            type="password"
            minlength="12"
            maxlength="1024"
            required
          />
        </div>

        <div class="form-actions">
          <Button variant="ghost" type="button" @click="showAddMemberModal = false">取消</Button>
          <Button variant="primary" type="submit" :loading="addMemberLoading">创建成员</Button>
        </div>
      </form>
    </Modal>

    <!-- Create Group Modal -->
    <Modal v-model="showCreateGroupModal" title="创建成员组" width="420px">
      <form class="group-form" @submit.prevent="onSubmitCreateGroup">
        <div class="form-row">
          <label>组名</label>
          <input v-model="newGroupName" type="text" maxlength="128" required />
        </div>
        <div class="form-actions">
          <Button variant="ghost" type="button" @click="showCreateGroupModal = false">取消</Button>
          <Button variant="primary" type="submit" :loading="createGroupLoading">确认创建</Button>
        </div>
      </form>
    </Modal>

    <!-- Group Members Modal -->
    <Modal v-model="showGroupMembersModal" :title="`管理组成员 · ${editingGroup?.name}`" width="480px">
      <div class="group-members-list">
        <label
          v-for="m in adminData.members.filter((x) => x.role !== 'admin')"
          :key="m.id"
          class="member-checkbox-row"
        >
          <input
            type="checkbox"
            :checked="selectedGroupMemberIds.has(m.id)"
            @change="toggleGroupMember(m.id)"
          />
          <div class="member-info">
            <span class="m-display">{{ m.display }}</span>
            <span class="m-name">@{{ m.name }}</span>
          </div>
        </label>
      </div>
      <template #footer>
        <Button variant="ghost" type="button" @click="showGroupMembersModal = false">取消</Button>
        <Button variant="primary" type="button" :loading="groupMembersSaving" @click="onSaveGroupMembers">
          保存成员
        </Button>
      </template>
    </Modal>

    <!-- Group Grants Modal -->
    <Modal v-model="showGroupGrantModal" :title="`成员组授权 · ${editingGrantGroup?.name}`" width="600px">
      <div class="group-grants-list">
        <div v-for="p in profilesStore.profiles" :key="p.id" class="grant-row">
          <span class="p-name">{{ p.name }}</span>
          <div class="perm-options">
            <label class="perm-checkbox">
              <input
                type="checkbox"
                :checked="(groupGrantsMap[p.id] || []).includes('view')"
                @change="toggleGroupPerm(p.id, 'view')"
              />
              <span>查看</span>
            </label>
            <label class="perm-checkbox">
              <input
                type="checkbox"
                :checked="(groupGrantsMap[p.id] || []).includes('watch')"
                @change="toggleGroupPerm(p.id, 'watch')"
              />
              <span>观看画面</span>
            </label>
            <label class="perm-checkbox">
              <input
                type="checkbox"
                :checked="(groupGrantsMap[p.id] || []).includes('operate')"
                @change="toggleGroupPerm(p.id, 'operate')"
              />
              <span>控制操作</span>
            </label>
            <label class="perm-checkbox" title="组内只读成员不会获得此权限">
              <input
                type="checkbox"
                :checked="(groupGrantsMap[p.id] || []).includes('checkout')"
                @change="toggleGroupPerm(p.id, 'checkout')"
              />
              <span>本机运行</span>
            </label>
          </div>
        </div>
      </div>
      <template #footer>
        <Button variant="ghost" type="button" @click="showGroupGrantModal = false">取消</Button>
        <Button variant="primary" type="button" :loading="groupGrantsSaving" @click="onSaveGroupGrants">
          保存组授权
        </Button>
      </template>
    </Modal>

    <!-- Member Direct Grants Modal -->
    <MemberGrantModal
      v-model="showMemberGrantModal"
      :member="targetGrantMember"
      @updated="loadData"
    />

    <!-- Reset Password Modal -->
    <ResetPasswordModal
      v-model="showResetPasswordModal"
      :member="targetResetMember"
      @reset="loadData"
    />
  </div>
</template>

<style scoped>
.team-view {
  padding: 30px 32px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  overflow-y: auto;
  height: 100%;
}

.view-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}

.view-title {
  font-size: 28px;
  font-weight: 750;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

.view-desc {
  font-size: 13px;
  color: var(--color-muted);
  margin-top: 4px;
}

.tab-nav {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 8px;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: none;
  color: var(--color-muted);
  font-size: 13px;
  font-weight: 500;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.tab-btn.active {
  color: var(--color-accent);
  background-color: rgba(91, 226, 196, 0.1);
  font-weight: 600;
}

.tab-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
}

.table-container {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
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
}

tbody tr {
  border-bottom: 1px solid var(--color-border-subtle);
  height: 52px;
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

.font-bold {
  font-weight: 600;
  color: var(--color-text);
}

.status-active {
  color: var(--color-accent);
  font-weight: 500;
}
.status-disabled {
  color: var(--color-subtle);
}

.admin-tag {
  color: var(--color-subtle);
  font-size: 12px;
}

.td-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.btn-danger:hover {
  color: var(--color-danger);
}

.empty-cell {
  text-align: center;
  padding: 40px !important;
  color: var(--color-subtle);
}

.member-form,
.group-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.form-error {
  padding: 8px 12px;
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.form-row label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 6px;
}

.group-members-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 240px;
  overflow-y: auto;
}

.member-checkbox-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background-color: var(--color-bg);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.member-info {
  display: flex;
  gap: 8px;
  font-size: 13px;
}
.m-display {
  font-weight: 600;
  color: var(--color-text);
}
.m-name {
  color: var(--color-subtle);
}

.group-grants-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 280px;
  overflow-y: auto;
}

.grant-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background-color: var(--color-bg);
  border-radius: var(--radius-sm);
}

.p-name {
  font-weight: 600;
  color: var(--color-text);
  font-size: 13px;
}

.perm-options {
  display: flex;
  gap: 14px;
}

.perm-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-muted);
  cursor: pointer;
}
</style>
