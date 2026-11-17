import { api } from './client'

export type ClientPlatform = 'win-x86_64' | 'mac-arm64' | 'mac-x86_64' | 'lin-x86_64'

export interface ClientPackage {
  platform: ClientPlatform
  version: string
  file: string
  sha256: string
  size: number
  minimum_protocol: number
  protocol: number
}

export interface ClientUpdate {
  protocol: number
  minimum_protocol: number
  packages: ClientPackage[]
}

export async function apiClientUpdate(): Promise<ClientUpdate> {
  return api.get<ClientUpdate>('/client/update')
}

export function clientPackageUrl(platform: ClientPlatform): string {
  return `/team/v1/client/packages/${platform}`
}

function parts(version: string): number[] {
  return version.split('.').map(Number)
}

export function newerVersion(candidate: string, current: string): boolean {
  const left = parts(candidate)
  const right = parts(current)
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0)
    if (difference) return difference > 0
  }
  return false
}

/** Both sides must speak a protocol the other still accepts. */
export function compatible(
  client: { protocol: number; minimum_protocol: number },
  server: { protocol: number; minimum_protocol: number },
): boolean {
  return server.minimum_protocol <= client.protocol && client.minimum_protocol <= server.protocol
}
