import { api, API_PREFIX } from './client'
import type { RemoteEntryResponse, RemoteFileItem } from '../types/remote'

export async function apiGetRemoteEntry(profileId: string): Promise<RemoteEntryResponse> {
  return api.get<RemoteEntryResponse>(`/profiles/${profileId}/remote`)
}

export async function apiListRemoteFiles(runId: string): Promise<RemoteFileItem[]> {
  const result = await api.get<{ files: RemoteFileItem[] }>(`/files/${runId}`)
  return result.files
}

export async function apiUploadRemoteFile(
  runId: string,
  file: File,
  csrfToken: string,
): Promise<{ name: string; size: number }> {
  const response = await fetch(
    `${API_PREFIX}/files/${runId}/${encodeURIComponent(file.name)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-CSRF-Token': csrfToken,
      },
      body: file,
      credentials: 'same-origin',
    },
  )
  if (!response.ok) {
    const errorJson = await response.json().catch(() => ({}))
    throw new Error(errorJson.error || '上传文件失败')
  }
  return response.json()
}

export async function apiDownloadRemoteFile(runId: string, fileName: string): Promise<Blob> {
  const response = await fetch(
    `${API_PREFIX}/files/${runId}/${encodeURIComponent(fileName)}`,
    {
      method: 'GET',
      credentials: 'same-origin',
    },
  )
  if (!response.ok) {
    throw new Error('下载文件失败')
  }
  return response.blob()
}
