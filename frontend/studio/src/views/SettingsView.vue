<script setup lang="ts">
import { downloadBlob } from '../utils/download'
import { ref, onMounted } from 'vue'
import {
  apiGetSettings,
  apiSaveSettings,
  apiGetConfiguration,
  apiImportConfiguration,
  apiGetKernels,
  apiActivateKernel,
  apiKernelCatalog,
  apiCacheKernel,
  apiRemoveKernel,
} from '../api/settings'
import { useNotificationStore } from '../stores/notification'
import Button from '../components/common/Button.vue'
import Badge from '../components/common/Badge.vue'
import { generateRequestId } from '../types/api'
import { waitForTask } from '../api/tasks'
import type { AvailableKernel, CachedKernel, SettingsData, KernelItem } from '../types/health'
import {
  Cpu,
  Layers,
  FileJson,
  Download,
  Upload,
  CheckCircle,
  Save,
} from 'lucide-vue-next'

const notification = useNotificationStore()

const loading = ref(false)
const saving = ref(false)
const settingsReady = ref(false)
const savedSettings = ref<SettingsData | null>(null)

const settings = ref<SettingsData>({
  executable_path: '',
  max_running: 5,
  automation_enabled: false,
})

const kernels = ref<KernelItem[]>([])
const cachedKernels = ref<CachedKernel[] | null>(null)
const catalog = ref<AvailableKernel[]>([])
const kernelsLoading = ref(false)
const catalogLoading = ref(false)
const kernelBusy = ref('')

async function loadData() {
  loading.value = true
  try {
    const s = await apiGetSettings()
    settings.value = s
    savedSettings.value = { ...s }
    settingsReady.value = true
    await loadKernels()
  } catch (err: any) {
    notification.error(err.message || '加载设置失败')
  } finally {
    loading.value = false
  }
}

async function loadKernels() {
  kernelsLoading.value = true
  try {
    const inventory = await apiGetKernels()
    kernels.value = inventory.installed
    cachedKernels.value = inventory.cache
  } catch (err: any) {
    notification.error(err.message || '读取内核失败')
  } finally {
    kernelsLoading.value = false
  }
}

onMounted(() => {
  loadData()
})

async function onSaveSettings() {
  if (!settingsReady.value || loading.value || !savedSettings.value) return
  saving.value = true
  try {
    const changes: Partial<SettingsData> = {
      max_running: Number(settings.value.max_running),
    }
    const saved = await apiSaveSettings(changes)
    settings.value = saved
    savedSettings.value = { ...saved }
    notification.success('设置已保存，将在之后启动的环境中生效。')
  } catch (err: any) {
    notification.error(err.message || '保存设置失败')
  } finally {
    saving.value = false
  }
}

async function onActivateKernel(kernelId: string) {
  try {
    await waitForTask(await apiActivateKernel(generateRequestId(), kernelId))
    notification.success('已设置新环境默认内核')
    await loadKernels()
  } catch (err: any) {
    notification.error(err.message || '设置默认内核失败')
  }
}

async function onLoadCatalog() {
  catalogLoading.value = true
  try {
    const task = await waitForTask(await apiKernelCatalog(generateRequestId()))
    catalog.value = task.result?.kernels ?? []
  } catch (err: any) {
    notification.error(err.message || '读取官方版本失败')
  } finally {
    catalogLoading.value = false
  }
}

// Four platform packages take minutes on a typical LAN uplink.
const KERNEL_TASK_TIMEOUT_MS = 30 * 60 * 1000

async function onKernelTask(kernelId: string, operation: typeof apiCacheKernel, done: string) {
  kernelBusy.value = kernelId
  try {
    await waitForTask(await operation(generateRequestId(), kernelId), undefined, KERNEL_TASK_TIMEOUT_MS)
    notification.success(done)
    await loadKernels()
    catalog.value = catalog.value.map(item => item.kernel_id === kernelId
      ? { ...item, cached: operation === apiCacheKernel } : item)
  } catch (err: any) {
    notification.error(err.message || '内核任务失败')
    await loadKernels()
  } finally {
    kernelBusy.value = ''
  }
}

const onCacheKernel = (kernelId: string) => onKernelTask(kernelId, apiCacheKernel, '已缓存全部平台安装包')
const onRemoveKernel = (kernelId: string) => onKernelTask(kernelId, apiRemoveKernel, '已删除该内核')

