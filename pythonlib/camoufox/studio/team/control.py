"""Connection-bound input leases; generations fence queued input without stopping a browser."""

import asyncio
import time

from ..store import ValidationError
from ..engine import StudioError
from .native import validate_event


class ControlError(Exception):
    def __init__(self, code):
        self.code = code


class RemoteRun:
    def __init__(self, service, profile_id, run_id, native):
        self.service, self.profile_id, self.run_id, self.native = service, profile_id, run_id, native
        self.gate = asyncio.Lock()
        self.controller = None
        self.generation = 0
        self.sequence = 0
        self.deadline = 0

    async def watch(self, access):
        if self.service.closing:
            raise ControlError('run_conflict')
        await self.service.work(access.require_profile, self.profile_id, 'watch')
        session = self.service.engine.sessions.get(self.profile_id)
        run = await self.service.work(self.service.repository.latest_run, self.profile_id)
        if (not session or session.stop.is_set() or session.remote is not self.native
                or not run or run['id'] != self.run_id or run['state'] != 'running'):
            raise ControlError('run_conflict')

    async def clear(self):
        # The same gate covers the complete native dispatch and key/button release barrier.
        self.generation += 1
        self.controller = None
        self.deadline = 0
        session = self.service.engine.sessions.get(self.profile_id)
        if session is None or session.remote is not self.native:
            return
        try:
            if not session.stop.is_set():
                try:
                    await self.native.drain()
                except Exception:
                    if not session.stop.is_set():
                        raise
            # Browser exit owns worker/display teardown. A viewer can outlive it;
            # wait for that cleanup instead of treating its closed pipe as a leak.
            if session.stop.is_set():
                await asyncio.shield(session.task)
        except BaseException:
            self.service.failed = True
            if self.service.engine.sessions.get(self.profile_id) is session:
                await self.service.engine.stop(self.profile_id)
            raise

    async def expire(self):
        if self.controller:
            try:
                await self.service.work(self.controller[1].require_profile, self.profile_id, 'operate')
                await self.service.work(self.controller[1].require_profile, self.profile_id, 'watch')
            except ValidationError:
                await self.clear()
                return
            if self.deadline <= time.monotonic():
                await self.clear()

    def status(self, connection_id=None):
        result = {'generation': self.generation, 'controller': self.controller[2] if self.controller else None,
                'width': self.native.width, 'height': self.native.height, 'run_id': self.run_id}
        if connection_id is not None:
            result['mine'] = bool(self.controller and self.controller[0] == connection_id)
        return result

    async def handle(self, access, connection_id, message):
        async with self.service.gate, self.gate:
            await self.watch(access)
            await self.expire()
            if not isinstance(message, dict):
                raise ControlError('invalid_request')
            kind = message.get('kind')
            if kind == 'status' and set(message) == {'kind'}:
                return self.status(connection_id)
            if type(message.get('generation')) is not int or message['generation'] != self.generation:
                raise ControlError('control_changed')
            await self.service.work(access.require_profile, self.profile_id, 'operate')
            def require_display():
                viewer = getattr(self.service, 'viewers', {}).get(connection_id)
                if viewer is not None and (viewer.closed or not viewer.ready):
                    raise ControlError('display_required')
            require_display()
            if kind == 'claim' and set(message) == {'kind', 'generation', 'force'}:
                if type(message['force']) is not bool:
                    raise ControlError('invalid_request')
                actor = await self.service.work(access.resolve)
                if message['force']:
                    await self.service.work(access.require_admin)
                require_display()
                if self.controller and self.controller[0] != connection_id:
                    if not message['force']:
                        raise ControlError('control_busy')
                    await self.clear()
                if not self.controller:
                    await self.clear()
                    await self.watch(access)
                    require_display()
                    self.controller = (connection_id, access, actor['member']['id'])
                    self.sequence = 0
                    await self.service.work(self.service.store.record_audit, 'control.claimed',
                                            actor_id=actor['member']['id'], target_kind='run', target_id=self.run_id)
                self.deadline = time.monotonic() + 30
            elif kind in ('heartbeat', 'release', 'reset-input', 'input', 'clipboard'):
                if not self.controller or self.controller[0] != connection_id:
                    raise ControlError('control_required')
                if kind == 'clipboard' and set(message) == {'kind', 'generation', 'source', 'request_id'}:
                    if (message['source'] not in ('primary', 'clipboard')
                            or type(message['request_id']) is not int or message['request_id'] <= 0):
                        raise ControlError('invalid_request')
                    try:
                        text = await self.native.clipboard(message['source'])
                    except StudioError as error:
                        code = str(error)
                        raise ControlError(code if code in ('clipboard_capacity', 'clipboard_unsupported')
                                           else 'clipboard_unavailable') from None
                    await self.watch(access)
                    await self.service.work(access.require_profile, self.profile_id, 'operate')
                    await self.expire()
                    if not self.controller or self.controller[0] != connection_id:
                        raise ControlError('control_changed')
                    return {**self.status(connection_id), 'clipboard': text,
                            'clipboard_request': message['request_id']}
                elif kind == 'heartbeat' and set(message) == {'kind', 'generation'}:
                    self.deadline = time.monotonic() + 30
                elif kind == 'release' and set(message) == {'kind', 'generation'}:
                    await self.clear()
                elif kind == 'reset-input' and set(message) == {'kind', 'generation'}:
                    try:
                        await self.native.drain()
                    except BaseException:
                        await self.clear()
                        raise
                elif kind == 'input' and set(message) == {'kind', 'generation', 'sequence', 'event'}:
                    if type(message['sequence']) is not int or message['sequence'] != self.sequence + 1:
                        raise ControlError('input_sequence')
                    if not isinstance(message['event'], dict):
                        raise ControlError('invalid_request')
                    self.sequence = message['sequence']
                    try:
                        validate_event(message['event'], self.native.width, self.native.height)
                    except StudioError:
                        raise ControlError('invalid_input') from None
                    try:
                        require_display()
                        await self.native.input(message['event'])
                    except BaseException:
                        await self.clear()
                        raise
                else:
                    raise ControlError('invalid_request')
            else:
                raise ControlError('invalid_request')
            return self.status(connection_id)

    async def disconnect(self, connection_id):
        async with self.service.gate, self.gate:
            if self.controller and self.controller[0] == connection_id:
                await self.clear()
