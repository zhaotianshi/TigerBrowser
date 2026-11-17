<script setup lang="ts">
import { ref, shallowRef, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { apiGetRemoteEntry } from '../api/remote'
import { waitForCommand } from '../api/tasks'
import { NativeRemoteController } from '../components/remote/NativeRemoteController'
import { REMOTE_REASON_TEXT, type RemoteEntryResponse } from '../types/remote'
import Button from '../components/common/Button.vue'
import Modal from '../components/common/Modal.vue'
import {
  Maximize,
  Minimize,
  RefreshCw,
  Send,
  Upload,
  FileDown,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  Lock,
} from 'lucide-vue-next'

const route = useRoute()
const authStore = useAuthStore()

const profileId = computed(() => (route.params.profileId as string) || '')

// State
const loading = ref(true)
const entry = ref<RemoteEntryResponse | null>(null)
const errorNotice = ref('')
// Refs must not be deeply unwrapped here: NativeRemoteController carries its
// own Vue ref (version) that the view reads to recompute status.
const controller = shallowRef<NativeRemoteController | null>(null)
const isFullscreen = ref(false)
const scaleMode = ref<'actual' | 'fit'>('actual')
const toolsCollapsed = ref(false)
const textInput = ref('')
const clipboardSource = ref<'primary' | 'clipboard'>('primary')
const clipboardLoading = ref(false)
const receivedClipboard = ref('')
const clipboardNotice = ref('')
const clipboardPreview = ref<HTMLTextAreaElement | null>(null)

// Files drawer
const showFilesDrawer = ref(false)
const fileList = ref<any[]>([])
const fileUploading = ref(false)
const fileRefreshing = ref(false)
const fileUploadError = ref('')

// In-page Login Modal (when 401 without leaving the page)
const showLoginModal = ref(false)
const loginUsername = ref('')
const loginPassword = ref('')
const loginLoading = ref(false)
const loginError = ref('')

// Elements
const viewportRef = ref<HTMLElement | null>(null)
const kasmRef = ref<HTMLElement | null>(null)
const displayStyle = computed(() => {
  const c = controller.value
  void c?.version.value
  return { width: `${c?.width || 1920}px`, height: `${c?.height || 1080}px` }
})
let connectionAttempt = 0
let disposed = false
let restartControl: AbortController | null = null

const statusText = computed(() => {
  const c = controller.value
  if (!c) return '未连接'
  // Read the version counter so this recomputes on every server status.
  const v = c.version.value
  void v
  if (c.mine) {
    return `你正在控制 · ${c.width} × ${c.height}`
  }
  if (c.controller) {
    return '只读观看 · 其他成员正在控制'
  }
  return '只读观看 · 当前无人控制'
})

const isMine = computed(() => {
  const c = controller.value
  if (!c) return false
  const v = c.version.value
  void v
  return c.mine
})

const canClaim = computed(() => {
  return (
    authStore.isAdmin ||
    entry.value?.profile.permissions.includes('operate')
  )
})

function disconnectSession() {
  controller.value?.close()
  controller.value = null
}

async function initRemote() {
  const attempt = ++connectionAttempt
  disconnectSession()
  loading.value = true
  errorNotice.value = ''
  browserExited.value = false

  if (!authStore.isAuthenticated) {
    const success = await authStore.checkSession()
    if (disposed || attempt !== connectionAttempt) return
    if (!success) {
      loading.value = false
      showLoginModal.value = true
      return
    }
  }

  try {
    const res = await apiGetRemoteEntry(profileId.value)
    if (disposed || attempt !== connectionAttempt) return
    entry.value = res

    if (!res.available) {
      loading.value = false
      return
    }

    await nextTick()
    if (disposed || attempt !== connectionAttempt) return
    startSession(res)
  } catch (err: any) {
    if (disposed || attempt !== connectionAttempt) return
    disconnectSession()
    errorNotice.value = err.message || '获取远程入口失败'
  } finally {
    if (!disposed && attempt === connectionAttempt) loading.value = false
  }
}

function startSession(remote: RemoteEntryResponse) {
  if (controller.value) {
    controller.value.close()
  }

  const c = new NativeRemoteController(
    remote.profile.run!.id,
    authStore.session!,
    (status) => { if (controller.value === c) onStatusUpdate(status) },
    (msg, _isErr) => {
      if (controller.value === c) errorNotice.value = msg
    },
    () => { if (controller.value === c) onConnectionClosed() },
  )

  controller.value = c
  if (remote.transport !== 'kasmvnc' || remote.protocol_version !== 2 ||
      !remote.control_path || !remote.display_path || !kasmRef.value) {
    throw new Error('KasmVNC 远程入口不完整，请刷新状态。')
  }
  c.connectKasm(remote.control_path, remote.display_path, kasmRef.value)
}

function onStatusUpdate(_status: any) {
  loading.value = false
}

// The remote browser exiting (tab-close on the last window, or the run
// stopping) must not leave the view frozen on the last frame.
const browserExited = ref(false)
const restarting = ref(false)

function onConnectionClosed() {
  browserExited.value = true
}

async function onRestartAndReconnect() {
  const run = entry.value?.profile.run
  const id = entry.value?.profile.id
  if (!id || disposed || restarting.value) return
  restartControl = new AbortController()
  const signal = restartControl.signal
  restarting.value = true
  errorNotice.value = ''
  try {
    // If a run record is still active, stop that generation first so the
    // start lands on a clean state.
    if (run && run.state !== 'stopped') {
      const { apiStopProfile } = await import('../api/profiles')
      const { generateRequestId } = await import('../types/api')
      await waitForCommand(await apiStopProfile(id, generateRequestId(), run.id), signal)
    }
    if (signal.aborted) return
    const { apiStartProfile } = await import('../api/profiles')
    const { generateRequestId } = await import('../types/api')
    const mode = authStore.capabilities?.browser_mode === 'remote' ? 'remote' : undefined
    await waitForCommand(await apiStartProfile(id, generateRequestId(), mode), signal)
    if (signal.aborted) return
    browserExited.value = false
    await initRemote()
  } catch (err: any) {
    if (!signal.aborted) errorNotice.value = err.message || '重新启动环境失败'
  } finally {
    restarting.value = false
  }
}

// In-page login
async function onInPageLogin() {
  loginLoading.value = true
  loginError.value = ''
  try {
    await authStore.login(loginUsername.value, loginPassword.value)
    showLoginModal.value = false
    await initRemote()
  } catch (err: any) {
    loginError.value = err.message || '登录失败'
  } finally {
    loginLoading.value = false
  }
}

// Fullscreen
async function toggleFullscreen() {
  try {
    if (!document.fullscreenElement) {
      await viewportRef.value?.requestFullscreen()
      isFullscreen.value = true
      toolsCollapsed.value = true
    } else {
      await document.exitFullscreen()
      isFullscreen.value = false
    }
  } catch {
    errorNotice.value = '浏览器未允许全屏。可最大化当前窗口进行操作。'
  }
}

function onClaim(force = false) {
  errorNotice.value = ''
  controller.value?.claimControl(force)
}

function onRelease() {
  controller.value?.releaseControl()
}

function onSendText() {
  errorNotice.value = ''
  if (controller.value?.sendText(textInput.value)) {
    textInput.value = ''
  }
}

function onTextEnter(event: KeyboardEvent) {
  if (!event.isComposing && !event.shiftKey) {
    event.preventDefault()
    onSendText()
  }
}

async function copyReceivedText() {
  const preview = clipboardPreview.value
  if (!preview) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(receivedClipboard.value)
    } else {
      preview.focus()
      preview.select()
      if (!document.execCommand('copy')) throw new Error('copy_denied')
    }
    clipboardNotice.value = '已复制至本机'
  } catch {
    preview.focus()
    preview.select()
    clipboardNotice.value = '浏览器未允许自动复制；请点击“复制至本机”，或按 Ctrl+C 复制已选中的内容。'
  }
}

