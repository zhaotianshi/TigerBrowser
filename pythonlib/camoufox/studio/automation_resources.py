"""Page-bound passive observations and bounded transfers of browser-owned bytes."""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_CHUNK = 1024 * 1024
MAX_EVENT_TIMEOUT = 300
HANDLE_SECONDS = 300
CAPABILITIES = {
    'mainWorldEval': True, 'responseObservation': True, 'responseBodyRead': True,
    'downloadBinaryRead': True, 'downloadEventFilter': True, 'binaryTransferVersion': 1,
    'maxTransferBytes': MAX_DOWNLOAD, 'maxChunkBytes': MAX_CHUNK,
}


class AutomationError(Exception):
    def __init__(self, code, message, status=400, name='Error'):
        super().__init__(message)
        self.code, self.status, self.name = code, status, name


def invalid():
    return AutomationError('invalid_request', 'The automation request is invalid.')


def timeout_seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= MAX_EVENT_TIMEOUT * 1000:
        raise invalid()
    return value / 1000


def origin(url):
    if not isinstance(url, str) or not url or len(url) > 8192 or any(ord(char) <= 32 for char in url):
        raise invalid()
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError()
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        return parts.scheme, parts.hostname.lower(), port
    except (ValueError, TypeError):
        raise invalid() from None


@dataclass
class BinaryResult:
    data: bytes
    metadata: dict


class URLFilter:
    def __init__(self, opts):
        if (not isinstance(opts, dict) or set(opts) - {'origin', 'url', 'urlPrefix'}
                or 'origin' not in opts or {'url', 'urlPrefix'} <= set(opts)):
            raise invalid()
        self.origin = origin(opts['origin'])
        parsed = urlsplit(opts['origin'])
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise invalid()
        self.url, self.prefix = opts.get('url'), opts.get('urlPrefix')
        for value in (self.url, self.prefix):
            if value is not None and (not isinstance(value, str) or len(value) > 8192 or origin(value) != self.origin
                                      or urlsplit(value).fragment):
                raise invalid()

    def matches(self, url):
        try:
            return (origin(url) == self.origin and (self.url is None or url == self.url)
                    and (self.prefix is None or url.startswith(self.prefix)))
        except AutomationError:
            return False


