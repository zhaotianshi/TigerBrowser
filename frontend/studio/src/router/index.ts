import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { desktopAvailable } from '../desktop/bridge'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      redirect: '/profiles',
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/profiles',
      name: 'profiles',
      component: () => import('../views/ProfilesView.vue'),
    },
    {
      path: '/proxies',
      name: 'proxies',
      component: () => import('../views/ProxiesView.vue'),
      meta: { admin: true },
    },
    {
      path: '/activity',
      name: 'activity',
      component: () => import('../views/ActivityView.vue'),
    },
    {
      path: '/api-keys',
      name: 'api-keys',
      component: () => import('../views/ApiKeysView.vue'),
      meta: { operator: true },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { admin: true },
    },
    {
      path: '/local',
      name: 'local',
      component: () => import('../views/LocalView.vue'),
      meta: { desktop: true },
    },
    {
      path: '/downloads',
      name: 'downloads',
      component: () => import('../views/DownloadsView.vue'),
    },
    {
      path: '/team',
      name: 'team',
      component: () => import('../views/TeamView.vue'),
      meta: { admin: true },
    },
    {
      path: '/status',
      name: 'status',
      component: () => import('../views/HealthView.vue'),
      meta: { admin: true },
    },
    {
      path: '/remote/:profileId',
      name: 'remote',
      component: () => import('../views/RemoteView.vue'),
      meta: { remote: true },
    },
  ],
})

// Handle legacy format: /#remote=<UUID>
router.beforeEach(async (to, _from, next) => {
  // The legacy entry "#remote=<uuid>" is the raw location hash; vue-router
  // parses it as the path "/remote=<uuid>" rather than a /remote/:id route.
  const hash = window.location.hash || ''
  const legacyMatch = /^#\/?remote=([0-9a-f-]{36})$/i.exec(hash)
  if (legacyMatch) {
    const profileId = legacyMatch[1]
    if (to.name !== 'remote' || to.params.profileId !== profileId) {
      return next({ name: 'remote', params: { profileId } })
    }
  }

  const authStore = useAuthStore()

  if (!authStore.initialized) {
    await authStore.checkSession()
  }

  if (to.meta.public) {
    if (authStore.isAuthenticated) {
      return next('/profiles')
    }
    return next()
  }

  // Remote view handles its own in-page auth restoration / prompt without forcing full redirection
  if (to.meta.remote) {
    return next()
  }

  if (!authStore.isAuthenticated) {
    return next({ name: 'login', query: { redirect: to.fullPath } })
  }

  if (to.meta.admin && !authStore.isAdmin) {
    return next('/profiles')
  }
  if (to.meta.operator && !authStore.isOperator) return next('/profiles')
  if (to.meta.desktop && !desktopAvailable()) return next('/profiles')

  next()
})

export default router
