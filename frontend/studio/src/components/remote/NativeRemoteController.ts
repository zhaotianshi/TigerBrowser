import type {
  RemoteStatusMessage,
  RemoteEvent,
  RemoteFileItem,
} from '../../types/remote'
import type { SessionInfo } from '../../types/member'
import { MAX_TEXT_BYTES } from '../../types/remote'
import { apiListRemoteFiles, apiUploadRemoteFile, apiDownloadRemoteFile } from '../../api/remote'
import { ref, type Ref } from 'vue'
import { RemoteInputQueue } from './remote-input'
import { createKasmDisplay, type KasmDisplay } from './kasm-display'

export type RemoteNotifyCallback = (message: string, isError?: boolean) => void

export class NativeRemoteController {
  private socket: WebSocket | null = null
  private displaySocket: WebSocket | null = null
  private display: KasmDisplay | null = null
  private kasm: { path: string; target: HTMLElement } | null = null
  private displayReady = false
  private heartbeatTimer: any = null
  private inputQueue = new RemoteInputQueue((event: RemoteEvent) => this.dispatchInput(event))
  private downloadURLs: Set<string> = new Set()
  private aborted = false
  private clipboardSequence = 0
  private clipboardRequest: { id: number; resolve: (text: string) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> } | null = null

  public generation = 0
  public sequence = 0
  public mine = false
  public width = 1920
  public height = 1080
  public controller: string | null = null
  public keyCodes: Set<string> = new Set()
  // Vue-visible counter; the view recomputes status whenever a server
  // status message or close event bumps it, because class fields are not
  // reactive on their own.
  public readonly version: Ref<number> = ref(0)

  constructor(
    public readonly runId: string,
    public readonly session: SessionInfo,
    private readonly onStatus: (status: RemoteStatusMessage) => void,
    private readonly notify: RemoteNotifyCallback,
    private readonly onClosed: () => void,
  ) {
    if (session.capabilities?.key_codes) {
      this.keyCodes = new Set(session.capabilities.key_codes)
    }
  }

  private connectControl(wsPath: string) {
    this.socket = new WebSocket(this.socketURL(wsPath))

    this.socket.onopen = () => {
      // Send CSRF hello within 5 seconds
      this.socket?.send(JSON.stringify({ csrf: this.session.csrf }))
    }

    this.socket.onmessage = (event) => {
      if (this.aborted) return

      if (event.data instanceof Blob) {
        this.connectionClosed('KasmVNC 控制连接收到无效画面。')
        return
      }

      let msg: RemoteStatusMessage & { clipboard?: string; clipboard_request?: number }
      try {
        msg = JSON.parse(event.data)
        if (!msg || typeof msg !== 'object') throw new Error('invalid_status')
      } catch {
        this.connectionClosed('远程连接收到无效消息。')
        return
      }

      if (msg.error) {
        const errorMap: Record<string, string> = {
          control_busy: '其他成员正在操作，请等待释放或请管理员接管。',
          control_changed: '控制权已变化，旧输入未执行。',
          control_required: '请先申请控制权。',
          invalid_input: '此输入未被支持，浏览器仍在运行。中文请使用发送文本功能。',
          clipboard_unavailable: '没有可复制的文字。请先在远程页面选中文字，或按 Ctrl+C 后选择环境剪贴板。',
          clipboard_capacity: '远程内容超过 1 MiB，无法复制。',
          clipboard_unsupported: '当前执行系统暂不支持远程剪贴板读取。',
        }
        if (this.clipboardRequest && msg.clipboard_request === this.clipboardRequest.id) {
          this.finishClipboard(new Error(errorMap[msg.error] || '远程操作被拒绝'))
        }
        this.notify(errorMap[msg.error] || '远程操作被拒绝', true)
        return
      }

      if (msg.generation < this.generation) return

      if (this.clipboardRequest && (msg.generation !== this.generation || !msg.mine)) {
        this.finishClipboard(new Error('控制权已变化，请重新复制。'))
      }
      if (typeof msg.clipboard === 'string' && msg.clipboard_request === this.clipboardRequest?.id) {
        this.finishClipboard(null, msg.clipboard)
      }

      if (msg.generation !== this.generation || !msg.mine) this.inputQueue.cancel()
      if (msg.generation !== this.generation) this.sequence = 0

      this.generation = msg.generation
      this.mine = !!msg.mine
      this.width = msg.width || this.width
      this.height = msg.height || this.height
      this.controller = msg.controller || null
      this.version.value++

      this.onStatus(msg)

      if (this.kasm && !this.displaySocket) {
        if (typeof msg.viewer_id !== 'string' || !msg.viewer_id) {
          this.connectionClosed('KasmVNC 观看会话未建立。')
          return
        }
        this.connectDisplay(msg.viewer_id)
      }

      if (this.mine && !this.heartbeatTimer) {
        this.startHeartbeat()
      } else if (!this.mine && this.heartbeatTimer) {
        this.stopHeartbeat()
      }
    }

    this.socket.onclose = () => this.connectionClosed()
    this.socket.onerror = () => this.connectionClosed('远程连接失败，请检查网络后重新连接。')
  }

