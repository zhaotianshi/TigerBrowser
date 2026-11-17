<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { apiCreateProxy, apiParseProxy, apiUpdateProxy } from '../../api/proxies'
import { useProxiesStore } from '../../stores/proxies'
import { useNotificationStore } from '../../stores/notification'
import type { CreateProxyInput, ProxyItem, ProxyProtocol } from '../../types/proxy'

const props = defineProps<{ modelValue: boolean; proxy: ProxyItem | null }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'saved'): void
}>()

const proxiesStore = useProxiesStore()
const notification = useNotificationStore()
const isEdit = computed(() => props.proxy !== null)
const name = ref('')
const protocol = ref<ProxyProtocol>('http')
const server = ref('')
const username = ref('')
const password = ref('')
const clearPassword = ref(false)
const proxyLink = ref('')
const parsing = ref(false)
const saving = ref(false)
const error = ref('')

function fillAddress(address: string) {
  const [scheme, endpoint] = address.split('://')
  protocol.value = scheme === 'socks5' || scheme === 'https' ? scheme : 'http'
  server.value = endpoint || ''
}

watch(() => props.modelValue, (open) => {
  if (!open) return
  name.value = props.proxy?.name || ''
  fillAddress(props.proxy?.server || '')
  username.value = props.proxy?.username || ''
  proxyLink.value = ''
  password.value = ''
  clearPassword.value = false
  error.value = ''
})

async function parseLink() {
  if (!proxyLink.value.trim()) return
  parsing.value = true
  error.value = ''
  try {
    const parsed = await apiParseProxy(proxyLink.value.trim())
    fillAddress(parsed.server)
    name.value ||= parsed.name
    username.value = parsed.username || ''
    password.value = parsed.password || ''
    clearPassword.value = !!props.proxy?.has_password && !password.value
    proxyLink.value = ''
  } catch (cause: any) {
    error.value = cause.message || '代理地址格式无效'
  } finally {
    parsing.value = false
  }
}

async function save() {
  if (!name.value.trim() || !server.value.trim()) {
    error.value = '请填写代理名称和服务器地址'
    return
  }
  const input: CreateProxyInput = {
    name: name.value.trim(),
    server: protocol.value + '://' + server.value.trim(),
    username: username.value,
    password: clearPassword.value ? undefined : password.value || undefined,
    clear_password: clearPassword.value,
  }
  saving.value = true
  error.value = ''
  try {
    if (props.proxy) {
      await apiUpdateProxy(props.proxy.id, input)
    } else {
      await apiCreateProxy(input)
    }
    notification.success('代理已保存')
    emit('saved')
    emit('update:modelValue', false)
    await proxiesStore.fetchProxies()
  } catch (cause: any) {
    error.value = cause.message || '保存代理失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    :title="isEdit ? '编辑代理' : '添加代理资源'"
    width="640px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="proxy-editor">

      <div class="quick-import">
        <label class="form-label">快速解析代理地址</label>
        <div class="import-row">
          <input
            v-model="proxyLink"
            type="password"
            placeholder="socks5://proxy.example:1080"
          />
          <Button variant="default" size="compact" type="button" :loading="parsing" @click="parseLink">
            解析填充
          </Button>
        </div>
      </div>

      <div v-if="error" class="editor-error">
        {{ error }}
      </div>

      <div class="form-grid-2">
        <div class="form-row">
          <label class="form-label required">代理名称</label>
          <input v-model="name" type="text" placeholder="例如：美国西雅图 01" required />
        </div>

        <div class="form-row">
          <label class="form-label required">协议类型</label>
          <select v-model="protocol">
            <option value="http">HTTP</option>
            <option value="https">HTTPS</option>
            <option value="socks5">SOCKS5</option>
          </select>
        </div>
      </div>

      <div class="form-row">
        <label class="form-label required">服务器地址</label>
        <input
          v-model="server"
          type="text"
          placeholder="例如 192.168.1.100:1080 或 domain.com:443"
          required
        />
      </div>

      <div class="form-grid-2">
        <div class="form-row">
          <label class="form-label">用户名</label>
          <input v-model="username" type="text" placeholder="无认证时留空" />
        </div>

        <div class="form-row">
          <label class="form-label">密码</label>
          <input
            v-model="password"
            type="password"
            placeholder="留空保留已保存密码"
            :disabled="clearPassword"
          />
          <label v-if="isEdit" class="clear-checkbox">
            <input v-model="clearPassword" type="checkbox" />
            <span>清除已保存密码</span>
          </label>
        </div>
      </div>

    </div>

    <template #footer>
      <Button variant="ghost" type="button" @click="emit('update:modelValue', false)">
        取消
      </Button>
      <Button variant="primary" type="button" :loading="saving" :disabled="parsing" @click="save">
        {{ isEdit ? '保存修改' : '创建代理' }}
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.proxy-editor {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.quick-import {
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 12px 14px;
}

.import-row {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.import-row input {
  flex: 1;
}

.editor-error {
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
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.clear-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-subtle);
  cursor: pointer;
  margin-top: 4px;
}



</style>
