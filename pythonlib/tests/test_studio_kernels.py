"""A profile selects an exact installed build independently of global activation."""

import pytest

from camoufox import multiversion
from camoufox.pkgman import Version
from camoufox.studio import kernels
from camoufox.studio.store import ProfileStore, ValidationError
from test_studio_team_service import workspace


@pytest.fixture
def installed(tmp_path, monkeypatch):
    versions = []
    for number, build in enumerate(('beta.31', 'beta.32')):
        path = tmp_path / build
        path.mkdir()
        (path / 'browser').touch()
        versions.append(multiversion.InstalledVersion('official', Version(build, '152.0.4'), path))
    monkeypatch.setattr(multiversion, 'list_installed', lambda: versions)
    monkeypatch.setattr(kernels, 'launch_path', lambda path: str(path / 'browser'))
    return versions


def test_exact_identity_build_wins_and_missing_build_fails(installed):
    for version in installed:
        identifier = kernels.kernel_id(version)
        profile = {'kernel': 'official/152.0.4-beta.999',
                   'identity': {'kernel': identifier, 'browser_version': '152.0.4'}}
        assert kernels.resolve_kernel(profile) == (version.path / 'browser', identifier)
    with pytest.raises(ValueError, match='kernel_not_installed'):
        kernels.resolve_kernel({'kernel': 'official/152.0.4-beta.999', 'identity': None})


def test_historical_identity_never_guesses_between_builds(installed):
    profile = {'kernel': '', 'identity': {'browser_version': '152.0.4'}}
    with pytest.raises(ValueError, match='kernel_ambiguous'):
        kernels.resolve_kernel(profile)
    profile['kernel'] = kernels.kernel_id(installed[0])
    assert kernels.resolve_kernel(profile)[0] == installed[0].path / 'browser'
    profile['kernel'] = ''
    installed.pop()
    assert kernels.resolve_kernel(profile)[1] == kernels.kernel_id(installed[0])


def test_historical_full_version_selects_only_its_build_without_rewriting_identity(installed):
    import copy
    profile = {'kernel': '', 'identity': {'browser_version': '152.0.4-beta.31'}}
    original = copy.deepcopy(profile)
    assert kernels.resolve_kernel(profile) == (installed[0].path / 'browser', kernels.kernel_id(installed[0]))
    assert profile == original
    installed.pop(0)
    with pytest.raises(ValueError, match='kernel_not_installed'):
        kernels.resolve_kernel(profile)


@pytest.mark.parametrize('duplicate_repository', [False, True])
def test_historical_full_version_rejects_duplicate_builds(installed, duplicate_repository):
    installed[1].version = installed[0].version
    if duplicate_repository:
        installed[1].repo_name = 'other'
    with pytest.raises(ValueError, match='kernel_ambiguous'):
        kernels.resolve_kernel({'kernel': '', 'identity': {'browser_version': '152.0.4-beta.31'}})


def test_same_build_with_two_asset_revisions_is_ambiguous(installed):
    installed[1].version = installed[0].version
    with pytest.raises(ValueError, match='kernel_ambiguous'):
        kernels.resolve_kernel({'kernel': kernels.kernel_id(installed[0]), 'identity': None})


def test_default_migration_is_once_and_preserves_historical_identities(installed, tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / 'workspace')
    first = store.save_profile({'name': 'Unstarted'})
    historical = store.save_profile({'name': 'Historical'})
    identity = {'browser_version': '152.0.4', 'config': {'navigator.platform': 'Win32'}}
    store.set_identity(historical['id'], identity)
    monkeypatch.setattr(multiversion, 'get_active_path', lambda **_: installed[0].path)
    kernels.initialize_default(store)
    assert store.get_profile(first['id'])['kernel'] == kernels.kernel_id(installed[0])
    assert store.get_profile(historical['id'])['identity'] == identity
    assert store.get_profile(historical['id'])['kernel'] == ''
    monkeypatch.setattr(multiversion, 'get_active_path', lambda **_: installed[1].path)
    kernels.initialize_default(store)
    assert store.get_default_kernel() == kernels.kernel_id(installed[0])


def test_quarantined_copy_is_not_rewritten_by_kernel_initialization(installed, tmp_path, monkeypatch):
    import json
    store = ProfileStore(tmp_path / 'quarantined')
    profile = store.save_profile({'name': 'Unstarted copy'})
    with store._write() as (connection, _):
        connection.execute("INSERT INTO workspace_metadata VALUES ('execution_barrier', ?)",
                           (json.dumps({'reason': 'restore_quarantine'}),))
    monkeypatch.setattr(multiversion, 'get_active_path', lambda **_: installed[0].path)
    kernels.initialize_default(store)
    assert store.get_default_kernel() == ''
    assert store.get_profile(profile['id'])['kernel'] == ''


def test_kernel_change_resets_identity_but_preserves_site_data(tmp_path):
    store = ProfileStore(tmp_path)
    store.set_default_kernel('official/152.0.4-beta.31')
    profile = store.save_profile({'name': 'Pinned'})
    assert profile['kernel'] == 'official/152.0.4-beta.31'
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'saved-site-data')
    store.set_identity(profile['id'], {'kernel': profile['kernel'], 'config': {'navigator.platform': 'Win32'}})
    store.set_default_kernel('official/152.0.4-beta.32')
    assert store.get_profile(profile['id'])['identity'] is not None
    updated = store.save_profile({'kernel': 'official/152.0.4-beta.32'}, profile['id'])
    assert updated['identity'] is None
    assert (directory / 'cookies.sqlite').read_bytes() == b'saved-site-data'
    assert store.save_profile({'name': 'New'})['kernel'] == 'official/152.0.4-beta.32'
    for invalid in ('../browser', 'official/../../browser', '/tmp/browser', None, ''):
        with pytest.raises(ValidationError):
            store.save_profile({'kernel': invalid}, profile['id'])


def test_default_addon_paths_are_resolved_on_the_current_host(tmp_path, monkeypatch):
    from camoufox import addons
    from camoufox.studio import distribution
    monkeypatch.setattr(addons, 'ADDONS_DIR', tmp_path / 'current-addons')
    store = ProfileStore(tmp_path / 'workspace')
    identity = {'config': {'addons': [r'C:\old-host\addons\UBO']}}
    assert distribution.profile_addon_paths(store, identity) == distribution.default_addon_paths()


def test_new_default_does_not_interrupt_an_existing_team_run(workspace, installed, monkeypatch):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from camoufox.studio.team.service import create_app
    from test_studio_team_automation import engine_factory
    from test_studio_team_service import ORIGIN, login, mutation, settle
    store, _, _, _, profile, _ = workspace
    store.set_default_kernel(kernels.kernel_id(installed[0]), initialize=True)
    application = create_app(store.root, origin=ORIGIN, browser_mode='headless', engine_factory=engine_factory)
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = login(client)
        command = mutation(client, csrf, '/profiles/' + profile['id'] + '/start', {'request_id': str(uuid4())}).json()
        assert settle(client, command)['state'] == 'succeeded'
        response = mutation(client, csrf, '/kernels/activate', {'request_id': str(uuid4()), 'id': installed[1].relative_path})
        assert response.status_code == 202, response.text
        service = application.state.team
        client.portal.call(lambda: service.jobs[response.json()['id']]['task'])
        assert store.get_default_kernel() == kernels.kernel_id(installed[1])
        assert store.get_profile(profile['id'])['kernel'] == kernels.kernel_id(installed[0])
        assert service.repository.latest_run(profile['id'])['state'] == 'running'
