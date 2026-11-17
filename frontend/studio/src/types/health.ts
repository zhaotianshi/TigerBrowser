export interface HealthData {
  active_runs: number
  pending_commands: number
  active_tasks: number
  max_running: number
  disk_free_bytes: number
  runtime_registered: boolean
  execution_blocked: boolean
  recovery_required: boolean
  closing: boolean
  remote_display: boolean
  maintenance: boolean
}

export interface TaskItem<Result = any> {
  id: string
  request_id: string
  kind: string
  profile_id?: string
  state: 'accepted' | 'running' | 'succeeded' | 'failed'
  result?: Result
  error?: string | null
  created_at: string
  updated_at: string
}

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

export interface ActivityItem {
  id?: string
  action: string
  profile_id?: string
  detail: any
  created_at: string
}

export interface SettingsData {
  executable_path: string
  max_running: number
  automation_enabled: boolean
}


export interface TimezoneCatalog {
  version: string
  timezones: { name: string; region: string }[]
}

export interface KernelItem {
  id: string
  kernel_id: string
  version: string
  digest: string
  active: boolean
}

export interface CachedKernel {
  kernel_id: string
  complete: boolean
  platforms: Record<string, { sha256: string; size: number }>
}

export interface AvailableKernel {
  kernel_id: string
  version: string
  build: string
  prerelease: boolean
  cached: boolean
  complete: boolean
}

export interface KernelInventory {
  installed: KernelItem[]
  // null when the server was started without a kernel cache directory
  cache: CachedKernel[] | null
}
