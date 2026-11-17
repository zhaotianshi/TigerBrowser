<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { apiCreateKey, apiListKeys, apiRevokeKey, type AutomationConnection, type AutomationKey } from '../api/api-keys'
import { apiListProfiles } from '../api/profiles'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'
import { downloadBlob } from '../utils/download'
import type { ProfileSummary } from '../types/profile'
import Button from '../components/common/Button.vue'

const auth = useAuthStore()
const notification = useNotificationStore()
const keys = ref<AutomationKey[]>([])
const profiles = ref<ProfileSummary[]>([])
const enabled = ref(false)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const name = ref('')
const expires = ref('')
const ips = ref('')
const limited = ref(false)
const selected = ref<string[]>([])
const connection = ref<AutomationConnection | null>(null)
const revoking = ref<AutomationKey | null>(null)
const eligibleProfiles = computed(() => profiles.value.filter(profile => !profile.archived && profile.permissions.includes('operate')))
let generation = 0

function clearCredentials() {
  generation++
  connection.value = null
  revoking.value = null
  keys.value = []
  profiles.value = []
}
watch(() => auth.session, clearCredentials, { flush: 'sync' })
onUnmounted(clearCredentials)

async function refresh() {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const [result, choices] = await Promise.all([apiListKeys(), apiListProfiles()])
    if (current !== generation) return
    keys.value = result.keys
    profiles.value = choices
    enabled.value = result.enabled
  } catch (err: any) {
    if (current === generation) error.value = err.message
  } finally {
    if (current === generation) loading.value = false
  }
}

async function create() {
  if (busy.value || !auth.isOperator) return
  connection.value = null
  const current = generation
  busy.value = true
  error.value = ''
  try {
    const result = await apiCreateKey({
      name: name.value.trim(), profile_ids: limited.value ? selected.value : null,
      allowed_ips: ips.value.split(/[\s,，]+/).filter(Boolean),
      expires_at: expires.value ? new Date(expires.value).toISOString() : null,
    })
    if (current !== generation) return
    connection.value = result.connection
    keys.value.unshift(result.key)
    name.value = ''
  } catch (err: any) {
    if (current === generation) error.value = err.message
  } finally {
    busy.value = false
  }
}

function download() {
  if (!connection.value) return
  downloadBlob(new Blob([JSON.stringify(connection.value, null, 2)], { type: 'application/json' }), 'automation.json')
}

async function revoke() {
  if (!revoking.value || busy.value) return
  const current = generation
  const id = revoking.value.id
  busy.value = true
  try {
    await apiRevokeKey(id)
    if (current !== generation) return
    connection.value = null
    revoking.value = null
    notification.success('密钥已吊销，其任务标签页和借用已释放')
    await refresh()
  } catch (err: any) {
    if (current === generation) error.value = err.message
  } finally {
    busy.value = false
  }
}

function date(value: string | null) {
  return value ? new Date(value).toLocaleString() : '—'
}
onMounted(refresh)
</script>

