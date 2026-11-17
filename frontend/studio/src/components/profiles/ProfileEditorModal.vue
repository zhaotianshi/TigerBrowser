<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import TimezoneSelect from './TimezoneSelect.vue'
import { useProxiesStore } from '../../stores/proxies'
import { useProfilesStore } from '../../stores/profiles'
import { useNotificationStore } from '../../stores/notification'
import { apiCreateProfile, apiUpdateProfile, apiGetProfile } from '../../api/profiles'
import { apiGetKernels } from '../../api/settings'
import type { KernelItem } from '../../types/health'
import type { ProfileDetail, CreateProfileInput, FingerprintOptions } from '../../types/profile'

const props = defineProps<{
  modelValue: boolean
  profileId?: string | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'saved'): void
}>()

const proxiesStore = useProxiesStore()
const profilesStore = useProfilesStore()
const notification = useNotificationStore()

const activeTab = ref<'general' | 'network' | 'fingerprint' | 'launch'>('general')
const loading = ref(false)
const saving = ref(false)
const error = ref('')

const isEdit = computed(() => !!props.profileId)
const originalRevision = ref(0)
const originalDetail = ref<ProfileDetail | null>(null)

// Form fields
const name = ref('')
const groupName = ref('')
const tagsInput = ref('')
const notes = ref('')
const kernel = ref('')
const kernels = ref<KernelItem[]>([])
const kernelError = ref('')
const kernelLoading = ref(false)
const retainedKernel = computed(() => kernel.value && !kernels.value.some(item => item.kernel_id === kernel.value))

async function loadKernels() {
  kernelLoading.value = true
  kernelError.value = ''
  try { kernels.value = (await apiGetKernels()).installed }
  catch { kernels.value = []; kernelError.value = '内核目录暂不可用，已保存的版本会保留。' }
  finally { kernelLoading.value = false }
}

const os = ref<'windows' | 'linux' | 'macos'>('windows')
const locale = ref('en-US')
const timezone = ref('')
const proxyId = ref<string | null>(null)
const proxyTimezone = computed(() => proxiesStore.proxies.find(proxy => proxy.id === proxyId.value)?.last_probe)

const fingerprint = ref<FingerprintOptions>({
  screen_mode: 'custom',
  screen_width: 1920,
  screen_height: 1080,
  fonts_mode: 'identity',
  webgl_mode: 'identity',
  webgl_vendor: '',
  webgl_renderer: '',
  webgpu_mode: 'disabled',
  audio_mode: 'identity',
  voices_mode: 'identity',
  media_mode: 'identity',
  hardware_concurrency: 0,
  webrtc_mode: 'disabled',
  do_not_track: false,
})

const startUrlsInput = ref('')
const humanize = ref(true)
const blockImages = ref(false)
const headless = ref(false)

const localeOptions = [
  'en-US',
  'en-GB',
  'zh-CN',
  'zh-TW',
  'ja-JP',
  'ko-KR',
  'de-DE',
  'fr-FR',
  'ru-RU',
  'es-ES',
]

// Native mode warnings
const nativeWarning = computed(() => {
  const warnings: string[] = []
  if (
    fingerprint.value.webgpu_mode === 'native' &&
    fingerprint.value.webgl_mode !== 'native'
  ) {
    warnings.push('WebGPU 真实模式需要同时将 WebGL 参数设为本机真实值。')
  }
  return warnings.join(' ')
})

watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    void loadKernels()
    error.value = ''
    activeTab.value = 'general'

    if (proxiesStore.proxies.length === 0) {
      proxiesStore.fetchProxies()
    }

    if (props.profileId) {
      loading.value = true
      try {
        const detail = await apiGetProfile(props.profileId)
        originalDetail.value = detail
        originalRevision.value = detail.revision

        name.value = detail.name
        groupName.value = detail.group_name || ''
        tagsInput.value = detail.tags?.join(', ') || ''
        notes.value = detail.notes || ''
        kernel.value = detail.kernel || ''

        os.value = detail.os
        locale.value = detail.locale || 'en-US'
        timezone.value = detail.timezone || ''
        proxyId.value = detail.proxy_id || null

        fingerprint.value = {
          screen_mode: detail.fingerprint?.screen_mode || 'identity',
          screen_width: detail.fingerprint?.screen_width || 1920,
          screen_height: detail.fingerprint?.screen_height || 1080,
          fonts_mode: detail.fingerprint?.fonts_mode || 'identity',
          webgl_mode: detail.fingerprint?.webgl_mode || 'identity',
          webgl_vendor: detail.fingerprint?.webgl_vendor || '',
          webgl_renderer: detail.fingerprint?.webgl_renderer || '',
          webgpu_mode: detail.fingerprint?.webgpu_mode || 'disabled',
          audio_mode: detail.fingerprint?.audio_mode || 'identity',
          voices_mode: detail.fingerprint?.voices_mode || 'identity',
          media_mode: detail.fingerprint?.media_mode || 'identity',
          hardware_concurrency: detail.fingerprint?.hardware_concurrency ?? 0,
          webrtc_mode: detail.fingerprint?.webrtc_mode || 'disabled',
          do_not_track: !!detail.fingerprint?.do_not_track,
        }

        startUrlsInput.value = detail.start_urls?.join('\n') || ''
        humanize.value = detail.humanize ?? true
        blockImages.value = detail.block_images ?? false
        headless.value = detail.headless ?? false
      } catch (err: any) {
        error.value = err.message || '加载环境详情失败'
      } finally {
        loading.value = false
      }
    } else {
      // Defaults for create
      originalDetail.value = null
      kernel.value = ''
      originalRevision.value = 0
      name.value = ''
      groupName.value = ''
      tagsInput.value = ''
      notes.value = ''
      os.value = 'windows'
      locale.value = 'en-US'
      timezone.value = ''
      proxyId.value = null
      fingerprint.value = {
        screen_mode: 'custom',
        screen_width: 1920,
        screen_height: 1080,
        fonts_mode: 'identity',
        webgl_mode: 'identity',
        webgl_vendor: '',
        webgl_renderer: '',
        webgpu_mode: 'disabled',
        audio_mode: 'identity',
        voices_mode: 'identity',
        media_mode: 'identity',
        hardware_concurrency: 0,
        webrtc_mode: 'disabled',
        do_not_track: false,
      }
      startUrlsInput.value = ''
      humanize.value = true
      blockImages.value = false
      headless.value = false
    }
  },
)