async function onCopyToLocal() {
  if (!controller.value || !isMine.value || clipboardLoading.value) return
  clipboardLoading.value = true
  errorNotice.value = ''
  receivedClipboard.value = ''
  clipboardNotice.value = ''
  try {
    receivedClipboard.value = await controller.value.readClipboard(clipboardSource.value)
    await nextTick()
    await copyReceivedText()
  } catch (err: any) {
    errorNotice.value = err.message || '读取远程内容失败'
  } finally {
    clipboardLoading.value = false
  }
}

// Pointer & Wheel handlers. JS e.button: 0=left, 1=middle, 2=right; the
// native protocol (xdotool / X11) uses 1=left, 2=middle, 3=right, with 0
// meaning "move only, no button press". Map explicitly — sending JS codes
// verbatim turns a left click into a bare mousemove.
function mapButton(jsButton: number): number {
  const table: Record<number, number> = { 0: 1, 1: 2, 2: 3, 3: 4 }
  return table[jsButton] ?? 0
}

function frameRect(): { rect: DOMRect; nw: number; nh: number } | null {
  const canvas = kasmRef.value?.querySelector('canvas')
  if (!canvas) return null
  return { rect: canvas.getBoundingClientRect(), nw: controller.value?.width || canvas.width, nh: controller.value?.height || canvas.height }
}

