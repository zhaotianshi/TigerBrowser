"""Group grants merge with direct grants without bypassing role or login checks."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from camoufox.studio.access import MemberStore
from camoufox.studio.store import ProfileStore, ValidationError
from test_studio_team_store import _initialize


@pytest.fixture
def team(tmp_path):
    store = ProfileStore(tmp_path)
    admin = _initialize(store)
    members = {role: store.create_member(admin['id'], role, role.title(), role,
                                         'group-test-password-1')
               for role in ('operator', 'viewer')}
    outsider = store.create_member(admin['id'], 'outside', 'Outside', 'operator',
                                   'group-test-password-1')
    profile = store.save_profile({'name': 'Shared', 'group_name': 'Research'})
    group = store.create_member_group(admin['id'], 'Research')
    yield SimpleNamespace(store=store, admin=admin, members=members, outsider=outsider,
                          profile=profile, group=group)


def add_members(team):
    return team.store.set_group_members(
        team.admin['id'], team.group['id'], [m['id'] for m in team.members.values()],
        expected_revision=team.group['revision'])


def test_group_grants_and_direct_grants_union_with_role_caps(team):
    group = add_members(team)
    team.store.save_group_grants(team.admin['id'], group['id'],
                                {team.profile['id']: ['view', 'operate']},
                                expected_revision=group['revision'])
    operator, viewer = team.members['operator'], team.members['viewer']
    team.store.grant_profile(team.admin['id'], operator['id'], team.profile['id'], ['watch'])
    for role, member in team.members.items():
        opened = team.store.login(role, 'group-test-password-1')
        resolved = team.store.resolve_session(opened['session_id'])
        expected = {'view', 'watch', 'operate'} if role == 'operator' else {'view'}
        assert resolved['permissions'][team.profile['id']] == expected
        assert MemberStore(team.store, opened).get_profile(team.profile['id'])['id'] == team.profile['id']
    team.store.check_profile_permission(operator['id'], team.profile['id'], 'operate')
    for member in (viewer, team.outsider):
        with pytest.raises(ValidationError, match='access_denied'):
            team.store.check_profile_permission(member['id'], team.profile['id'], 'operate')
    assert team.store.visible_profiles(team.outsider['id']) == []


def test_membership_diff_revokes_only_changed_members(team):
    group = add_members(team)
    before = {role: team.store.login(role, 'group-test-password-1') for role in team.members}
    group = team.store.set_group_members(team.admin['id'], group['id'],
                                        [team.members['operator']['id'], team.outsider['id']],
                                        expected_revision=group['revision'])
    assert team.store.resolve_session(before['viewer']['session_id']) is None
    assert team.store.resolve_session(before['operator']['session_id']) is not None
    assert set(team.store.list_group_members(group['id'])) == {
        team.members['operator']['id'], team.outsider['id']}


def test_group_grant_update_revokes_all_group_sessions_but_not_unrelated(team):
    group = add_members(team)
    opened = [team.store.login(role, 'group-test-password-1') for role in team.members]
    outsider = team.store.login('outside', 'group-test-password-1')
    team.store.save_group_grants(team.admin['id'], group['id'],
                                {team.profile['id']: ['view']}, expected_revision=group['revision'])
    assert all(team.store.resolve_session(session['session_id']) is None for session in opened)
    assert team.store.resolve_session(outsider['session_id']) is not None


def test_unchanged_group_updates_preserve_sessions_and_revision(team):
    group = add_members(team)
    group = team.store.save_group_grants(team.admin['id'], group['id'],
                                        {team.profile['id']: ['view', 'operate']},
                                        expected_revision=group['revision'])
    opened = team.store.login('operator', 'group-test-password-1')
    same = team.store.set_group_members(team.admin['id'], group['id'],
                                       [m['id'] for m in team.members.values()],
                                       expected_revision=group['revision'])
    assert same == group
    assert team.store.save_group_grants(team.admin['id'], group['id'],
                                       {team.profile['id']: ['operate', 'view']},
                                       expected_revision=group['revision']) == group
    assert team.store.resolve_session(opened['session_id']) is not None


def test_deleting_group_preserves_direct_access_and_other_groups(team):
    group = add_members(team)
    group = team.store.save_group_grants(team.admin['id'], group['id'],
                                        {team.profile['id']: ['view', 'operate']},
                                        expected_revision=group['revision'])
    operator = team.members['operator']
    team.store.grant_profile(team.admin['id'], operator['id'], team.profile['id'], ['view'])
    other = team.store.create_member_group(team.admin['id'], 'Observers')
    other = team.store.set_group_members(team.admin['id'], other['id'], [operator['id']],
                                        expected_revision=other['revision'])
    team.store.save_group_grants(team.admin['id'], other['id'], {team.profile['id']: ['watch']},
                                expected_revision=other['revision'])
    opened = team.store.login('operator', 'group-test-password-1')
    team.store.delete_member_group(team.admin['id'], group['id'], expected_revision=group['revision'])
    assert team.store.resolve_session(opened['session_id']) is None
    assert [item['id'] for item in team.store.list_member_groups()] == [other['id']]
    team.store.check_profile_permission(operator['id'], team.profile['id'], 'view')
    team.store.check_profile_permission(operator['id'], team.profile['id'], 'watch')
    with pytest.raises(ValidationError, match='access_denied'):
        team.store.check_profile_permission(operator['id'], team.profile['id'], 'operate')
    with team.store._connect() as connection:
        for table in ('team_group_members', 'team_group_grants'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table} WHERE group_id = ?',
                                      (group['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('method', ['set_group_members', 'save_group_grants', 'delete_member_group'])
def test_stale_group_revision_cannot_overwrite_changes(team, method):
    group = add_members(team)
    args = {'set_group_members': [[]], 'save_group_grants': [{team.profile['id']: ['view']}],
            'delete_member_group': []}[method]
    with pytest.raises(ValidationError, match='revision_conflict'):
        getattr(team.store, method)(team.admin['id'], group['id'], *args,
                                   expected_revision=team.group['revision'])
    assert team.store.get_member_group(group['id']) == group


@pytest.mark.parametrize('invalid', [None, 'member', ['invalid'], [str(uuid4())]])
def test_invalid_membership_is_atomic(team, invalid):
    group = add_members(team)
    opened = team.store.login('operator', 'group-test-password-1')
    with pytest.raises((ValidationError, KeyError)):
        team.store.set_group_members(team.admin['id'], group['id'], invalid,
                                     expected_revision=group['revision'])
    assert team.store.get_member_group(group['id']) == group
    assert len(team.store.list_group_members(group['id'])) == 2
    assert team.store.resolve_session(opened['session_id']) is not None


def test_duplicate_and_administrator_group_members_are_rejected(team):
    for members in ([team.admin['id']], [team.outsider['id'], team.outsider['id']]):
        with pytest.raises(ValidationError):
            team.store.set_group_members(team.admin['id'], team.group['id'], members,
                                         expected_revision=team.group['revision'])


def test_group_grant_batch_rolls_back_invalid_row(team):
    group = add_members(team)
    another = team.store.save_profile({'name': 'Other'})
    opened = team.store.login('operator', 'group-test-password-1')
    with pytest.raises(ValidationError, match='role_permission_denied'):
        team.store.save_group_grants(team.admin['id'], group['id'],
                                    {team.profile['id']: ['view'], another['id']: ['manage']},
                                    expected_revision=group['revision'])
    assert team.store.list_group_grants(group['id']) == []
    assert team.store.get_member_group(group['id']) == group
    assert team.store.resolve_session(opened['session_id']) is not None


def test_non_admin_group_writes_and_facade_reads_are_denied(team):
    operator = team.members['operator']
    operations = (
        ('create_member_group', ['Other'], {}),
        ('set_group_members', [team.group['id'], []], {'expected_revision': 1}),
        ('save_group_grants', [team.group['id'], {}], {'expected_revision': 1}),
        ('delete_member_group', [team.group['id']], {'expected_revision': 1}),
    )
    for name, args, kwargs in operations:
        with pytest.raises(ValidationError, match='admin_required'):
            getattr(team.store, name)(operator['id'], *args, **kwargs)
    opened = team.store.login('operator', 'group-test-password-1')
    access = MemberStore(team.store, opened)
    with pytest.raises(ValidationError, match='admin_required'):
        access.list_member_groups()
    team.store.reset_member_password(team.admin['id'], operator['id'], 'replacement-test-password')
    with pytest.raises(ValidationError, match='session_expired'):
        access.get_member_group(team.group['id'])


def test_group_names_unique_and_exports_never_include_group_data(team):
    for name in ('', 'bad\nname', 'x' * 129):
        with pytest.raises(ValidationError):
            team.store.create_member_group(team.admin['id'], name)
    with pytest.raises(ValidationError, match='group_name_in_use'):
        team.store.create_member_group(team.admin['id'], 'Research')
    exported = team.store.export_config()
    assert set(exported) == {'schema', 'version', 'profiles', 'proxies'}
    assert team.group['id'] not in json.dumps(exported)
    with pytest.raises(ValidationError):
        team.store.export_config({'groups'})


def test_disabled_member_and_deleted_profile_do_not_keep_group_access(team):
    group = add_members(team)
    team.store.save_group_grants(team.admin['id'], group['id'],
                                {team.profile['id']: ['view']}, expected_revision=group['revision'])
    team.store.set_member_active(team.admin['id'], team.members['viewer']['id'], False)
    with pytest.raises(ValidationError, match='member_disabled'):
        team.store.visible_profiles(team.members['viewer']['id'])
    team.store.delete_profile(team.profile['id'])
    assert team.store.list_group_grants(group['id']) == []


def test_v2_migration_keeps_identity_direct_grants_and_logins(team):
    operator = team.members['operator']
    team.store.grant_profile(team.admin['id'], operator['id'], team.profile['id'], ['view'])
    team.store.set_identity(team.profile['id'], {'config': {'navigator.platform': 'Win32'}})
    before = team.store.get_profile(team.profile['id'])
    opened = team.store.login('operator', 'group-test-password-1')
    with team.store._write() as (connection, _):
        for table in ('team_group_grants', 'team_group_members', 'team_groups'):
            connection.execute(f'DROP TABLE {table}')
        connection.execute("UPDATE workspace_metadata SET value='2' WHERE key='team_schema_version'")
    migrated = ProfileStore(team.store.root)
    assert migrated.list_member_groups() == []
    assert migrated.get_profile(team.profile['id']) == before
    assert migrated.resolve_session(opened['session_id'])['permissions'][team.profile['id']] == {'view'}
    with migrated._connect() as connection:
        assert connection.execute(
            "SELECT value FROM workspace_metadata WHERE key='team_schema_version'").fetchone()[0] == '6'


def test_group_visibility_respects_archive_and_not_environment_labels(team):
    group = add_members(team)
    team.store.save_group_grants(team.admin['id'], group['id'], {team.profile['id']: ['view']},
                                expected_revision=group['revision'])
    team.store.archive_profile(team.profile['id'])
    assert team.store.visible_profiles(team.members['viewer']['id']) == []
    assert team.store.visible_profiles(team.members['viewer']['id'], archived=True)[0]['id'] == team.profile['id']
    assert team.store.visible_profiles(team.outsider['id'], archived=True) == []


def test_concurrent_group_updates_from_distinct_stores_have_one_winner(team):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    stores = [ProfileStore(team.store.root), ProfileStore(team.store.root)]

    def update(index):
        member = list(team.members.values())[index]
        barrier.wait(timeout=5)
        try:
            stores[index].set_group_members(team.admin['id'], team.group['id'], [member['id']],
                                           expected_revision=team.group['revision'])
            return 'saved'
        except ValidationError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == ['revision_conflict', 'saved']
    assert team.store.get_member_group(team.group['id'])['revision'] == 2
    assert len(team.store.list_group_members(team.group['id'])) == 1


def test_role_cap_does_not_disclose_ids_when_no_group_permission_applies(team):
    group = add_members(team)
    team.store.save_group_grants(team.admin['id'], group['id'], {team.profile['id']: ['operate']},
                                expected_revision=group['revision'])
    opened = team.store.login('viewer', 'group-test-password-1')
    assert team.store.resolve_session(opened['session_id'])['permissions'] == {}
    assert team.store.visible_profiles(team.members['viewer']['id']) == []