class Observation:
    """Latch the first matching request and follow only its redirect descendants."""

    def __init__(self, page, opts):
        if not isinstance(opts, dict) or set(opts) - {'origin', 'url', 'urlPrefix', 'method', 'timeout'}:
            raise invalid()
        self.filter = URLFilter({key: value for key, value in opts.items() if key not in ('method', 'timeout')})
        self.method = opts.get('method')
        if (not isinstance(self.method, str) or not self.method.isascii()
                or not self.method.isalpha() or self.method != self.method.upper() or len(self.method) > 16):
            raise invalid()
        timeout = timeout_seconds(opts.get('timeout', 30000))
        self.page, self.request, self.response = page, None, None
        self.started = time.monotonic()
        self.future = asyncio.get_running_loop().create_future()
        self.record = {'state': 'waiting', 'requestId': None, 'responseId': None,
                       'redirectedFrom': None, 'redirects': [], 'status': None,
                       'mimeType': None, 'declaredBytes': None, 'timings': {}}
        self.listeners = {'request': self._request, 'response': self._response,
                          'requestfinished': self._finished, 'requestfailed': self._failed}
        self.timer = asyncio.get_running_loop().call_later(timeout, self._timeout)
        for name, callback in self.listeners.items():
            page.on(name, callback)

    def _elapsed(self):
        return round((time.monotonic() - self.started) * 1000, 3)

    def _detach(self):
        for name, callback in self.listeners.items():
            self.page.remove_listener(name, callback)
        self.timer.cancel()

    def _finish(self, state):
        self.record['state'] = state
        self.record['timings']['completedMs'] = self._elapsed()
        self._detach()
        if not self.future.done():
            self.future.set_result(None)

    def _timeout(self):
        self._finish('timed_out')

    def close(self):
        self._finish('released')
        self.request = self.response = None

    def _request(self, request):
        previous = request.redirected_from
        if self.request is None:
            if request.method != self.method or not self.filter.matches(request.url):
                return
        elif previous is not self.request:
            return
        else:
            if len(self.record['redirects']) >= 20:
                self._finish('redirect_limit')
                return
            self.record['redirects'].append({key: self.record[key] for key in (
                'requestId', 'responseId', 'status', 'method')})
            self.record['redirectedFrom'] = self.record['requestId']
        self.request, self.response = request, None
        self.record.update(state='requested', requestId=secrets.token_urlsafe(18), responseId=None,
                           status=None, method=request.method, mimeType=None, declaredBytes=None)
        self.record['timings']['requestMs'] = self._elapsed()

    def _response(self, response):
        if response.request is not self.request:
            return
        self.response = response
        headers = response.headers
        length = headers.get('content-length', '')
        self.record.update(state='responded', responseId=secrets.token_urlsafe(18), status=response.status,
                           mimeType=headers.get('content-type', '').split(';', 1)[0][:256] or None,
                           declaredBytes=int(length) if length.isascii() and length.isdecimal() and len(length) < 20 else None)
        self.record['timings']['responseMs'] = self._elapsed()
        if headers.get('content-disposition', '').split(';', 1)[0].strip().lower() == 'attachment':
            self._finish('attachment')

    def _finished(self, request):
        if request is self.request:
            if (self.response is not None and self.response.status in (301, 302, 303, 307, 308)
                    and 'location' in self.response.headers):
                return
            self._finish('complete' if self.response is not None else 'unavailable')

    def _failed(self, request):
        if request is self.request:
            self._finish('failed')

    def status(self):
        return self.record | {'redirects': list(self.record['redirects']), 'timings': dict(self.record['timings'])}

    async def result(self):
        await asyncio.shield(self.future)
        if self.record['state'] == 'timed_out':
            raise AutomationError('operation_timeout', 'The response observation timed out.', 408, 'TimeoutError')
        if self.record['state'] == 'released':
            raise AutomationError('handle_not_found', 'The response observation was released.', 404)
        return self.status()


