<script setup lang="ts">
import { watch, onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useEventsStore } from './stores/events'
import { useDesktopStore } from './stores/desktop'
import Sidebar from './components/layout/Sidebar.vue'
import Header from './components/layout/Header.vue'
import TransportWarning from './components/layout/TransportWarning.vue'
import DownloadsView from './views/DownloadsView.vue'
import Toast from './components/common/Toast.vue'

const route = useRoute()
const authStore = useAuthStore()
const eventsStore = useEventsStore()
const desktopStore = useDesktopStore()

// The desktop client opens this server's local cache once a member is logged in.
watch(
  () => authStore.session?.workspace_id,
  (workspaceId) => {
    if (workspaceId && desktopStore.available) {
      void desktopStore.attach(workspaceId)
      void desktopStore.checkUpdate().catch(() => undefined)
    }
  },
  { immediate: true },
)

// A client that cannot speak this server's protocol shows only the update page.
const blocked = computed(() => desktopStore.compatibleClient === false)

const isRemoteView = computed(() => !!route.meta.remote)
const isPublicView = computed(() => !!route.meta.public)

watch(
  () => [authStore.isAuthenticated, isRemoteView.value, isPublicView.value],
  ([authed, remote, publicView]) => {
    if (authed && !remote && !publicView) {
      eventsStore.connect()
    } else {
      eventsStore.disconnect()
    }
  },
  { immediate: true },
)

onMounted(() => {
  if (authStore.isAuthenticated && !isRemoteView.value && !isPublicView.value) {
    eventsStore.connect()
  }
})

onUnmounted(() => {
  eventsStore.disconnect()
})
</script>

<template>
  <div id="studio-app" class="studio-root">
    <!-- Dedicated Remote View or Public/Login View -->
    <template v-if="isRemoteView || isPublicView">
      <router-view />
    </template>

    <!-- Full Workspace Console -->
    <template v-else>
      <Sidebar />
      <div class="workspace-main">
        <TransportWarning />
        <Header />
        <main class="workspace-body">
          <template v-if="blocked">
            <p class="blocked" role="alert">此客户端版本与服务器不兼容，更新后才能继续使用。</p>
            <DownloadsView />
          </template>
          <router-view v-else />
        </main>
      </div>
    </template>

    <!-- Global Toast Container -->
    <Toast />
  </div>
</template>

<style scoped>
.studio-root {
  width: 100vw;
  height: 100vh;
  display: flex;
  overflow: hidden;
  background-color: var(--color-bg);
}

.workspace-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  min-width: 0;
}

.workspace-body {
  flex: 1;
  overflow: hidden;
  position: relative;
  min-height: 0;
}

.blocked {
  margin: 20px 20px 0;
  color: var(--color-danger);
}

@media (max-width: 760px) {
  .studio-root { flex-direction: column; height: 100dvh; }
  .workspace-main { min-height: 0; height: auto; }
}
</style>
