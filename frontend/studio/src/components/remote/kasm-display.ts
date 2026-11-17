export interface KasmDisplay {
  disconnect(): void
}

export async function createKasmDisplay(target: HTMLElement, socket: WebSocket): Promise<KasmDisplay> {
  // The pinned decoder and its provenance ship with the Vue public assets.
  const modulePath = '/kasm-client.js'
  const client = await import(/* @vite-ignore */ modulePath)
  return client.createKasmDisplay(target, socket)
}
