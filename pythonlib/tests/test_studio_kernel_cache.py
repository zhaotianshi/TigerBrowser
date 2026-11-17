"""The server caches verified official packages for every client platform."""

import hashlib
import io
import json
import zipfile
from uuid import uuid4

import pytest

from camoufox import multiversion
from camoufox.studio import kernel_cache
from camoufox.studio.kernel_cache import KernelCache, KernelCacheError, PLATFORMS
from camoufox.studio.store import ProfileStore

KERNEL = 'official/152.0.4-beta.31'


def package(platform):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('camoufox-bin', platform)
    return data.getvalue()


PACKAGES = {platform: package(platform) for platform in PLATFORMS}


def release(build='beta.31', *, digests=True, skip=()):
    assets = []
    for platform, payload in PACKAGES.items():
        if platform in skip:
            continue
        os_name, arch = PLATFORMS[platform]
        name = f'camoufox-152.0.4-{build}-{os_name}.{arch}.zip'
        assets.append({'name': name, 'browser_download_url': 'https://example.test/' + platform,
                       'id': len(assets), 'size': len(payload), 'updated_at': '2026-10-01T00:00:00Z',
                       'created_at': '2026-10-01T00:00:00Z',
                       'digest': 'sha256:' + hashlib.sha256(payload).hexdigest() if digests else None})
    return {'prerelease': True, 'assets': assets}


@pytest.fixture
def remote(tmp_path, monkeypatch):
    state = {'releases': [release()], 'downloads': [], 'tamper': None}
    monkeypatch.setattr(kernel_cache, 'fetch_releases', lambda config: state['releases'])

    def download(url, *, buffer, timeout, **_):
        assert timeout == kernel_cache.TIMEOUT
        platform = url.rsplit('/', 1)[1]
        state['downloads'].append(platform)
        buffer.write(state['tamper'] if state['tamper'] and platform == 'mac-arm64' else PACKAGES[platform])
        buffer.seek(0)
        return buffer

    monkeypatch.setattr(kernel_cache, 'webdl', download)
    monkeypatch.setattr(multiversion, 'BROWSERS_DIR', tmp_path / 'browsers')
    monkeypatch.setattr(multiversion, 'COMPAT_FLAG', tmp_path / 'compat')
    monkeypatch.setattr(multiversion, 'CONFIG_FILE', tmp_path / 'config.json')
    return state


def test_all_platforms_are_cached_verified_and_host_installed_without_activation(tmp_path, remote):
    cache = KernelCache(tmp_path / 'kernels')
    entry = cache.cache(KERNEL)
    assert entry['complete'] and set(entry['platforms']) == set(PLATFORMS)
    for platform, payload in PACKAGES.items():
        path, record = cache.package(KERNEL, platform)
        assert path.read_bytes() == payload and record['sha256'] == hashlib.sha256(payload).hexdigest()
    installed = multiversion.list_installed()
    assert [item.version.full_string for item in installed] == ['152.0.4-beta.31']
    assert (installed[0].path / 'camoufox-bin').read_text() == kernel_cache.host_platform()
    assert not multiversion.CONFIG_FILE.exists()
    remote['downloads'].clear()
    cache.cache(KERNEL)
    assert remote['downloads'] == []


def test_assets_without_published_digests_are_never_cached(tmp_path, remote):
    remote['releases'] = [release(digests=False)]
    cache = KernelCache(tmp_path / 'kernels')
    with pytest.raises(KernelCacheError, match='kernel_digest_missing'):
        cache.cache(KERNEL)
    assert remote['downloads'] == [] and cache.list() == []
    assert not cache.available()[0]['complete']


def test_missing_platform_and_unofficial_repositories_are_rejected(tmp_path, remote):
    remote['releases'] = [release(skip={'mac-x86_64'})]
    cache = KernelCache(tmp_path / 'kernels')
    with pytest.raises(KernelCacheError, match='kernel_not_published'):
        cache.cache(KERNEL)
    for kernel, code in (('coryking/152.0.4-beta.31', 'kernel_repository_unsupported'),
                         ('official/../x', 'invalid_kernel'), ('', 'invalid_kernel')):
        with pytest.raises(KernelCacheError, match=code):
            cache.cache(kernel)


def test_tampered_download_leaves_no_listed_package(tmp_path, remote):
    remote['tamper'] = b'replaced package'
    cache = KernelCache(tmp_path / 'kernels')
    with pytest.raises(KernelCacheError, match='kernel_(size|digest)_mismatch'):
        cache.cache(KERNEL)
    with pytest.raises(KernelCacheError, match='kernel_not_cached'):
        cache.package(KERNEL, 'mac-arm64')
    assert not list((tmp_path / 'kernels').rglob('.download-*'))
    assert multiversion.list_installed() == []
    remote['tamper'] = None
    assert cache.cache(KERNEL)['complete']


@pytest.mark.parametrize('historical_version', ['152.0.4', '152.0.4-beta.31'])
def test_referenced_kernels_cannot_be_removed(tmp_path, remote, historical_version):
    cache = KernelCache(tmp_path / 'kernels')
    cache.cache(KERNEL)
    store = ProfileStore(tmp_path / 'workspace')
    profile = store.save_profile({'name': 'Pinned', 'kernel': KERNEL})
    with pytest.raises(KernelCacheError, match='kernel_in_use'):
        cache.remove(KERNEL, store.kernel_references())
    store.save_profile({'kernel': 'official/152.0.4-beta.32'}, profile['id'])
    store.set_identity(profile['id'], {'browser_version': historical_version, 'config': {'navigator.platform': 'Win32'}})
    with pytest.raises(KernelCacheError, match='kernel_in_use'):
        cache.remove(KERNEL, store.kernel_references())
    store.delete_profile(profile['id'])
    cache.remove(KERNEL, store.kernel_references())
    assert cache.list() == [] and multiversion.list_installed() == []


def test_service_downloads_packages_with_login_and_refuses_root_inside_workspace(tmp_path, remote):
    from starlette.testclient import TestClient
    from camoufox.studio.team.service import TeamService, create_app
    from test_studio_team_service import ORIGIN, PREFIX, engine_factory, login, mutation

    store = ProfileStore(tmp_path / 'workspace')
    store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'admin-password-1')
    with pytest.raises(ValueError):
        TeamService(store.root, kernel_root=store.root / 'kernels')
    application = create_app(store.root, origin=ORIGIN, engine_factory=engine_factory,
                             kernel_root=tmp_path / 'kernels')
    with TestClient(application, base_url=ORIGIN) as client:
        path = PREFIX + '/kernels/' + KERNEL + '/win-x86_64'
        assert client.get(path).status_code == 401
        csrf = login(client)
        assert client.get(path).json() == {'error': 'kernel_not_cached'}
        response = mutation(client, csrf, '/kernels/cache', {'request_id': str(uuid4()), 'kernel_id': KERNEL})
        assert response.status_code == 202, response.text
        client.portal.call(lambda: application.state.team.jobs[response.json()['id']]['task'])
        assert client.get(PREFIX + '/tasks/' + response.json()['id']).json()['state'] == 'succeeded'
        download = client.get(path)
        assert download.content == PACKAGES['win-x86_64']
        assert download.headers['x-content-sha256'] == hashlib.sha256(PACKAGES['win-x86_64']).hexdigest()
        inventory = client.get(PREFIX + '/kernels').json()
        assert [item['kernel_id'] for item in inventory['cache']] == [KERNEL]
        assert str(tmp_path) not in json.dumps(inventory)
        assert client.get(PREFIX + '/kernels/official/..%2F..%2Fsecret/win-x86_64').status_code in (400, 404)
