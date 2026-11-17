<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGetTimezones } from '../../api/settings'
import type { TimezoneCatalog } from '../../types/health'

const props = defineProps<{ modelValue: string; id: string }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: string): void }>()
const catalog = ref<TimezoneCatalog>({ version: '', timezones: [] })
const query = ref('')
const loading = ref(true)
const error = ref('')
const labels: Record<string, string> = {
  Africa: '非洲', America: '美洲', Antarctica: '南极洲', Asia: '亚洲', Atlantic: '大西洋',
  Australia: '澳大利亚', Etc: '固定偏移', Europe: '欧洲', Indian: '印度洋', Pacific: '太平洋',
}
const retained = computed(() => props.modelValue && !catalog.value.timezones.some(zone => zone.name === props.modelValue))
const groups = computed(() => {
  const result: Record<string, string[]> = {}
  const search = query.value.trim().toLocaleLowerCase()
  for (const zone of catalog.value.timezones) {
    if (zone.name !== props.modelValue && !`${labels[zone.region] || zone.region} ${zone.name}`.toLocaleLowerCase().includes(search)) continue
    ;(result[zone.region] ??= []).push(zone.name)
  }
  return result
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    catalog.value = await apiGetTimezones()
  } catch (cause: any) {
    error.value = cause.message || '读取服务器时区失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <div class="timezone-select">
    <input v-model="query" type="search" aria-label="搜索时区" placeholder="搜索地区或 IANA 名称" :disabled="loading || !!error" />
    <select :id="id" :value="modelValue" :disabled="loading || !!error"
      @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)">
      <option value="">自动（跟随代理出口）</option>
      <option v-if="retained" :value="modelValue">{{ modelValue }}（已保存）</option>
      <optgroup v-for="(names, region) in groups" :key="region" :label="labels[region] || region">
        <option v-for="name in names" :key="name" :value="name">{{ labels[region] || region }} · {{ name }}</option>
      </optgroup>
    </select>
    <p v-if="loading" role="status">正在读取服务器时区…</p>
    <p v-if="error" role="alert">{{ error }} <button type="button" @click="load">重新加载</button></p>
  </div>
</template>

<style scoped>
.timezone-select { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
input, select { width: 100%; min-width: 0; }
p { margin: 0; font-size: 12px; color: var(--color-muted); }
</style>