async function onSave() {
  if (!name.value.trim()) {
    error.value = '请填写环境名称'
    activeTab.value = 'general'
    return
  }

  saving.value = true
  error.value = ''

  const tags = tagsInput.value
    .split(/[,，]/)
    .map((t) => t.trim())
    .filter(Boolean)

  const start_urls = startUrlsInput.value
    .split('\n')
    .map((u) => u.trim())
    .filter(Boolean)

  const inputData: CreateProfileInput = {
    name: name.value.trim(),
    kernel: isEdit.value ? kernel.value : kernel.value || undefined,
    group_name: groupName.value.trim() || undefined,
    tags,
    notes: notes.value.trim() || undefined,
    os: os.value,
    locale: locale.value.trim(),
    timezone: timezone.value.trim(),
    proxy_id: proxyId.value || null,
    start_urls,
    headless: headless.value,
    humanize: humanize.value,
    block_images: blockImages.value,
    fingerprint: fingerprint.value,
  }

  try {
    if (isEdit.value && props.profileId) {
      await apiUpdateProfile(props.profileId, inputData, originalRevision.value)
      notification.success('环境配置已保存')
    } else {
      await apiCreateProfile(inputData)
      notification.success('环境创建成功')
    }
    emit('saved')
    emit('update:modelValue', false)
    await profilesStore.fetchProfiles()
  } catch (err: any) {
    error.value = err.message || '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    :title="isEdit ? '编辑环境配置' : '创建浏览器环境'"
    width="740px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="editor-layout">
      <!-- Tabs Header -->
      <div class="tab-nav">
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'general' }"
          @click="activeTab = 'general'"
        >
          基础信息
        </button>
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'network' }"
          @click="activeTab = 'network'"
        >
          网络与系统
        </button>
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'fingerprint' }"
          @click="activeTab = 'fingerprint'"
        >
          高级指纹
        </button>
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'launch' }"
          @click="activeTab = 'launch'"
        >
          启动行为
        </button>
      </div>

      <div v-if="error" class="editor-error">
        {{ error }}
      </div>

      <!-- Tab 1: General -->
      <div v-show="activeTab === 'general'" class="tab-content">
        <div class="form-row">
          <label class="form-label" for="profile-kernel">浏览器内核</label>
          <select id="profile-kernel" v-model="kernel" :disabled="kernelLoading || !!kernelError">
            <option v-if="!isEdit || !originalDetail?.kernel" value="">{{ isEdit ? '保留历史身份版本（需唯一匹配）' : '使用服务器默认内核' }}</option>
            <option v-if="retainedKernel" :value="kernel">{{ kernel }}（未安装）</option>
            <option v-for="item in kernels" :key="item.id" :value="item.kernel_id">{{ item.kernel_id }}{{ item.active ? '（默认）' : '' }}</option>
          </select>
          <p v-if="kernelError" class="hint">{{ kernelError }}</p>
          <p v-if="isEdit && kernel !== originalDetail?.kernel" class="hint">更换内核会重新生成设备身份，网站数据保留。</p>
        </div>
        <div class="form-row">
          <label class="form-label required">环境名称</label>
          <input
            v-model="name"
            type="text"
            placeholder="例如：产品运营 · 美国 01"
            maxlength="128"
            required
          />
        </div>

        <div class="form-row">
          <label class="form-label">分组名称</label>
          <input
            v-model="groupName"
            type="text"
            placeholder="设置环境分组，便于在控制台筛选"
            maxlength="128"
          />
        </div>

        <div class="form-row">
          <label class="form-label">标签</label>
          <input
            v-model="tagsInput"
            type="text"
            placeholder="多个标签用逗号分隔，如：电商, 海外, 推广"
          />
        </div>

        <div class="form-row">
          <label class="form-label">备注说明</label>
          <textarea
            v-model="notes"
            rows="4"
            placeholder="记录环境绑定的用途、账号说明或其它维护备忘"
          />
        </div>
      </div>

      <!-- Tab 2: Network & OS -->
      <div v-show="activeTab === 'network'" class="tab-content">
        <div class="form-grid-2">
          <div class="form-row">
            <label class="form-label">模拟操作系统</label>
            <select v-model="os">
              <option value="windows">Windows</option>
              <option value="macos">macOS</option>
              <option value="linux">Linux</option>
            </select>
          </div>

          <div class="form-row">
            <label class="form-label">浏览器语言 (Locale)</label>
            <select v-model="locale">
              <option v-for="loc in localeOptions" :key="loc" :value="loc">
                {{ loc }}
              </option>
            </select>
          </div>
        </div>

        <div class="form-grid-2">
          <div class="form-row">
            <label class="form-label" for="profile-timezone">时区 (Timezone)</label>
            <p v-if="proxyTimezone" class="proxy-timezone-hint">最近探测出口时区：{{ proxyTimezone.timezone }}
              <time :datetime="proxyTimezone.checked_at">（{{ new Date(proxyTimezone.checked_at).toLocaleString('zh-CN') }}）</time>
            </p>
            <TimezoneSelect id="profile-timezone" v-model="timezone" />
          </div>

          <div class="form-row">
            <label class="form-label">绑定网络代理</label>
            <select v-model="proxyId">
              <option :value="null">直连 · 使用执行机本地网络</option>
              <option v-for="p in proxiesStore.proxies" :key="p.id" :value="p.id">
                {{ p.name }} ({{ p.server }})
              </option>
            </select>
          </div>
        </div>

        <div class="notice-box">
          <p class="notice-title">重要提示：</p>
          <p>手动切换代理会保留固定设备指纹与网站数据；启动时仍使用所选节点和自动或指定时区。</p>
          <p>停止后修改分辨率会保留其它设备指纹和网站数据。</p>
          <p>
            修改操作系统或语言会导致旧指纹失效，下次启动时将生成与所选系统匹配的新指纹。已保存的 Cookie 和网站登录数据将完好保留。
          </p>
        </div>
      </div>

      <!-- Tab 3: Advanced Fingerprint -->
      <div v-show="activeTab === 'fingerprint'" class="tab-content">
        <div v-if="nativeWarning" class="warning-box">
          {{ nativeWarning }}
        </div>

        <!-- Section: Screen & Graphics -->
        <div class="section-box">
          <h4 class="section-title">屏幕与图形</h4>
          <div class="form-grid-2">
            <div class="form-row">
              <label class="form-label">屏幕分辨率</label>
              <select v-model="fingerprint.screen_mode">
                <option value="identity">跟随环境指纹 (随机生成真实常见分辨率)</option>
                <option value="native">执行机真实分辨率</option>
                <option value="custom">自定义分辨率</option>
              </select>
            </div>

            <div v-if="fingerprint.screen_mode === 'custom'" class="form-row">
              <label class="form-label">自定义宽高 (像素)</label>
              <div class="dimension-inputs">
                <input
                  v-model.number="fingerprint.screen_width"
                  type="number"
                  min="320"
                  max="16384"
                  placeholder="宽度"
                />
                <span>×</span>
                <input
                  v-model.number="fingerprint.screen_height"
                  type="number"
                  min="240"
                  max="16384"
                  placeholder="高度"
                />
              </div>
            </div>
          </div>

          <div class="form-grid-2">
            <div class="form-row">
              <label class="form-label">字体列表 (Fonts)</label>
              <select v-model="fingerprint.fonts_mode">
                <option value="identity">跟随环境指纹</option>
                <option value="native">执行机真实字体</option>
              </select>
            </div>

            <div class="form-row">
              <label class="form-label">WebGL 参数</label>
              <select v-model="fingerprint.webgl_mode">
                <option value="identity">跟随环境指纹</option>
                <option value="native">执行机真实显卡</option>
                <option value="custom">自定义厂商与渲染器</option>
              </select>
            </div>
          </div>

          <div v-if="fingerprint.webgl_mode === 'custom'" class="form-grid-2">
            <div class="form-row">
              <label class="form-label">WebGL 厂商</label>
              <input
                v-model="fingerprint.webgl_vendor"
                type="text"
                placeholder="例如 Google Inc. (NVIDIA)"
              />
            </div>
            <div class="form-row">
              <label class="form-label">WebGL 渲染器</label>
              <input
                v-model="fingerprint.webgl_renderer"
                type="text"
                placeholder="例如 ANGLE (NVIDIA, GeForce RTX 4070...)"
              />
            </div>
          </div>

          <div class="form-grid-2">
            <div class="form-row">
              <label class="form-label">WebGPU</label>
              <select v-model="fingerprint.webgpu_mode">
                <option value="disabled">禁用</option>
                <option value="native">执行机真实能力</option>
              </select>
            </div>
          </div>
        </div>

        <!-- Section: Media & Hardware -->
        <div class="section-box">
          <h4 class="section-title">媒体与硬件</h4>
          <div class="form-grid-3">
            <div class="form-row">
              <label class="form-label">AudioContext</label>
              <select v-model="fingerprint.audio_mode">
                <option value="identity">跟随指纹</option>
                <option value="native">真实能力</option>
              </select>
            </div>

            <div class="form-row">
              <label class="form-label">语音合成 (Voices)</label>
              <select v-model="fingerprint.voices_mode">
                <option value="identity">跟随指纹</option>
                <option value="native">真实能力</option>
              </select>
            </div>

            <div class="form-row">
              <label class="form-label">媒体设备 (Devices)</label>
              <select v-model="fingerprint.media_mode">
                <option value="identity">跟随指纹</option>
                <option value="native">真实能力</option>
              </select>
            </div>
          </div>

          <div class="form-grid-2">
            <div class="form-row">
              <label class="form-label">CPU 逻辑核心数</label>
              <input
                v-model.number="fingerprint.hardware_concurrency"
                type="number"
                min="0"
                max="256"
                placeholder="0 为自动跟随生成指纹"
              />
            </div>

            <div class="form-row">
              <label class="form-label">WebRTC 行为</label>
              <select v-model="fingerprint.webrtc_mode">
                <option value="disabled">禁用 (最安全，推荐节点环境)</option>
                <option value="native">真实连接 (可能泄露公网 IP)</option>
                <option value="proxy">经代理转发 (需绑定支持的代理)</option>
              </select>
            </div>
          </div>

          <div class="form-row check-row">
            <label class="checkbox-label">
              <input v-model="fingerprint.do_not_track" type="checkbox" />
              <span>发送 Do Not Track (DNT) 请求头</span>
            </label>
          </div>
        </div>
      </div>

      <!-- Tab 4: Launch Behavior -->
      <div v-show="activeTab === 'launch'" class="tab-content">
        <div class="form-row">
          <label class="form-label">启动页面 (Start URLs)</label>
          <textarea
            v-model="startUrlsInput"
            rows="5"
            placeholder="每行输入一个完整的网页 URL，如：&#10;https://www.google.com&#10;https://x.com"
          />
        </div>

        <div class="checkbox-list">
          <label class="checkbox-label">
            <input v-model="humanize" type="checkbox" />
            <div>
              <span class="label-title">启用拟人化鼠标轨迹 (Humanize)</span>
              <p class="label-desc">
                使用 Cursory 自然贝塞尔曲线模拟人类光标移动，有效防止行为验证码检测。
              </p>
            </div>
          </label>

          <label class="checkbox-label">
            <input v-model="blockImages" type="checkbox" />
            <div>
              <span class="label-title">阻止图片加载 (Block Images)</span>
              <p class="label-desc">
                在浏览器中禁止加载所有图片资源，大幅降低代理带宽消耗并加快加载速度。
              </p>
            </div>
          </label>

          <label class="checkbox-label">
            <input v-model="headless" type="checkbox" />
            <div>
              <span class="label-title">默认无头启动 (Headless)</span>
              <p class="label-desc">
                启动时不显示图形窗口，适用于自动化爬虫与后台调度任务。
              </p>
            </div>
          </label>
        </div>
      </div>
    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button variant="primary" type="button" :loading="saving" @click="onSave">
        {{ isEdit ? '保存修改' : '创建环境' }}
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.editor-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tab-nav {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 8px;
}

