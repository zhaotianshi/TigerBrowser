import { expect, it, vi } from 'vitest'
import { createRenderer, ssrContextKey } from 'vue'
import ProfileEditor from '../components/profiles/ProfileEditorModal.vue'

const update = vi.hoisted(() => vi.fn().mockResolvedValue({}))
vi.mock('../api/profiles', () => ({ apiUpdateProfile: update, apiCreateProfile: vi.fn(), apiGetProfile: vi.fn() }))
vi.mock('../stores/proxies', () => ({ useProxiesStore: () => ({ proxies: [], fetchProxies: vi.fn() }) }))
vi.mock('../stores/profiles', () => ({ useProfilesStore: () => ({ fetchProfiles: vi.fn() }) }))
vi.mock('../stores/notification', () => ({ useNotificationStore: () => ({ success: vi.fn() }) }))

it('saves an explicit empty timezone when returning an existing profile to automatic mode', async () => {
  const renderer = createRenderer<any, any>({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null,
  })
  let state: any
  const app = renderer.createApp({ ...ProfileEditor, render: () => null,
    setup: (props: unknown, context: unknown) => {
      state = (ProfileEditor as any).setup(props, context)
      return state
    },
  }, { modelValue: false, profileId: 'profile' })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  try {
    state.name.value = 'Saved profile'
    state.originalRevision.value = 3
    state.timezone.value = ''
    await state.onSave()
    expect(update).toHaveBeenCalledWith('profile', expect.objectContaining({ timezone: '' }), 3)
  } finally {
    app.unmount()
  }
})
