"""Local automation authorized by the member's server login, which the desktop web view holds."""

import time
from uuid import uuid4

import requests

from ..automation import AutomationError
from ..store import ValidationError, validate_id
from ..team.transport import exact_origin, session_cookie
from .sync import TIMEOUT, SyncError

# The automation sweeper renews every five seconds; a missed renewal window fails closed.
STALE = 30
MESSAGES = {
    'profile_checked_out': '环境已签出到其他设备，请等待签回。',
    'profile_busy': '环境正在服务器运行或维护，请先停止。',
    'access_denied': '该成员没有此环境的本机运行权限。',
    'admin_required': '创建环境需要管理员权限。',
    'session_expired': '客户端登录已失效，请在客户端重新登录。',
    'server_unreachable': '无法连接服务器，暂时不能签出环境。',
}
STATUS = {'access_denied': 403, 'admin_required': 403, 'session_expired': 401, 'server_unreachable': 503}


def automation_error(code):
    return AutomationError(code, MESSAGES.get(code, '本机运行准备失败，请在客户端查看原因。'), STATUS.get(code, 409))


class ServerSession:
    """MemberStore's interface for AutomationBridge, answered from a renewed copy of the server's decision."""

    def __init__(self, origin, cookie, *, clock=time.monotonic):
        self.origin, self.cookie, self.clock = origin, cookie, clock
        self.name = session_cookie(exact_origin(origin).scheme)
        self.http = requests.Session()
        self.http.trust_env = False
        self.snapshot = None

    def _request(self, method, path, body=None):
        headers = {'Origin': self.origin, 'Cookie': f'{self.name}={self.cookie}'}
        if method == 'POST':
            if self.snapshot is None:
                raise SyncError('session_expired')
            headers['X-CSRF-Token'] = self.snapshot['session']['csrf']
        try:
            response = self.http.request(method, self.origin + '/team/v1' + path, headers=headers,
                                         json=body, timeout=TIMEOUT)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise SyncError('server_unreachable') from None
        if not response.ok:
            raise SyncError(data.get('error', 'operation_failed'))
        return data

    def renew_local_automation(self):
        try:
            session = self._request('GET', '/me')
            profiles = self._request('GET', '/profiles')['profiles']
        except SyncError:
            self.snapshot = None
            raise
        self.snapshot = {'session': session, 'at': self.clock(),
                         'profiles': {item['id']: item for item in profiles if not item['archived']}}

    def resolve(self):
        snapshot = self.snapshot
        if snapshot is None or self.clock() - snapshot['at'] > STALE:
            raise ValidationError('session_expired')
        return snapshot['session']

    def require_admin(self):
        session = self.resolve()
        if session['member']['role'] != 'admin':
            raise ValidationError('admin_required')
        return session

    def require_profile(self, profile_id, permission):
        """Every operation here runs a checked-out copy, so the check-out grant authorizes it."""
        self.resolve()
        profile = self.snapshot['profiles'].get(profile_id)
        if profile is None or 'checkout' not in profile['permissions']:
            raise ValidationError('access_denied')

    def list_profiles(self, archived=False):
        self.resolve()
        return [] if archived else list(self.snapshot['profiles'].values())

    def checkout(self, profile_id, device):
        return self._request('POST', f'/profiles/{validate_id(profile_id)}/checkout',
                             {'request_id': str(uuid4()), **device})

    def create_profile(self, data):
        """New profiles always live on the server; the listing then reports them like any other."""
        created = self._request('POST', '/profiles', data)
        self.renew_local_automation()
        return self.snapshot['profiles'][created['id']]