function onPointerDown(e: PointerEvent) {
  const f = frameRect()
  const surface = kasmRef.value
  if (!f || !surface || !isMine.value) return
  surface.focus()
  surface.setPointerCapture(e.pointerId)
  controller.value?.handlePointer(e.clientX, e.clientY, f.rect, true, mapButton(e.button), f.nw, f.nh)
}

function onPointerUp(e: PointerEvent) {
  const f = frameRect()
  if (!f || !isMine.value) return
  controller.value?.handlePointer(e.clientX, e.clientY, f.rect, false, mapButton(e.button), f.nw, f.nh)
}

function onPointerMove(e: PointerEvent) {
  const f = frameRect()
  if (!f || !isMine.value) return
  controller.value?.handlePointerMove(e.clientX, e.clientY, f.rect, f.nw, f.nh)
}

function onPointerCancel() {
  controller.value?.resetInput()
}

function onWheel(e: WheelEvent) {
  if (!isMine.value) return
  e.preventDefault()
  controller.value?.handleWheel(e.deltaY)
}

function onKeyDown(e: KeyboardEvent) {
  if (!isMine.value) return
  if (e.code === 'Escape' && isFullscreen.value) {
    return
  }
  e.preventDefault()
  controller.value?.handleKey(e.code, true, e.isComposing || e.key === 'Process' || e.keyCode === 229)
}

function onKeyUp(e: KeyboardEvent) {
  if (!isMine.value) return
  e.preventDefault()
  controller.value?.handleKey(e.code, false, e.isComposing || e.key === 'Process' || e.keyCode === 229)
}

// IME (Chinese input method): while composing, key events belong to the IME;
// once the candidate text is committed, forward it as a native text event.
function onCompositionStart() {
  controller.value?.resetInput()
}

function onCompositionEnd(e: CompositionEvent) {
  if (!isMine.value) return
  const text = e.data || ''
  if (text) {
    controller.value?.handleCompositionText(text)
  }
}

// Files management
async function onRefreshFiles() {
  if (!controller.value) return
  fileRefreshing.value = true
  fileUploadError.value = ''
  try {
    fileList.value = await controller.value.listFiles()
  } catch (err: any) {
    fileUploadError.value = err.message || '获取文件列表失败'
  } finally {
    fileRefreshing.value = false
  }
}

async function onFileUpload(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file || !controller.value) return
  if (file.size > 8 * 1024 * 1024) {
    fileUploadError.value = '文件大小不能超过 8 MiB'
    return
  }

  fileUploading.value = true
  fileUploadError.value = ''
  try {
    await controller.value.uploadFile(file)
    await onRefreshFiles()
  } catch (err: any) {
    fileUploadError.value = err.message || '上传文件失败'
  } finally {
    fileUploading.value = false
  }
}

async function onDownloadFile(name: string) {
  if (!controller.value) return
  try {
    await controller.value.downloadFile(name)
  } catch (err: any) {
    fileUploadError.value = err.message || '下载文件失败'
  }
}

// Window events
function onFullscreenChange() {
  isFullscreen.value = !!document.fullscreenElement
}

function onWindowBlur() {
  controller.value?.releaseControl()
}

