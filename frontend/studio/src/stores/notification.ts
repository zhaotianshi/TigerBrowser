import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ToastItem {
  id: string
  type: 'success' | 'error' | 'warning' | 'info'
  message: string
}

export const useNotificationStore = defineStore('notification', () => {
  const toasts = ref<ToastItem[]>([])

  function show(message: string, type: 'success' | 'error' | 'warning' | 'info' = 'info', durationMs = 4000) {
    const id = Math.random().toString(36).slice(2, 9)
    toasts.value.push({ id, type, message })
    if (durationMs > 0) {
      setTimeout(() => {
        remove(id)
      }, durationMs)
    }
  }

  function success(message: string, durationMs = 3500) {
    show(message, 'success', durationMs)
  }

  function error(message: string, durationMs = 6000) {
    show(message, 'error', durationMs)
  }

  function warning(message: string, durationMs = 5000) {
    show(message, 'warning', durationMs)
  }

  function info(message: string, durationMs = 3500) {
    show(message, 'info', durationMs)
  }

  function remove(id: string) {
    const idx = toasts.value.findIndex((t) => t.id === id)
    if (idx !== -1) {
      toasts.value.splice(idx, 1)
    }
  }

  return {
    toasts,
    show,
    success,
    error,
    warning,
    info,
    remove,
  }
})
