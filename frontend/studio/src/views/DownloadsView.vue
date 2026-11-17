<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiClientUpdate, clientPackageUrl, type ClientPackage, type ClientPlatform } from '../api/client-update'
import { useAuthStore } from '../stores/auth'
import { useDesktopStore } from '../stores/desktop'
import { useNotificationStore } from '../stores/notification'
import { ERROR_MESSAGES } from '../types/api'
import Button from '../components/common/Button.vue'
import { Download, RefreshCw } from 'lucide-vue-next'

const PLATFORMS: Record<ClientPlatform, string> = {
  'win-x86_64': 'Windows 10/11 x64',
  'mac-arm64': 'macOS（Apple 芯片）',
  'mac-x86_64': 'macOS（Intel）',
  'lin-x86_64': 'Ubuntu 24.04 x64',
}

const auth = useAuthStore()
const desktopStore = useDesktopStore()
const notification = useNotificationStore()
const packages = ref<ClientPackage[]>([])
const loading = ref(false)
const error = ref('')
const insecure = computed(() => !auth.capabilities?.secure_transport)

function size(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    packages.value = (await apiClientUpdate()).packages
    if (desktopStore.available) await desktopStore.checkUpdate()
  } catch (cause: any) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function install() {
  try {
    await desktopStore.installUpdate()
  } catch (cause: any) {
    notification.error(cause.message)
  }
}

onMounted(load)
</script>

<template>
  <div class="downloads-view">
    <section class="card">
      <header class="card-header">
        <Download :size="18" />
        <h2 class="card-title">客户端下载</h2>
        <Button variant="ghost" size="compact" :loading="loading" @click="load">
          <RefreshCw :size="14" />
          <span>刷新</span>
        </Button>
      </header>
      <p class="hint">
        安装包由管理员放到服务器的客户端目录，每个文件都附带 SHA-256 摘要。安装包未签名，
        Windows 首次运行会出现 SmartScreen 提示，macOS 需要右键选择“打开”。
      </p>
      <p v-if="insecure" class="warning">
        当前为 HTTP 连接：安装包与摘要来自同一明文连接，无法防止同一网络上的第三方替换。需要传输保护时请服务器启用 HTTPS。
      </p>
      <p v-if="error" class="error">{{ error }}</p>
      <p v-else-if="!loading && packages.length === 0" class="hint">服务器尚未提供客户端安装包。</p>
      <table v-else class="table">
        <thead>
          <tr><th>平台</th><th>版本</th><th>大小</th><th>SHA-256</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="item in packages" :key="item.platform">
            <td>{{ PLATFORMS[item.platform] }}</td>
            <td>{{ item.version }}</td>
            <td>{{ size(item.size) }}</td>
            <td><code class="digest">{{ item.sha256 }}</code></td>
            <td><a class="link" :href="clientPackageUrl(item.platform)" :download="item.file">下载</a></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="desktopStore.available" class="card">
      <header class="card-header">
        <h2 class="card-title">本客户端</h2>
      </header>
      <p class="hint">当前版本 {{ desktopStore.clientVersion || '—' }}</p>
      <template v-if="desktopStore.newerPackage && desktopStore.packaged">
        <p>服务器提供新版本 {{ desktopStore.newerPackage.version }}。更新前请关闭本机运行的环境。</p>
        <p class="hint">
          Windows 会在后台安装并重新打开客户端；macOS 打开磁盘映像后把应用拖入“应用程序”，
          Ubuntu 在软件安装器中完成安装。客户端随后退出。
        </p>
        <Button variant="primary" size="compact" :loading="desktopStore.updateState?.state === 'downloading'"
                @click="install">
          <Download :size="14" />
          <span>下载并安装</span>
        </Button>
      </template>
      <p v-else-if="desktopStore.newerPackage" class="hint">
        服务器提供新版本 {{ desktopStore.newerPackage.version }}；源码运行的客户端请用 Git 更新。
      </p>
      <p v-else-if="desktopStore.compatibleClient !== null" class="hint">已是服务器提供的最新版本。</p>
      <p v-if="desktopStore.updateState?.state === 'error'" class="error">
        {{ ERROR_MESSAGES[desktopStore.updateState.error] || desktopStore.updateState.error }}
      </p>
    </section>
  </div>
</template>

<style scoped>
.downloads-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px;
  overflow: auto;
  height: 100%;
}

.card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-title {
  font-size: 15px;
  margin: 0;
  flex: 1;
}

.hint {
  color: var(--color-muted);
  font-size: 12px;
  margin: 0;
}

.warning {
  color: var(--color-warning);
  font-size: 12px;
  margin: 0;
}

.error {
  color: var(--color-danger);
  font-size: 12px;
  margin: 0;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th,
.table td {
  text-align: left;
  padding: 8px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.digest {
  font-size: 11px;
  word-break: break-all;
}

.link {
  color: var(--color-accent);
}
</style>