function onVisibilityChange() {
  if (document.visibilityState !== 'visible') {
    controller.value?.releaseControl()
  }
}

onMounted(() => {
  initRemote()
  document.addEventListener('fullscreenchange', onFullscreenChange)
  window.addEventListener('blur', onWindowBlur)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  disposed = true
  restartControl?.abort()
  connectionAttempt++
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  window.removeEventListener('blur', onWindowBlur)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  controller.value?.close()
})
</script>

<template>
  <div ref="viewportRef" class="remote-viewport" :class="`scale-${scaleMode}`">
    <!-- Screen View -->
    <div class="canvas-wrapper">
      <div
        v-if="entry?.available"
        ref="kasmRef"
        class="remote-frame kasm-surface"
        :style="displayStyle"
        tabindex="0"
        role="img"
        aria-label="TigerBrowser 远程浏览器画面"
        @pointerdown="onPointerDown"
        @pointerup="onPointerUp"
        @pointermove="onPointerMove"
        @pointercancel="onPointerCancel"
        @blur="onPointerCancel"
        @wheel="onWheel"
        @keydown="onKeyDown"
        @keyup="onKeyUp"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @contextmenu.prevent
      />

      <div v-else-if="loading" class="screen-loading">
        <RefreshCw :size="32" class="spin-icon" />
        <p>正在连接原生浏览器画面…</p>
      </div>

      <!-- Unavailable Banner -->
      <div v-else-if="entry && !entry.available" class="unavailable-screen">
        <AlertTriangle :size="36" class="warn-icon" />
        <h3 class="unavailable-title">原生画面暂不可用</h3>
        <p class="unavailable-desc">
          {{ entry.reason ? REMOTE_REASON_TEXT[entry.reason] : '环境当前未处于可连接状态。' }}
        </p>
        <div class="unavailable-actions">
          <Button variant="primary" size="compact" @click="initRemote">
            <RefreshCw :size="14" />
            <span>刷新状态</span>
          </Button>
          <a href="#/profiles" class="back-link">返回控制台</a>
        </div>
      </div>

      <!-- Browser Exited Banner -->
      <div v-if="browserExited" class="exited-screen">
        <AlertTriangle :size="36" class="warn-icon" />
        <h3 class="unavailable-title">远程连接已断开</h3>
        <p class="unavailable-desc">
          画面与输入连接已关闭。可先重新连接；环境已停止时再重新启动。
        </p>
        <div class="unavailable-actions">
          <Button variant="default" size="compact" @click="initRemote">
            <RefreshCw :size="14" />
            <span>重新连接</span>
          </Button>
          <Button variant="primary" size="compact" :loading="restarting" @click="onRestartAndReconnect">
            <RefreshCw :size="14" />
            <span>重新启动并连接</span>
          </Button>
          <a href="#/profiles" class="back-link">返回控制台</a>
        </div>
      </div>
    </div>

    <!-- Floating Tools Overlay -->
    <div v-if="entry?.available" class="tools-overlay" :class="{ collapsed: toolsCollapsed }">
      <div class="tools-bar">
        <!-- Collapse Toggle -->
        <button
          class="ghost toggle-btn"
          type="button"
          :title="toolsCollapsed ? '展开工具栏' : '收起工具栏'"
          @click="toolsCollapsed = !toolsCollapsed"
        >
          <component :is="toolsCollapsed ? ChevronDown : ChevronUp" :size="16" />
          <span class="status-indicator" :class="{ 'is-mine': isMine }">
            KasmVNC · {{ statusText }}
          </span>
        </button>

        <!-- Actions -->
        <div v-show="!toolsCollapsed" class="tools-actions">
          <Button v-if="canClaim && !isMine" variant="primary" size="compact" @click="onClaim()">
            申请控制
          </Button>
          <Button
            v-if="isMine"
            variant="danger"
            size="compact"
            @click="onRelease"
          >
            释放控制
          </Button>

          <Button
            v-if="authStore.isAdmin && !isMine"
            variant="default"
            size="compact"
            title="强制释放当前控制者并接管"
            @click="onClaim(true)"
          >
            管理员接管
          </Button>

          <!-- Scale selector -->
          <select v-model="scaleMode" class="scale-select">
            <option value="actual">100% 原始大小</option>
            <option value="fit">适应窗口</option>
          </select>

          <!-- Fullscreen Toggle -->
          <Button variant="ghost" size="compact" @click="toggleFullscreen">
            <component :is="isFullscreen ? Minimize : Maximize" :size="14" />
            <span>{{ isFullscreen ? '退出全屏' : '全屏' }}</span>
          </Button>

          <!-- Files Drawer Toggle -->
          <Button
            variant="ghost"
            size="compact"
            @click="showFilesDrawer = !showFilesDrawer; if (showFilesDrawer) onRefreshFiles()"
          >
            <Upload :size="14" />
            <span>文件交换</span>
          </Button>
        </div>
      </div>

      <!-- Text / IME Input for Chinese -->
      <div v-show="!toolsCollapsed" class="text-input-bar">
        <textarea
          v-model="textInput"
          rows="2"
          :disabled="!isMine"
          placeholder="粘贴链接或长文本，Enter 发送，Shift+Enter 换行（最多 1 MiB）"
          @keydown.enter="onTextEnter"
        />
        <Button variant="default" size="compact" :disabled="!isMine || !textInput" @click="onSendText">
          <Send :size="13" />
          <span>发送</span>
        </Button>
      </div>

      <div v-show="!toolsCollapsed" class="clipboard-bar">
        <select v-model="clipboardSource" aria-label="复制来源" :disabled="!isMine || clipboardLoading">
          <option value="primary">选中内容</option>
          <option value="clipboard">环境剪贴板</option>
        </select>
        <Button size="compact" :disabled="!isMine" :loading="clipboardLoading" @click="onCopyToLocal">
          复制至本机
        </Button>
        <span>{{ isMine ? '选中文字后复制；已按 Ctrl+C 的内容请选择环境剪贴板。' : '申请控制后可发送和复制文字，无需全屏。' }}</span>
      </div>
      <div v-if="receivedClipboard && !toolsCollapsed" class="clipboard-result">
        <textarea ref="clipboardPreview" :value="receivedClipboard" readonly rows="3" aria-label="远程复制内容" />
        <Button size="compact" @click="copyReceivedText">复制至本机</Button>
        <span role="status">{{ clipboardNotice }}</span>
      </div>

      <!-- File Drawer -->
      <div v-show="!toolsCollapsed && showFilesDrawer" class="files-drawer">
        <div class="drawer-header">
          <span class="drawer-title">环境文件交换 (Uploads / Downloads)</span>
          <div class="drawer-actions">
            <label class="upload-btn">
              <Upload :size="13" />
              <span>上传文件 (≤8MB)</span>
              <input type="file" hidden @change="onFileUpload" />
            </label>
            <button class="ghost icon-btn" type="button" @click="onRefreshFiles">
              <RefreshCw :size="13" :class="{ 'spin-icon': fileRefreshing }" />
            </button>
          </div>
        </div>

        <div v-if="fileUploadError" class="drawer-error">
          {{ fileUploadError }}
        </div>

        <div class="file-list">
          <div v-for="f in fileList" :key="f.name" class="file-item">
            <span class="file-name">{{ f.name }}</span>
            <button class="ghost dl-btn" type="button" @click="onDownloadFile(f.name)">
              <FileDown :size="13" />
              <span>下载 ({{ f.size }} B)</span>
            </button>
          </div>
          <div v-if="fileList.length === 0" class="empty-files">
            暂无已完成的下载文件
          </div>
        </div>
      </div>

      <!-- Error Toast -->
      <div v-if="errorNotice" class="remote-notice">
        <span>{{ errorNotice }}</span>
        <button class="ghost close-notice" type="button" @click="errorNotice = ''">✕</button>
      </div>
    </div>

    <!-- In-Page Login Modal -->
    <Modal v-model="showLoginModal" title="登录进入远程工作区" width="400px" :show-close="false">
      <form class="inpage-login" @submit.prevent="onInPageLogin">
        <p class="login-desc">访问此环境画面需要先恢复登录凭证。</p>
        <div v-if="loginError" class="login-err">{{ loginError }}</div>
        <div class="form-row">
          <label>用户名</label>
          <input v-model="loginUsername" type="text" required />
        </div>
        <div class="form-row">
          <label>密码</label>
          <input v-model="loginPassword" type="password" required />
        </div>
        <Button variant="primary" type="submit" :loading="loginLoading">
          <Lock :size="14" />
          <span>确认登录</span>
        </Button>
      </form>
    </Modal>
  </div>