async function onExportConfig() {
  try {
    const config = await apiGetConfiguration()
    const jsonStr = JSON.stringify(config, null, 2)
    const blob = new Blob([jsonStr], { type: 'application/json' })
    downloadBlob(blob, 'camoufox-studio-config.json')
    notification.success('工作区配置已导出（不含代理凭据与网站数据）')
  } catch (err: any) {
    notification.error(err.message || '导出配置失败')
  }
}

async function onImportConfig(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return

  const confirmed = confirm(
    '导入工作区配置将在全部环境停止状态下创建新环境与代理。\n确定要导入？',
  )
  if (!confirmed) return

  try {
    const text = await file.text()
    const parsed = JSON.parse(text)
    await apiImportConfiguration(generateRequestId(), parsed)
    notification.success('配置导入任务已提交处理')
  } catch (err: any) {
    notification.error(err.message || '导入配置失败')
  }
}
</script>

<template>
  <div class="settings-view">
    <div class="view-header">
      <div>
        <h1 class="view-title">工作区设置</h1>
        <p class="view-desc">
          配置固定执行机引擎参数、并发上限、内核管理与工作区整体配置导入导出。
        </p>
      </div>
    </div>

    <div class="settings-grid">
      <!-- Section 1: Engine Settings -->
      <section class="settings-card">
        <div class="card-header">
          <Cpu :size="20" class="header-icon" />
          <h2 class="card-title">浏览器与执行机引擎</h2>
        </div>

        <form class="card-form" @submit.prevent="onSaveSettings">
          <div class="form-row">
            <label class="form-label">最大并发运行环境数</label>
            <input
              v-model.number="settings.max_running"
              type="number"
              min="1"
              max="64"
              required
            />
          </div>

          <div class="card-actions">
            <Button variant="primary" type="submit" :loading="saving" :disabled="!settingsReady || loading">
              <Save :size="14" />
              <span>保存引擎设置</span>
            </Button>
          </div>
        </form>
      </section>

      <!-- Section 2: Kernel Management -->
      <section class="settings-card">
        <div class="card-header">
          <Layers :size="20" class="header-icon" />
          <h2 class="card-title">内核管理</h2>
        </div>

        <div class="card-content">
          <p class="card-desc">
            默认内核只用于新建环境，已有环境按各自固定版本启动。缓存一个官方版本会下载 Windows、macOS（两种芯片）和
            Linux 四个平台的安装包并校验 SHA-256，客户端本机运行时从服务器取得，不需要访问 GitHub。
          </p>

          <div class="kernels-list">
            <div v-for="k in kernels" :key="k.id" class="kernel-item">
              <div class="kernel-info">
                <span class="kernel-ver">{{ k.version || k.id }}</span>
                <span class="kernel-hash font-mono">{{ k.digest ? k.digest.slice(0, 16) + '…' : '' }}</span>
              </div>
              <div>
                <Badge v-if="k.active" variant="accent" label="新环境默认" />
                <Button
                  v-else
                  variant="default"
                  size="compact"
                  @click="onActivateKernel(k.id)"
                >
                  设为新环境默认
                </Button>
              </div>
            </div>

            <div v-if="kernels.length === 0" class="empty-kernels">
              {{ kernelsLoading ? '正在读取已安装内核…' : '尚未安装内核，请先安装并设置默认版本' }}
            </div>
          </div>

          <div v-if="cachedKernels === null" class="empty-kernels">
            服务器未配置内核缓存目录（--kernel-root），无法缓存和分发各平台内核。
          </div>
          <template v-else>
            <div class="kernels-list">
              <div v-for="k in cachedKernels" :key="k.kernel_id" class="kernel-item">
                <div class="kernel-info">
                  <span class="kernel-ver">{{ k.kernel_id }}</span>
                  <span class="kernel-hash">{{ k.complete ? '四个平台已缓存' : `已缓存 ${Object.keys(k.platforms).length}/4 个平台` }}</span>
                </div>
                <div class="kernel-row-actions">
                  <Button v-if="!k.complete" variant="default" size="compact" :loading="kernelBusy === k.kernel_id"
                    :disabled="!!kernelBusy" @click="onCacheKernel(k.kernel_id)">补全缓存</Button>
                  <Button variant="danger" size="compact" :loading="kernelBusy === k.kernel_id"
                    :disabled="!!kernelBusy" @click="onRemoveKernel(k.kernel_id)">删除</Button>
                </div>
              </div>
            </div>
            <div class="kernel-install-actions">
              <Button variant="default" size="compact" :loading="catalogLoading" @click="onLoadCatalog">
                <Download :size="13" />
                <span>查看官方版本</span>
              </Button>
            </div>
            <div v-if="catalog.length" class="kernels-list">
              <div v-for="k in catalog" :key="k.kernel_id" class="kernel-item">
                <div class="kernel-info">
                  <span class="kernel-ver">{{ k.kernel_id }}</span>
                  <span class="kernel-hash">{{ k.prerelease ? '预发布' : '稳定版' }}{{ k.complete ? '' : ' · 缺少平台或摘要，不能缓存' }}</span>
                </div>
                <Badge v-if="k.cached" variant="accent" label="已缓存" />
                <Button v-else variant="default" size="compact" :disabled="!k.complete || !!kernelBusy"
                  :loading="kernelBusy === k.kernel_id" @click="onCacheKernel(k.kernel_id)">缓存</Button>
              </div>
            </div>
          </template>
        </div>
      </section>

      <!-- Section 3: Configuration Export/Import -->
      <section class="settings-card">
        <div class="card-header">
          <FileJson :size="20" class="header-icon" />
          <h2 class="card-title">配置导入与导出</h2>
        </div>

        <div class="card-content">
          <p class="card-desc">
            导出或备份工作区的环境设置与代理资源结构。导出的配置文件不包含代理明文密码、成员账号与本地网站数据。
          </p>

          <div class="config-actions-row">
            <Button variant="default" @click="onExportConfig">
              <Download :size="15" />
              <span>导出工作区配置 (JSON)</span>
            </Button>

            <label class="import-file-btn">
              <Upload :size="15" />
              <span>导入工作区配置</span>
              <input type="file" accept=".json" hidden @change="onImportConfig" />
            </label>
          </div>
        </div>
      </section>

      <!-- Section 4: Security Boundaries Notice -->
      <section class="settings-card">
        <div class="card-header">
          <CheckCircle :size="20" class="header-icon" />
          <h2 class="card-title">自动化与隔离边界说明</h2>
        </div>

        <div class="card-content">
          <div class="boundary-notice">
            <h4 class="notice-headline">团队安全规范：</h4>
            <p>
              团队工作区禁用旧的单 Token 自动化开关。同机应用可由服务器显式绑定已有成员，
              按该成员权限借用环境；自动化任务与网页控制台共用环境、代理和运行记录。
            </p>
            <p style="margin-top: 8px;">
              冷备份与隔离恢复属于执行机离线维护命令（<code>online_maintenance=false</code>），以确保恢复前验证来源工作区退役证明并保持工作区锁隔离。
            </p>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.settings-view {
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

.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}