<template>
  <div class="keys-page">
    <div class="page-heading"><div><h1>API 密钥</h1><p>让自动化应用直接调用服务器上的环境。</p></div>
      <Button :loading="loading" :disabled="busy" @click="refresh">刷新</Button></div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="!loading && !enabled" class="notice">服务器未开启自动化监听。请由管理员启用后再创建密钥。</p>
    <p v-if="auth.capabilities && !auth.capabilities.secure_transport" class="notice">当前使用 HTTP，密钥会明文传输。建议限定来源 IP；需要保密时请使用 HTTPS。</p>
    <section v-if="connection" class="panel one-time" aria-label="新建密钥">
      <h2>请保存连接文件</h2><p>密钥仅显示这一次，关闭后无法再次取回。连接文件包含密钥，请妥善保存。</p>
      <textarea readonly :value="JSON.stringify(connection, null, 2)" rows="6" aria-label="自动化连接文件" spellcheck="false" />
      <div class="actions"><Button @click="download">下载 automation.json</Button>
        <Button @click="connection = null">已保存，关闭</Button></div>
    </section>
    <form class="panel" @submit.prevent="create">
      <h2>创建密钥</h2>
      <fieldset :disabled="!enabled || loading || busy || !auth.isOperator">
        <div class="fields"><label>名称<input v-model="name" required maxlength="120" placeholder="例如：订单采集" /></label>
          <label>到期时间（本地时间，可选）<input v-model="expires" type="datetime-local" /></label></div>
        <label>允许的来源 IP / CIDR（可选）<textarea v-model="ips" rows="2" placeholder="每行一个，留空表示不限制来源地址" /></label>
        <label class="inline"><input v-model="limited" type="checkbox" />仅允许指定环境</label>
        <div v-if="limited" class="profile-choices"><label v-for="profile in eligibleProfiles" :key="profile.id" class="inline">
          <input v-model="selected" :value="profile.id" type="checkbox" />{{ profile.name }}</label>
          <p v-if="!eligibleProfiles.length">当前没有可操作的环境。</p></div>
        <p class="muted">密钥始终受所属成员当前权限限制。限定环境的管理员密钥不能创建新环境。</p>
        <Button type="submit" :loading="busy" :disabled="limited && !selected.length">创建密钥</Button>
      </fieldset>
    </form>
    <section class="panel"><h2>已创建的密钥</h2><p v-if="!keys.length">暂无密钥。</p>
      <ul class="key-list"><li v-for="key in keys" :key="key.id">
        <div class="key-heading"><strong>{{ key.name }}</strong><span>{{ key.member_name }}</span>
          <span v-if="key.revoked_at">已吊销</span><span v-else-if="key.expires_at && new Date(key.expires_at) <= new Date()">已过期</span>
          <Button v-else size="compact" :disabled="busy" @click="revoking = key">吊销</Button></div>
        <p>环境：{{ key.profile_ids ? `${key.profile_ids.length} 个指定环境` : '成员当前有权操作的环境' }} · 来源：{{ key.allowed_ips.join('、') || '不限' }}</p>
        <p>最近使用：{{ date(key.last_used_at) }} · 到期：{{ key.expires_at ? date(key.expires_at) : '长期有效' }}</p>
        <div v-if="revoking?.id === key.id" class="notice"><p>吊销后，此密钥创建的任务标签页会关闭，浏览器会继续运行。</p>
          <div class="actions"><Button :loading="busy" @click="revoke">确认吊销</Button><Button :disabled="busy" @click="revoking = null">取消</Button></div></div>
      </li></ul>
    </section>
  </div>
</template>

<style scoped>
.keys-page { padding: 28px; display: grid; gap: 20px; max-width: 1100px; margin: auto; }
.page-heading, .key-heading, .actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.page-heading { justify-content: space-between; }
h1 { font-size: 24px; } h2 { font-size: 17px; margin-bottom: 12px; }
p { margin: 8px 0; overflow-wrap: anywhere; }
.panel { padding: 22px; border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-surface); min-width: 0; }
fieldset { border: 0; padding: 0; min-width: 0; display: grid; gap: 14px; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
label { display: grid; gap: 7px; font-size: 13px; min-width: 0; }
input:not([type=checkbox]), textarea { width: 100%; box-sizing: border-box; padding: 10px; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; min-width: 0; }
.inline { display: flex; align-items: center; gap: 8px; }
.profile-choices { display: grid; gap: 8px; max-height: 240px; overflow: auto; }
.key-list { list-style: none; padding: 0; } .key-list li { padding: 16px 0; border-top: 1px solid var(--color-border); }
.key-heading strong { flex: 1; overflow-wrap: anywhere; } .muted, .key-list p { color: var(--color-muted); font-size: 13px; }
.notice { padding: 12px; border: 1px solid var(--color-border); border-radius: 6px; }
.error { color: var(--color-danger); } .one-time textarea { margin: 12px 0; font-family: monospace; }
@media (max-width: 600px) { .keys-page { padding: 16px; } .fields { grid-template-columns: 1fr; } .panel { padding: 16px; } }
</style>