</template>

<style scoped>
.remote-viewport {
  position: fixed;
  inset: 0;
  background-color: #060a12;
  overflow: auto;
  display: flex;
  align-items: center;
  justify-content: center;
  user-select: none;
}

.canvas-wrapper {
  min-width: 100%;
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.remote-viewport.scale-actual .remote-frame {
  max-width: none;
  max-height: none;
  cursor: crosshair;
}

.remote-viewport.scale-fit .remote-frame {
  max-width: 100vw;
  max-height: 100vh;
  object-fit: contain;
  cursor: crosshair;
}

.remote-frame {
  display: block;
  outline: none;
  image-rendering: -webkit-optimize-contrast;
}

.kasm-surface {
  flex-shrink: 0;
  overflow: hidden;
  touch-action: none;
}

.kasm-surface :deep(canvas) {
  pointer-events: none;
}

.screen-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  color: var(--color-muted);
}

.spin-icon {
  animation: spin 1.2s linear infinite;
  color: var(--color-accent);
}

.unavailable-screen {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 36px 48px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  max-width: 480px;
}

.warn-icon {
  color: var(--color-warning);
}

.unavailable-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
}

.unavailable-desc {
  font-size: 13px;
  color: var(--color-muted);
  line-height: 1.5;
}

.unavailable-actions {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
}