.settings-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--color-border-subtle);
  padding-bottom: 14px;
}

.header-icon {
  color: var(--color-accent);
}

.card-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text);
}

.card-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-content {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-desc {
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.form-row > input, .form-row > select { min-width: 0; width: 100%; }

.form-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
}

.card-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}

.kernels-list {
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 160px;
  overflow-y: auto;
}

.kernel-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background-color: var(--color-surface);
  border-radius: var(--radius-sm);
}

.kernel-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.kernel-ver {
  font-weight: 600;
  font-size: 13px;
  color: var(--color-text);
}
.kernel-hash {
  font-size: 11px;
  color: var(--color-subtle);
}

.empty-kernels {
  text-align: center;
  padding: 20px;
  color: var(--color-subtle);
  font-size: 12px;
}

.kernel-install-actions,
.kernel-row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.config-actions-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.import-file-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background-color: var(--color-raised);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
}
.import-file-btn:hover {
  background-color: var(--color-raised-hover);
}

.boundary-notice {
  background-color: rgba(117, 171, 255, 0.08);
  border: 1px solid rgba(117, 171, 255, 0.25);
  border-radius: var(--radius-md);
  padding: 16px;
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.6;
}

.notice-headline {
  color: var(--color-blue);
  font-weight: 700;
  margin-bottom: 6px;
}

@media (max-width: 900px) {
  .settings-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
