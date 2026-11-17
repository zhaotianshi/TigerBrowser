"""Session-bound local desktop access; browser internals retain their trusted store."""

from .store import ValidationError


class MemberStore:
    """Limit UI reads and mutations, including callbacks from already-open dialogs."""

    _ADMIN_METHODS = frozenset({
        'save_profile', 'update_profile_checked', 'clone_profile', 'archive_profile',
        'clone_profile_checked', 'delete_profile', 'delete_profile_checked', 'set_identity',
        'reset_identity_checked', 'save_proxy', 'get_proxy', 'delete_proxy',
        'import_proxies', 'save_settings', 'export_config', 'import_config',
        'list_members', 'create_member', 'set_member_active', 'reset_member_password',
        'list_grants', 'grant_profile', 'revoke_profile', 'list_audit',
        'create_member_group', 'list_member_groups', 'get_member_group', 'list_group_members',
        'set_group_members', 'list_group_grants', 'save_group_grants', 'delete_member_group',
    })

    def __init__(self, store, session):
        self._store = store
        self.session_id = session['session_id']
        self.root = store.root
        self.resolve()

    def resolve(self):
        session = self._store.resolve_session(self.session_id)
        if session is None:
            raise ValidationError('session_expired')
        return session

    def require_admin(self):
        session = self.resolve()
        if session['member']['role'] != 'admin':
            raise ValidationError('admin_required')
        return session

    def require_profile(self, profile_id, permission):
        session = self.resolve()
        self._store.check_profile_permission(session['member']['id'], profile_id, permission)

    def list_profiles(self, archived=False):
        session = self.resolve()
        return self._store.visible_profiles(session['member']['id'], archived=archived)

    def get_profile(self, profile_id):
        self.require_profile(profile_id, 'view')
        return self._store.get_profile(profile_id)

    def list_proxies(self):
        session = self.resolve()
        return self._store.list_proxies() if session['member']['role'] == 'admin' else []

    def list_proxy_summaries(self):
        session = self.resolve()
        admin = session['member']['role'] == 'admin'
        result = {}
        for proxy in self._store.list_proxies():
            summary = {'name': proxy['name']}
            if admin:
                summary.update(id=proxy['id'], server=proxy['server'])
            result[proxy['id']] = summary
        return result

    def get_settings(self):
        session = self.resolve()
        return self._store.get_settings() if session['member']['role'] == 'admin' else {}

    def list_events(self, limit=200):
        session = self.resolve()
        if session['member']['role'] == 'admin':
            return self._store.list_events(limit=limit)
        allowed = {p['id'] for archived in (False, True) for p in self.list_profiles(archived)}
        return [event for event in self._store.list_events(limit=limit)
                if event['profile_id'] in allowed]

    def change_own_password(self, member_id, current, new_password):
        session = self.resolve()
        if member_id != session['member']['id']:
            raise ValidationError('access_denied')
        return self._store.change_own_password(member_id, current, new_password)

    def logout(self, session_id):
        if session_id != self.session_id:
            raise ValidationError('access_denied')
        self._store.logout(session_id)

    def renew_local_automation(self):
        self._store.renew_local_automation_session(self.session_id)

    def __getattr__(self, name):
        if name not in self._ADMIN_METHODS:
            raise AttributeError(name)

        def authorized(*args, **kwargs):
            self.require_admin()
            return getattr(self._store, name)(*args, **kwargs)

        return authorized