class BodyTransfer:
    """Sequential chunks with a final digest; no success before a complete source."""

    def __init__(self, source, *, observation=None, budget=None):
        self.source, self.observation, self.budget = source, observation, budget
        self.reserved = 0
        self.data = self.path = None
        self.size = None
        self.offset = 0
        self.digest = hashlib.sha256()
        self.closed = self.busy = False
        self.mime = None
        self.timings = {}

    def close(self):
        self.closed = True
        self.data = self.path = self.source = None
        if self.reserved:
            self.budget['bytes'] -= self.reserved
            self.reserved = 0

    async def _file_work(self, function):
        # Drain admitted file work before deleting browser-owned artifacts on cancellation.
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _prepare(self, limit):
        started = time.monotonic()
        if self.observation is not None:
            result = await self.observation.result()
            self.timings['handleWaitMs'] = round((time.monotonic() - started) * 1000, 3)
            if result['state'] != 'complete':
                raise AutomationError('response_body_unavailable', 'Use the independently registered download event if one occurred.', 409)
            declared = result['declaredBytes']
            has_body = self.observation.request.method != 'HEAD' and result['status'] not in (204, 304)
            if has_body and declared is not None and declared > limit:
                raise AutomationError('image_size_limit', 'The response exceeds the requested size limit.', 413)
            if self.budget['bytes'] + limit > self.budget['limit']:
                raise AutomationError('resource_limit', 'Response body memory is already reserved.', 409)
            self.budget['bytes'] += limit
            self.reserved = limit
            started = time.monotonic()
            try:
                data = await self.observation.response.body()
            except Exception:
                raise AutomationError('response_body_unavailable', 'The browser did not expose this response body.', 409) from None
            if len(data) > limit:
                raise AutomationError('image_size_limit', 'The response exceeds the requested size limit.', 413)
            headers = self.observation.response.headers
            if (declared is not None and headers.get('content-encoding', 'identity').lower() == 'identity'
                    and has_body
                    and len(data) != declared):
                raise AutomationError('body_incomplete', 'The response body length does not match its declared length.', 502)
            self.data, self.size, self.mime = data, len(data), result['mimeType']
            self.timings['sourceReadMs'] = round((time.monotonic() - started) * 1000, 3)
        else:
            try:
                path = await self.source.path()
            except Exception:
                raise AutomationError('download_failed', 'The browser download failed.', 502) from None
            if path is None:
                raise AutomationError('download_failed', 'The browser download failed.', 502)
            self.timings['downloadWaitMs'] = round((time.monotonic() - started) * 1000, 3)
            self.path = Path(path)
            self.size = await self._file_work(lambda: self.path.stat().st_size)
        if self.size > limit:
            raise AutomationError('image_size_limit', 'The artifact exceeds the requested size limit.', 413)

    async def read(self, opts):
        if not isinstance(opts, dict) or set(opts) - {'offset', 'chunkBytes', 'maxBytes', 'timeout'}:
            raise invalid()
        offset, count, limit = opts.get('offset', 0), opts.get('chunkBytes', MAX_CHUNK), opts.get('maxBytes', MAX_DOWNLOAD)
        if (type(offset) is not int or offset < 0 or type(count) is not int or not 1 <= count <= MAX_CHUNK
                or type(limit) is not int or not 1 <= limit <= MAX_DOWNLOAD):
            raise invalid()
        timeout = timeout_seconds(opts.get('timeout', 30000))
        if self.closed:
            raise AutomationError('handle_not_found', 'The byte transfer was released.', 404)
        if self.busy:
            raise AutomationError('transfer_busy', 'Another read is using this handle.', 409)
        if offset != self.offset:
            raise AutomationError('transfer_offset', 'Read the next sequential offset.', 409)
        self.busy = True
        try:
            return await asyncio.wait_for(self._read(offset, count, limit), timeout)
        except BaseException:
            self.close()
            raise
        finally:
            self.busy = False

    async def _read(self, offset, count, limit):
        if self.size is None:
            await self._prepare(limit)
        if self.closed:
            raise AutomationError('handle_not_found', 'The byte transfer was released.', 404)
        if self.size > limit:
            raise AutomationError('image_size_limit', 'The artifact exceeds the requested size limit.', 413)
        started = time.monotonic()
        expected = min(count, self.size - offset)
        if self.path is not None:
            path, size = self.path, self.size

            def read():
                with path.open('rb') as stream:
                    if stream.seek(0, 2) != size:
                        raise AutomationError('body_incomplete', 'The download size changed during transfer.', 502)
                    stream.seek(offset)
                    return stream.read(expected)

            data = await self._file_work(read)
        else:
            data = self.data[offset:offset + expected]
        if self.closed:
            raise AutomationError('handle_not_found', 'The byte transfer was released.', 404)
        if len(data) != expected:
            raise AutomationError('body_incomplete', 'The browser artifact is incomplete.', 502)
        self.digest.update(data)
        self.offset += len(data)
        complete = self.offset == self.size
        return BinaryResult(data, {'transferVersion': 1, 'offset': offset, 'readBytes': len(data),
                                  'totalBytes': self.size, 'mimeType': self.mime, 'complete': complete,
                                  'sha256': self.digest.hexdigest() if complete else None,
                                  'chunkSha256': hashlib.sha256(data).hexdigest(),
                                  'timings': self.timings | {'chunkReadMs': round((time.monotonic() - started) * 1000, 3)}})