  connectKasm(controlPath: string, displayPath: string, target: HTMLElement) {
    this.socketURL(displayPath)
    this.kasm = { path: displayPath, target }
    this.connectControl(controlPath)
  }

  private socketURL(path: string) {
    if (!/^\/(?!\/)[^?#\\]*$/.test(path)) throw new Error('远程连接地址无效。')
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}${path}`
  }

  private connectDisplay(viewerId: string) {
    const kasm = this.kasm!
    const socket = new WebSocket(this.socketURL(kasm.path))
    this.displaySocket = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => socket.send(JSON.stringify({ csrf: this.session.csrf, viewer_id: viewerId }))
    socket.onclose = () => this.connectionClosed()
    socket.onerror = () => this.connectionClosed('KasmVNC 画面连接失败，请重新连接。')
    socket.onmessage = (event) => {
      socket.onmessage = null
      void this.installDisplay(kasm.target, socket, event.data)
    }
  }

  private async installDisplay(target: HTMLElement, socket: WebSocket, hello: unknown) {
    try {
      if (typeof hello !== 'string' || JSON.parse(hello).ready !== true) {
        throw new Error('invalid_display_hello')
      }
      const display = await createKasmDisplay(target, socket)
      if (this.aborted || socket.readyState !== WebSocket.OPEN) {
        display.disconnect()
        return
      }
      this.display = display
      socket.send(JSON.stringify({ ready: true }))
      this.displayReady = true
      this.version.value++
    } catch {
      this.connectionClosed('KasmVNC 画面初始化失败，请重新连接。')
    }
  }

  private connectionClosed(message?: string) {
    if (this.aborted) return
    this.close('远程连接已断开。')
    if (message) this.notify(message, true)
    this.onClosed()
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      if (this.mine) {
        this.send('heartbeat')
      }
    }, 5000)
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  send(kind: string, fields: Record<string, any> = {}) {
    if (kind === 'release' || kind === 'reset-input' || kind === 'claim') this.inputQueue.cancel()
    if (this.aborted || !this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return false
    }
    if (this.kasm && !this.displayReady && (kind === 'claim' || kind === 'input')) return false

    if (this.socket.bufferedAmount > 6 * MAX_TEXT_BYTES + 65536) {
      this.connectionClosed('网络积压，控制连接已自动断开；未发送的输入不会重放。')
      return false
    }

    this.socket.send(
      JSON.stringify({
        kind,
        generation: this.generation,
        ...fields,
      }),
    )
    return true
  }

  claimControl(force = false) {
    this.send('claim', { force })
  }

  releaseControl() {
    this.send('release')
  }

  resetInput() {
    if (this.mine) {
      this.send('reset-input')
    }
  }

  sendInput(event: RemoteEvent) {
    if (!this.mine || (this.kasm && !this.displayReady)) return false
    return this.inputQueue.enqueue(event)
  }

  private dispatchInput(event: RemoteEvent) {
    if (!this.mine) return false
    const sequence = this.sequence + 1
    const sent = this.send('input', {
      sequence,
      event,
    })
    if (sent) this.sequence = sequence
    return sent
  }

  handlePointer(
    clientX: number,
    clientY: number,
    rect: DOMRect,
    down: boolean,
    buttonIndex = 0,
    naturalWidth?: number,
    naturalHeight?: number,
  ) {
    if (!this.mine || !this.width || !this.height) return

    // Map the scaled canvas onto the saved remote screen's pixel grid.
    const srcW = naturalWidth || rect.width
    const srcH = naturalHeight || rect.height
    const scaleX = rect.width > 0 ? srcW / rect.width : 1
    const scaleY = rect.height > 0 ? srcH / rect.height : 1
    const frameX = (clientX - rect.left) * scaleX
    const frameY = (clientY - rect.top) * scaleY

    const x = Math.max(
      0,
      Math.min(
        this.width - 1,
        Math.floor((frameX * this.width) / srcW),
      ),
    )
    const y = Math.max(
      0,
      Math.min(
        this.height - 1,
        Math.floor((frameY * this.height) / srcH),
      ),
    )

    this.sendInput({
      kind: 'pointer',
      x,
      y,
      button: buttonIndex,
      down,
    })
  }

  handlePointerMove(clientX: number, clientY: number, rect: DOMRect, naturalWidth?: number, naturalHeight?: number) {
    this.handlePointer(clientX, clientY, rect, false, 0, naturalWidth, naturalHeight)
  }

  handleWheel(deltaY: number) {
    if (!this.mine) return
    this.sendInput({
      kind: 'wheel',
      up: deltaY < 0,
    })
  }

  handleKey(code: string, isDown: boolean, isComposing: boolean) {
    if (!this.mine) return

    // IME composition in progress: the browser will deliver the committed
    // text via compositionend; physical keys during composition belong to
    // the IME, not the remote browser. Do not reset held keys here — the
    // composition events drive the lifecycle.
    if (isComposing) return

    if (!this.keyCodes.has(code)) {
      return
    }

    this.sendInput({
      kind: 'key',
      code,
      down: isDown,
    })
  }

  // Committed IME text (e.g. Chinese from the local input method) goes out
  // as an explicit text event, per the native protocol.
  handleCompositionText(text: string) {
    return this.sendText(text)
  }

  sendText(text: string) {
    if (!this.mine || !text) return false
    if (/\0|[\uD800-\uDFFF]/u.test(text) || new TextEncoder().encode(text).length > MAX_TEXT_BYTES) {
      this.notify('文本必须是有效文字，且不能超过 1 MiB；内容未发送。', true)
      return false
    }
    return this.sendInput({
      kind: 'text',
      text,
    })
  }

  readClipboard(source: 'primary' | 'clipboard'): Promise<string> {
    if (!this.mine) return Promise.reject(new Error('请先申请控制权。'))
    if (this.clipboardRequest) return Promise.reject(new Error('正在读取远程内容，请稍候。'))
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.finishClipboard(new Error('读取远程内容超时，请重新复制。')), 7000)
      this.clipboardRequest = { id: ++this.clipboardSequence, resolve, reject, timer }
      if (!this.send('clipboard', { source, request_id: this.clipboardRequest.id })) {
        this.finishClipboard(new Error('远程连接不可用。'))
      }
    })
  }

  private finishClipboard(error: Error | null, text = '') {
    const request = this.clipboardRequest
    if (!request) return
    this.clipboardRequest = null
    clearTimeout(request.timer)
    if (error) request.reject(error)
    else request.resolve(text)
  }

  // File management
  async listFiles(): Promise<RemoteFileItem[]> {
    return apiListRemoteFiles(this.runId)
  }

  async uploadFile(file: File) {
    return apiUploadRemoteFile(this.runId, file, this.session.csrf)
  }

  async downloadFile(fileName: string) {
    const blob = await apiDownloadRemoteFile(this.runId, fileName)
    const url = URL.createObjectURL(blob)
    this.downloadURLs.add(url)

    const link = document.createElement('a')
    link.href = url
    link.download = fileName
    link.click()

    setTimeout(() => {
      URL.revokeObjectURL(url)
      this.downloadURLs.delete(url)
    }, 1000)
  }

  close(message = '远程连接已关闭。') {
    if (this.aborted) return
    this.aborted = true
    this.inputQueue.cancel()
    this.stopHeartbeat()
    this.mine = false
    this.displayReady = false
    this.version.value++
    this.finishClipboard(new Error(message))
    if (this.socket) {
      this.socket.close()
      this.socket = null
    }
    if (this.displaySocket) {
      this.displaySocket.close()
      this.displaySocket = null
    }
    this.display?.disconnect()
    this.display = null
    for (const u of this.downloadURLs) {
      URL.revokeObjectURL(u)
    }
    this.downloadURLs.clear()
  }
}
