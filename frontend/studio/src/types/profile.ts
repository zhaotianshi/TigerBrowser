export type ProfilePermission = 'view' | 'watch' | 'operate' | 'manage' | 'checkout'

export type RunState = 'starting' | 'running' | 'stopping' | 'stopped' | 'recovery_required' | 'error' | 'deleting'

export interface ProfileRun {
  id: string
  state: RunState
  mode: 'disabled' | 'headless' | 'desktop' | 'remote' | string
  display_backend?: string
  error: string | null
  updated_at: string
}

export interface ProfileProxySummary {
  id?: string
  name: string
  server?: string
}

export interface FingerprintOptions {
  screen_mode: 'identity' | 'native' | 'custom'
  screen_width: number
  screen_height: number
  fonts_mode: 'identity' | 'native'
  webgl_mode: 'identity' | 'native' | 'custom'
  webgl_vendor: string
  webgl_renderer: string
  webgpu_mode: 'disabled' | 'native'
  audio_mode: 'identity' | 'native'
  voices_mode: 'identity' | 'native'
  media_mode: 'identity' | 'native'
  hardware_concurrency: number
  webrtc_mode: 'disabled' | 'native' | 'proxy'
  do_not_track: boolean
}

export interface ProfileCheckout {
  id: string
  state: 'checked_out' | 'returning'
  member_name: string
  device_name: string
  platform: string
  created_at: string
  heartbeat_at: string
  mine: boolean
}

export interface ProfileSummary {
  id: string
  name: string
  group_name: string
  tags: string[]
  os: 'windows' | 'linux' | 'macos'
  kernel: string
  locale: string
  archived: boolean
  revision: number
  last_launch_at: string | null
  proxy: ProfileProxySummary | null
  permissions: ProfilePermission[]
  run: ProfileRun | null
  // The profile currently runs on this member computer; the server cannot start it.
  checked_out: ProfileCheckout | null
}

export interface ProfileDetail extends ProfileSummary {
  notes: string
  timezone: string
  proxy_id: string | null
  start_urls: string[]
  headless: boolean
  humanize: boolean
  block_images: boolean
  fingerprint: FingerprintOptions
  created_at: string
  updated_at: string
  identity_present: boolean
}

export interface CreateProfileInput {
  name: string
  kernel?: string
  group_name?: string
  tags?: string[]
  notes?: string
  os: 'windows' | 'linux' | 'macos'
  locale: string
  timezone?: string
  proxy_id?: string | null
  start_urls?: string[]
  headless?: boolean
  humanize?: boolean
  block_images?: boolean
  fingerprint?: Partial<FingerprintOptions>
}

export interface UpdateProfileInput {
  data: Partial<CreateProfileInput> & { archived?: boolean }
  revision: number
}

export interface BatchProfileItem {
  profile_id: string
  request_id: string
  run_id?: string
  revision?: number
}

export interface BatchProfileResult {
  profile_id: string
  command?: CommandItem
  task?: TaskItem
  error?: string
}

// Local structural copies to avoid circular imports; shapes mirror the service contract.
export interface CommandItem {
  id: string
  request_id: string
  profile_id: string
  run_id?: string
  operation: 'start' | 'stop'
  state: 'accepted' | 'succeeded' | 'failed' | 'unknown'
  error?: string | null
  updated_at: string
}

export interface TaskItem {
  id: string
  request_id: string
  kind: string
  profile_id?: string
  state: 'accepted' | 'running' | 'succeeded' | 'failed'
  result?: any
  error?: string | null
  created_at: string
  updated_at: string
}