.back-link {
  color: var(--color-accent);
  text-decoration: none;
  font-size: 13px;
}

.exited-screen {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 600;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 36px 48px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  max-width: 480px;
  box-shadow: var(--shadow-lg);
}

/* Tools Overlay */
.tools-overlay {
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 500;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 800px;
  width: 90%;
  max-height: calc(100vh - 40px);
  overflow-y: auto;
  pointer-events: none;
  transition: all 0.2s ease;
}

.tools-bar {
  pointer-events: auto;
  background-color: rgba(16, 26, 42, 0.88);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 8px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  box-shadow: var(--shadow-lg);
}

.toggle-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text);
}

.status-indicator {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
}
.status-indicator.is-mine {
  color: var(--color-accent);
}

.tools-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.scale-select {
  font-size: 12px;
  padding: 4px 8px;
  height: 28px;
}

.text-input-bar {
  pointer-events: auto;
  background-color: rgba(16, 26, 42, 0.92);
  backdrop-filter: blur(8px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  display: flex;
  gap: 8px;
}

.text-input-bar textarea {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  resize: vertical;
  user-select: text;
}

.clipboard-bar, .clipboard-result {
  pointer-events: auto;
  background-color: rgba(16, 26, 42, 0.95);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--color-muted);
}

.clipboard-result textarea {
  width: 100%;
  user-select: text;
  resize: vertical;
}

.files-drawer {
  pointer-events: auto;
  background-color: rgba(16, 26, 42, 0.95);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 200px;
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.drawer-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.drawer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.upload-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 4px 10px;
  background-color: var(--color-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  cursor: pointer;
}

.file-list {
  max-height: 120px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.file-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 8px;
  background-color: var(--color-bg);
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.dl-btn {
  font-size: 11px;
  padding: 2px 6px;
  color: var(--color-accent);
}

.empty-files {
  text-align: center;
  color: var(--color-subtle);
  font-size: 11px;
  padding: 12px;
}

.remote-notice {
  pointer-events: auto;
  background-color: var(--color-danger-bg);
  border: 1px solid rgba(255, 162, 169, 0.4);
  color: var(--color-danger);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
}

.close-notice {
  border: none;
  background: transparent;
  color: var(--color-danger);
  cursor: pointer;
}

.inpage-login {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.login-desc {
  font-size: 12px;
  color: var(--color-muted);
}

.login-err {
  font-size: 12px;
  color: var(--color-danger);
  background: var(--color-danger-bg);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
