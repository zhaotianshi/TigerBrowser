import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useProfilesStore } from './profiles'
import { useAuthStore } from './auth'
import type { ProfileSummary } from '../types/profile'

export type EventConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error'

export const useEventsStore = defineStore('events', () => {
  const state = ref<EventConnectionState>('disconnected')
  const lastUpdated = ref<Date | null>(null)

  let socket: WebSocket | null = null
  let pingInterval: any = null
  let reconnectTimer: any = null
  let shouldReconnect = false
  let reconnectDelay = 3000
  let generation = 0

  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return
    }

    shouldReconnect = true
    const current = ++generation
    clearTimeout(reconnectTimer)
    state.value = 'connecting'

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/team/v1/events`

    try {
      socket = new WebSocket(url)
    } catch {
      state.value = 'error'
      scheduleReconnect(current)
      return
    }
    const connection = socket
    const active = () => shouldReconnect && generation === current && socket === connection

    connection.onopen = () => {
      if (!active()) return
      state.value = 'connected'
      reconnectDelay = 3000
      clearTimeout(reconnectTimer)

      // Send ping every 25 seconds to keep alive
      clearInterval(pingInterval)
      pingInterval = setInterval(() => {
        if (active() && connection.readyState === WebSocket.OPEN) {
          connection.send('ping')
        }
      }, 25000)
    }

    connection.onmessage = (event) => {
      if (!active()) return
      try {
        const data = JSON.parse(event.data)
        if (data && Array.isArray(data.profiles)) {
          const profilesStore = useProfilesStore()
          profilesStore.setProfiles(data.profiles as ProfileSummary[])
          lastUpdated.value = new Date()
        }
      } catch {
        // Ignored unparsable event
      }
    }

    connection.onerror = () => {
      if (!active()) return
      state.value = 'error'
    }

    connection.onclose = () => {
      if (!active()) return
      clearInterval(pingInterval)
      socket = null
      state.value = 'disconnected'
      scheduleReconnect(current)
    }
  }

  function scheduleReconnect(current: number) {
    clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(async () => {
      if (!shouldReconnect || current !== generation) return
      reconnectDelay = Math.min(reconnectDelay * 1.5, 15000)
      const auth = useAuthStore()
      const valid = await auth.checkSession()
      if (!shouldReconnect || current !== generation) return
      if (valid) connect()
      else if (auth.isAuthenticated) scheduleReconnect(current)
      else disconnect()
    }, reconnectDelay)
  }

  function disconnect() {
    generation++
    shouldReconnect = false
    clearTimeout(reconnectTimer)
    clearInterval(pingInterval)
    if (socket) {
      socket.close()
      socket = null
    }
    state.value = 'disconnected'
  }

  return {
    state,
    lastUpdated,
    connect,
    disconnect,
  }
})
