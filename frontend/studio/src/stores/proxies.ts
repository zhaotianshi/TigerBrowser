import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiListProxies } from '../api/proxies'
import type { ProxyItem } from '../types/proxy'

export const useProxiesStore = defineStore('proxies', () => {
  const proxies = ref<ProxyItem[]>([])
  const loading = ref(false)
  const searchQuery = ref('')
  const selectedProxyId = ref<string | null>(null)

  const filteredProxies = computed(() => {
    const q = searchQuery.value.trim().toLowerCase()
    if (!q) return proxies.value

    return proxies.value.filter((p) => {
      return (
        p.name.toLowerCase().includes(q) ||
        p.server.toLowerCase().includes(q) ||
        p.username?.toLowerCase().includes(q)
      )
    })
  })

  const selectedProxy = computed(() =>
    proxies.value.find((p) => p.id === selectedProxyId.value) || null,
  )

  async function fetchProxies() {
    loading.value = true
    try {
      proxies.value = await apiListProxies()
    } finally {
      loading.value = false
    }
  }

  function selectProxy(id: string | null) {
    selectedProxyId.value = id
  }

  return {
    proxies,
    loading,
    searchQuery,
    selectedProxyId,
    filteredProxies,
    selectedProxy,
    fetchProxies,
    selectProxy,
  }
})