.tab-btn {
  background: transparent;
  border: none;
  color: var(--color-muted);
  font-size: 13px;
  font-weight: 500;
  padding: 8px 14px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.15s ease;
}

.tab-btn:hover {
  color: var(--color-text);
  background-color: var(--color-raised);
}

.tab-btn.active {
  color: var(--color-accent);
  background-color: rgba(91, 226, 196, 0.1);
  font-weight: 600;
}

.tab-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  animation: fadeIn 0.15s ease;
}

.editor-error {
  padding: 10px 14px;
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
  min-width: 0;
}

.form-row > select { width: 100%; min-width: 0; }
.proxy-timezone-hint { margin: 0; font-size: 12px; color: var(--color-muted); overflow-wrap: anywhere; }

.form-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
}

.form-label.required::after {
  content: ' *';
  color: var(--color-accent);
}

.form-grid-2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.form-grid-3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.dimension-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dimension-inputs input {
  flex: 1;
}

.section-box {
  background-color: rgba(21, 34, 53, 0.6);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
  border-left: 3px solid var(--color-accent);
  padding-left: 8px;
}

.notice-box {
  background-color: rgba(117, 171, 255, 0.1);
  border: 1px solid rgba(117, 171, 255, 0.3);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.5;
}

.notice-title {
  font-weight: 700;
  color: var(--color-blue);
  margin-bottom: 4px;
}

.warning-box {
  background-color: var(--color-warning-bg);
  border: 1px solid rgba(255, 208, 116, 0.4);
  color: var(--color-warning);
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.checkbox-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.checkbox-label {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  cursor: pointer;
}
.checkbox-label input {
  margin-top: 3px;
}

.label-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.label-desc {
  font-size: 12px;
  color: var(--color-subtle);
  margin-top: 2px;
  line-height: 1.4;
}

@media (max-width: 640px) {
  .form-grid-2, .form-grid-3 { grid-template-columns: minmax(0, 1fr); }
  .tab-nav { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
