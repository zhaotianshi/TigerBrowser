<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useProxiesStore } from '../stores/proxies'
import { useNotificationStore } from '../stores/notification'
import Button from '../components/common/Button.vue'
import Modal from '../components/common/Modal.vue'
import ProxyTable from '../components/proxies/ProxyTable.vue'
import ProxyEditorModal from '../components/proxies/ProxyEditorModal.vue'
import ProxyImportModal from '../components/proxies/ProxyImportModal.vue'
import ProxyProbeModal from '../components/proxies/ProxyProbeModal.vue'
import {
  Plus,
  FileInput,
  RefreshCw,
  Search,
} from 'lucide-vue-next'
import { apiDeleteProxy } from '../../src/api/proxies'
import type { ProxyItem } from '../types/proxy'

const proxiesStore = useProxiesStore()
const notification = useNotificationStore()

// Modals
const showEditorModal = ref(false)
const editingProxy = ref<ProxyItem | null>(null)

const showImportModal = ref(false)

const showProbeModal = ref(false)
const probingProxy = ref<ProxyItem | null>(null)

const showDeleteModal = ref(false)
const deletingProxy = ref<ProxyItem | null>(null)
const deleteLoading = ref(false)

onMounted(() => {
  proxiesStore.fetchProxies()
})

function openCreate() {
  editingProxy.value = null
  showEditorModal.value = true
}

function openEdit(proxy: ProxyItem) {
  editingProxy.value = proxy
  showEditorModal.value = true
}

function openProbe(proxy: ProxyItem) {
  probingProxy.value = proxy
  showProbeModal.value = true
}

function openDelete(proxy: ProxyItem) {
  deletingProxy.value = proxy
  showDeleteModal.value = true
}

async function onConfirmDelete() {
  if (!deletingProxy.value) return
  deleteLoading.value = true
  try {
    await apiDeleteProxy(deletingProxy.value.id)
    notification.success(`代理 "${deletingProxy.value.name}" 已成功删除`)
    showDeleteModal.value = false
    await proxiesStore.fetchProxies()
  } catch (err: any) {
    notification.error(err.message || '删除代理失败')
  } finally {
    deleteLoading.value = false
  }
}
</script>

<template>
  <div class="proxies-view">
    <!-- Header -->
    <div class="view-header">
      <div>
        <h1 class="view-title">代理资源</h1>
        <p class="view-desc">
          管理工作区共享的 HTTP、HTTPS 和 SOCKS5 代理。
        </p>
      </div>

      <div class="header-actions">
        <Button variant="primary" @click="openCreate">
          <Plus :size="16" />
          <span>添加代理</span>
        </Button>

        <Button variant="default" @click="showImportModal = true">
          <FileInput :size="15" />
          <span>批量导入</span>
        </Button>

        <Button
          variant="default"
          :loading="proxiesStore.loading"
          title="刷新列表"
          @click="proxiesStore.fetchProxies()"
        >
          <RefreshCw :size="15" />
        </Button>
      </div>
    </div>

    <!-- Filters Bar -->
    <div class="filters-bar">
      <div class="search-box">
        <Search :size="15" class="search-icon" />
        <input
          v-model="proxiesStore.searchQuery"
          type="search"
          placeholder="按名称、服务器地址或用户名搜索…"
        />
      </div>
    </div>

    <!-- Table -->
    <ProxyTable
      :on-edit="openEdit"
      :on-delete="openDelete"
      :on-probe="openProbe"
    />

    <!-- Modals -->
    <ProxyEditorModal
      v-model="showEditorModal"
      :proxy="editingProxy"
    />

    <ProxyImportModal v-model="showImportModal" />

    <ProxyProbeModal
      v-model="showProbeModal"
      :proxy="probingProxy"
    />

    <!-- Delete Confirmation Modal -->
    <Modal v-model="showDeleteModal" title="删除代理" width="440px">
      <div class="delete-confirm">
        <p>确定要删除代理 <strong>{{ deletingProxy?.name }}</strong>？</p>
        <p class="delete-hint">若有正在运行的环境引用此代理，需先停止这些环境。</p>
      </div>
      <template #footer>
        <Button variant="ghost" type="button" @click="showDeleteModal = false">取消</Button>
        <Button variant="danger" type="button" :loading="deleteLoading" @click="onConfirmDelete">确认删除</Button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.proxies-view {
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

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.filters-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.search-box {
  position: relative;
  flex: 1;
  max-width: 360px;
}

.search-icon {
  position: absolute;
  left: 11px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-subtle);
}

.search-box input {
  width: 100%;
  padding-left: 34px;
}

.delete-confirm {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 13px;
  color: var(--color-text);
}

.delete-hint {
  font-size: 12px;
  color: var(--color-subtle);
}
</style>
