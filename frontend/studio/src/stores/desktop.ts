import { defineStore } from 'pinia'
import { ref } from 'vue'
import { desktop, desktopAvailable, type DesktopDevice, type LocalState, type UpdateState } from '../desktop/bridge'
import { apiCancelCheckout, apiCheckoutProfile } from '../api/profiles'
import { apiClientUpdate, compatible, newerVersion, type ClientPackage } from '../api/client-update'
import { ERROR_MESSAGES, generateRequestId } from '../types/api'
import type { ProfileSummary } from '../types/profile'
import { useNotificationStore } from './notification'
import { useProfilesStore } from './profiles'

export const useDesktopStore = defineStore('desktop', () => {
  const available = desktopAvailable()
  const device = ref<DesktopDevice | null>(null)
  const states = ref<Record<string, LocalState>>({})
  const attachError = ref('')
  // null until checked; false blocks every operation until this computer installs a compatible client.
  const compatibleClient = ref<boolean | null>(null)
  const clientVersion = ref('')
  const packaged = ref(false)
  const newerPackage = ref<ClientPackage | null>(null)
  const updateState = ref<UpdateState | null>(null)
  let listening = false
  let updating = false

  function received(profileId: string, state: any) {
    const notification = useNotificationStore()
    if (!profileId) {
      // Engine messages carry their own user-facing text and no profile.
      if (state.error) notification.error(state.error)
      else if (state.message) notification.info(state.message)
      return
    }
    const previous = states.value[profileId]
    const next = { ...states.value }
    if (state.lease === null && state.browser !== 'running' && state.browser !== 'starting') delete next[profileId]
    else next[profileId] = state
    states.value = next
    if (state.error_code) notification.error(ERROR_MESSAGES[state.error_code] || `本机运行失败 (${state.error_code})`)
    if (previous?.lease !== state.lease) void useProfilesStore().fetchProfiles()
  }

  async function attach(workspaceId: string) {
    if (!available) return
    try {
      device.value = await desktop.attach(workspaceId)
      states.value = await desktop.states()
      attachError.value = ''
    } catch (cause: any) {
      attachError.value = cause.message
      return
    }
    if (!listening) {
      listening = true
      await desktop.onChange(received)
    }
  }

  async function checkUpdate() {
    if (!available) return
    const [client, server] = await Promise.all([desktop.capabilities(), apiClientUpdate()])
    clientVersion.value = client.version
    packaged.value = client.packaged
    compatibleClient.value = compatible(client, server)
    const offered = server.packages.find(item => item.platform === client.platform) ?? null
    newerPackage.value = offered && newerVersion(offered.version, client.version) ? offered : null
    if (!updating) {
      updating = true
      await desktop.onUpdate(next => { updateState.value = next })
    }
  }

  async function installUpdate() {
    updateState.value = null
    await desktop.installUpdate()
  }

  function state(profileId: string): LocalState | undefined {
    return states.value[profileId]
  }

  async function launch(profile: ProfileSummary) {
    if (!device.value) throw new Error(attachError.value || ERROR_MESSAGES.client_not_attached)
    // A lease this computer already holds continues; otherwise the server creates one first.
    if (states.value[profile.id]?.lease) {
      await desktop.launch(profile.id)
      return
    }
    const lease = await apiCheckoutProfile(profile.id, { request_id: generateRequestId(), ...device.value })
    try {
      await desktop.launch(profile.id, lease)
    } catch (cause) {
      // The bridge never received the lease, so nothing ran here and the server keeps its data.
      await apiCancelCheckout(lease.checkout.id, lease.token).catch(() => undefined)
      throw cause
    }
  }

  return {
    available,
    device,
    states,
    attachError,
    attach,
    compatibleClient,
    clientVersion,
    packaged,
    newerPackage,
    updateState,
    checkUpdate,
    installUpdate,
    state,
    launch,
    stop: (profileId: string) => desktop.stop(profileId),
    cancel: (profileId: string) => desktop.cancel(profileId),
    openFolder: (profileId: string) => desktop.openFolder(profileId),
    received,
  }
})
