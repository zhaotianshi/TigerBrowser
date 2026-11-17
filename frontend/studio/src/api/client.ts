import { ERROR_MESSAGES, type ApiErrorResponse } from '../types/api'

export const API_PREFIX = '/team/v1'

class ApiClient {
  private csrfToken = ''
  private onUnauthorizedCallback?: () => void

  setCsrf(token: string) {
    this.csrfToken = token
  }

  getCsrf(): string {
    return this.csrfToken
  }

  onUnauthorized(callback: () => void) {
    this.onUnauthorizedCallback = callback
  }

  async request<T>(path: string, options: {
    method?: 'GET' | 'POST'
    body?: any
    headers?: Record<string, string>
    timeoutMs?: number
  } = {}): Promise<T> {
    const { method = 'GET', body, headers = {}, timeoutMs = 10000 } = options
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)

    const reqHeaders: Record<string, string> = {
      ...headers,
    }

    let reqBody: any
    if (body !== undefined) {
      if (body instanceof FormData || body instanceof Blob) {
        reqBody = body
      } else {
        reqHeaders['Content-Type'] = 'application/json'
        reqBody = JSON.stringify(body)
      }
      if (this.csrfToken) {
        reqHeaders['X-CSRF-Token'] = this.csrfToken
      }
    }

    try {
      const response = await fetch(API_PREFIX + path, {
        method,
        headers: reqHeaders,
        body: reqBody,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      })

      clearTimeout(timer)

      if (!response.ok) {
        if (response.status === 401) {
          this.onUnauthorizedCallback?.()
        }

        let errorKey = 'operation_failed'
        try {
          const errData: ApiErrorResponse = await response.json()
          if (errData?.error) {
            errorKey = errData.error
          }
        } catch {
          // Response body was not JSON
        }

        const message = ERROR_MESSAGES[errorKey] || `操作失败 (${errorKey})`
        const error = new Error(message)
        ;(error as any).code = errorKey
        ;(error as any).status = response.status
        throw error
      }

      // Check if response has content; mutations like proxy creation return 201 with an empty body
      if (response.status === 204 || response.headers.get('content-length') === '0') {
        return undefined as T
      }
      const contentType = response.headers.get('content-type')
      if (contentType && contentType.includes('application/json')) {
        return (await response.json()) as T
      }
      return (await response.blob()) as unknown as T
    } catch (err: any) {
      clearTimeout(timer)
      if (err.name === 'AbortError') {
        throw new Error('请求超时，请检查服务网络。')
      }
      if (err.message && err.code) {
        throw err
      }
      throw new Error('连接团队服务失败，请检查网络或执行机状态。')
    }
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: 'GET' })
  }

  post<T>(path: string, body?: any): Promise<T> {
    return this.request<T>(path, { method: 'POST', body: body ?? {} })
  }
}

export const api = new ApiClient()
