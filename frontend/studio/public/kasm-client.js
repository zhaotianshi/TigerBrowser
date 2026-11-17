import RFB from './kasm-client/rfb.js';

// RFB owns a binary channel; the controller retains the socket's JSON and lifecycle events.
class DisplayChannel {
	constructor(socket) {
		this.socket = socket;
		this.binaryType = 'arraybuffer';
		this.protocol = '';
		this.onerror = null;
		this.onmessage = null;
		this.onopen = null;
		this.onclose = null;
		this.listeners = {
			message: (event) => {
				if (event.data instanceof ArrayBuffer) this.onmessage?.(event);
			},
			close: (event) => {
				try {
					this.onclose?.(event);
				} finally {
					this.detach();
				}
			},
			error: (event) => this.onerror?.(event),
		};
		socket.binaryType = 'arraybuffer';
		for (const [name, listener] of Object.entries(this.listeners)) {
			socket.addEventListener(name, listener);
		}
	}

	get readyState() {
		return this.socket.readyState;
	}

	send(data) {
		this.socket.send(data);
	}

	close() {
		this.socket.close();
	}

	detach() {
		for (const [name, listener] of Object.entries(this.listeners)) {
			this.socket.removeEventListener(name, listener);
		}
	}
}

export async function createKasmDisplay(target, socket) {
	if (!(target instanceof HTMLElement) || socket.readyState !== WebSocket.OPEN) {
		throw new Error('KasmVNC requires a display element and an open display socket');
	}
	const channel = new DisplayChannel(socket);
	let rfb;
	try {
		rfb = new RFB(target, document.createElement('input'), channel, {
			shared: true,
			hiDpi: false,
			videoRenderingMode: 'canvas2d',
		}, [], true);
		rfb.viewOnly = true;
		rfb.focusOnClick = false;
		rfb.resizeSession = false;
		rfb.scaleViewport = true;
		rfb.enableWebRTC = false;
		rfb.enableQOI = false;
		rfb.enableWebP = true;
		rfb.clipboardBinary = false;
		// Upstream attaches on a timer. Wait until its receive callback exists before
		// the controller releases the server's RFB greeting with its ready message.
		await new Promise((resolve) => setTimeout(resolve, 0));
		if (typeof channel.onmessage !== 'function' || socket.readyState !== WebSocket.OPEN) {
			throw new Error('KasmVNC display socket closed during initialization');
		}
	} catch (error) {
		rfb?.disconnect();
		channel.detach();
		throw error;
	}
	let disconnected = false;
	return {
		disconnect() {
			if (disconnected) return;
			disconnected = true;
			rfb.disconnect();
		},
	};
}
