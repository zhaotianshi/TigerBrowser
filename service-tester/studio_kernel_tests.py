"""Opt-in real exact-build selection; downloads one digest-verified official host package."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from camoufox import multiversion
from camoufox.pkgman import RepoConfig, fetch_releases, launch_path, list_available_versions, verify_sha256, webdl
from camoufox.studio.kernels import kernel_id
from studio_runtime_tests import command, login, running_team, site_state, workspace  # noqa: F401


@pytest.fixture
def real_kernels(monkeypatch):
    selected = os.environ.get('CAMOUFOX_STUDIO_BINARY')
    other = os.environ.get('CAMOUFOX_STUDIO_SECOND_KERNEL')
    assert selected and Path(selected).is_file(), 'Set CAMOUFOX_STUDIO_BINARY to a registered browser.'
    assert other, 'Set CAMOUFOX_STUDIO_SECOND_KERNEL to a different official exact build.'
    original = [item for item in multiversion.list_installed()
                if Path(launch_path(item.path)).resolve() == Path(selected).resolve()]
    assert len(original) == 1
    current = original[0]
    config = RepoConfig.get_default()
    assert other.startswith(config.name.lower() + '/') and other != kernel_id(current)
    versions = list_available_versions(config, releases=fetch_releases(config))
    matches = [item for item in versions if item.version.full_string == other.split('/', 1)[1]]
    assert len(matches) == 1, 'The requested build must have one unambiguous host package.'
    asset = matches[0]
    assert asset.sha256 and asset.asset_size, 'The official package must publish its digest and size.'
    original_config = multiversion.CONFIG_FILE
    configuration = original_config.read_bytes() if original_config.exists() else None
    with tempfile.TemporaryDirectory(prefix='camoufox-kernel-runtime-',
                                      dir=os.environ.get('CAMOUFOX_STUDIO_KERNEL_TEMP')) as directory:
        root = Path(directory).resolve()
        print(f'Isolated kernel acceptance directory: {root}')
        with monkeypatch.context() as isolated:
            for name, target in (
                ('INSTALL_DIR', root), ('BROWSERS_DIR', root / 'browsers'),
                ('CONFIG_FILE', root / 'config.json'), ('REPO_CACHE_FILE', root / 'repo_cache.json'),
                ('COMPAT_FLAG', root / '.0.5_FLAG'),
            ):
                isolated.setattr(multiversion, name, target)
            shutil.copytree(current.path, root / 'browsers' / current.repo_name / current.path.name)
            archive = root / 'official-host.zip'
            with archive.open('w+b') as stream:
                webdl(asset.url, buffer=stream, bar=False, progress_callback=lambda *_: None, timeout=(30, 120))
                assert stream.seek(0, os.SEEK_END) == asset.asset_size
                verify_sha256(stream, asset.sha256, desc=other)
            multiversion.install_archive(archive, config.name.lower(), asset.to_metadata())
            installed = {kernel_id(item): item for item in multiversion.list_installed()}
            assert set(installed) == {kernel_id(current), other}
            yield installed[kernel_id(current)], installed[other]
        assert (original_config.read_bytes() if original_config.exists() else None) == configuration


def executing_builds(directory):
    paths = set()
    for child in psutil.Process().children(recursive=True):
        try:
            executable = Path(child.exe()).resolve()
        except psutil.NoSuchProcess:
            continue
        if executable.is_relative_to(directory):
            paths.add(executable)
    return paths


@pytest.mark.asyncio
async def test_real_exact_builds_keep_their_identity_when_the_new_profile_default_changes(workspace, real_kernels):
    store, _, _, first = workspace
    primary, secondary = real_kernels
    store.save_profile({'kernel': kernel_id(primary)}, first['id'])
    store.set_default_kernel(kernel_id(primary))
    async with running_team(store) as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'root', 'synthetic-admin-password')
            headers = {'Origin': origin, 'X-CSRF-Token': csrf}
            response = await client.post('/team/v1/profiles', headers=headers,
                json={'name': 'Second exact build', 'os': first['os'], 'kernel': kernel_id(secondary)})
            assert response.status_code == 200
            second = response.json()
            runs, states, identities = {}, {}, {}
            for profile in (first, second):
                profile_id = profile['id']
                runs[profile_id] = await command(service, client, csrf, profile_id, 'start')
                states[profile_id] = await site_state(service.engine.sessions[profile_id].context,
                    origin + '/synthetic-site', create=True)
                identities[profile_id] = store.get_profile(profile_id)['identity']
            assert identities[first['id']]['kernel'] == kernel_id(primary)
            assert identities[second['id']]['kernel'] == kernel_id(secondary)
            paths = {Path(launch_path(item.path)).resolve() for item in real_kernels}
            assert executing_builds(multiversion.BROWSERS_DIR) == paths

            response = await client.post('/team/v1/kernels/activate', headers=headers,
                json={'request_id': str(uuid4()), 'id': secondary.relative_path})
            assert response.status_code == 202
            await asyncio.wait_for(asyncio.shield(service.jobs[response.json()['id']]['task']), timeout=60)
            assert store.get_default_kernel() == kernel_id(secondary)
            response = await client.post('/team/v1/profiles', headers=headers,
                json={'name': 'New default build', 'os': first['os']})
            assert response.status_code == 200 and response.json()['kernel'] == kernel_id(secondary)
            for profile in (first, second):
                profile_id = profile['id']
                assert store.get_profile(profile_id)['identity'] == identities[profile_id]
                assert await site_state(service.engine.sessions[profile_id].context,
                    origin + '/synthetic-site') == states[profile_id]
                await command(service, client, csrf, profile_id, 'stop', runs[profile_id]['run_id'])
            restarted = await command(service, client, csrf, first['id'], 'start')
            assert executing_builds(multiversion.BROWSERS_DIR) == {Path(launch_path(primary.path)).resolve()}
            assert await site_state(service.engine.sessions[first['id']].context,
                origin + '/synthetic-site') == states[first['id']]
            assert store.get_profile(first['id'])['identity'] == identities[first['id']]
            await command(service, client, csrf, first['id'], 'stop', restarted['run_id'])
    assert executing_builds(multiversion.BROWSERS_DIR) == set()
