export class RemoteInputQueue<T extends { kind: string; button?: number }> {
	private pending: T | null = null;
	private timer: ReturnType<typeof setTimeout> | null = null;
	constructor(private readonly dispatch: (event: T) => boolean) {}
	enqueue(event: T): boolean {
		if (event.kind !== "pointer" || event.button !== 0) {
			if (!this.flush()) return false;
			return this.dispatch(event);
		}
		if (this.timer !== null) {
			this.pending = event;
			return true;
		}
		if (!this.dispatch(event)) return false;
		this.schedule();
		return true;
	}
	private schedule() {
		this.timer = setTimeout(() => {
			this.timer = null;
			const event = this.pending;
			this.pending = null;
			if (event && this.dispatch(event)) this.schedule();
		}, 40);
	}
	private flush(): boolean {
		const event = this.pending;
		this.cancel();
		return event ? this.dispatch(event) : true;
	}
	cancel() {
		if (this.timer !== null) clearTimeout(this.timer);
		this.timer = null;
		this.pending = null;
	}
}
