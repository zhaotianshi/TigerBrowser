import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, ssrContextKey } from 'vue'
import TimezoneSelect from '../components/profiles/TimezoneSelect.vue'

const get = vi.hoisted(() => vi.fn())
vi.mock('../api/settings', () => ({ apiGetTimezones: get }))
const renderer = createRenderer<any, any>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
let app: ReturnType<typeof renderer.createApp>
afterEach(() => { app?.unmount(); vi.resetAllMocks() })

async function mount(value: string) {
  let state: any
  const changed = vi.fn()
  app = renderer.createApp({ ...TimezoneSelect, render: () => null,
    setup: (props: unknown, context: unknown) => {
      state = (TimezoneSelect as any).setup(props, context)
      return state
    },
  }, { id: 'timezone', modelValue: value, 'onUpdate:modelValue': changed })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  await new Promise<void>(resolve => setImmediate(resolve))
  return { state, changed }
}

it('searches server choices while retaining a saved alias', async () => {
  get.mockResolvedValue({ version: 'test', timezones: [
    { name: 'Asia/Shanghai', region: 'Asia' }, { name: 'Etc/UTC', region: 'Etc' },
  ] })
  const { state, changed } = await mount('US/Pacific')
  expect(state.retained.value).toBeTruthy()
  state.query.value = '亚洲'
  expect(state.groups.value).toEqual({ Asia: ['Asia/Shanghai'] })
  expect(changed).not.toHaveBeenCalled()
})

it('retains the selected canonical choice when the search does not match it', async () => {
  get.mockResolvedValue({ version: 'test', timezones: [{ name: 'Asia/Shanghai', region: 'Asia' }] })
  const { state, changed } = await mount('Asia/Shanghai')
  state.query.value = 'Europe'
  expect(state.retained.value).toBeFalsy()
  expect(state.groups.value).toEqual({ Asia: ['Asia/Shanghai'] })
  expect(changed).not.toHaveBeenCalled()
})

it('reports unavailable server data without clearing the saved value', async () => {
  get.mockRejectedValue(new Error('Timezone metadata unavailable'))
  const { state, changed } = await mount('US/Pacific')
  expect(state.error.value).toBe('Timezone metadata unavailable')
  expect(state.retained.value).toBeTruthy()
  expect(changed).not.toHaveBeenCalled()
})
