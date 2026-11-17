"""Publication requires a verified source, an owned draft and complete installer records."""

import json
import runpy
import subprocess
from pathlib import Path

import pytest

from ci import studio_release
from ci.studio_release import PLATFORMS, require_draft, require_source, verify_packages


SHA = 'a' * 40
TAG = 'studio-v0.5.7'


def gate():
    return {'name': 'All tests passed', 'head_sha': SHA, 'status': 'completed',
            'conclusion': 'success', 'app': {'slug': 'github-actions'}}


@pytest.mark.parametrize('change', [{}, {'name': 'Selected stage passed (macos)'},
    {'head_sha': 'b' * 40}, {'status': 'in_progress'}, {'conclusion': 'failure'},
    {'conclusion': 'skipped'}, {'conclusion': 'cancelled'}, {'app': {'slug': 'another-app'}}])
def test_release_requires_the_complete_gate_for_its_exact_source(change):
    check = {**gate(), **change}
    if not change:
        require_source(TAG, '0.5.7', 'refs/heads/main', SHA, [check])
    else:
        with pytest.raises(ValueError, match='full CI'):
            require_source(TAG, '0.5.7', 'refs/heads/main', SHA, [check])


@pytest.mark.parametrize(('tag', 'ref'), [('studio-v0.5.6', 'refs/heads/main'),
    (TAG, 'refs/heads/feature'), ('studio-v0.5.7;command', 'refs/heads/main')])
def test_release_rejects_a_different_version_or_branch(tag, ref):
    with pytest.raises(ValueError):
        require_source(tag, '0.5.7', ref, SHA, [gate()])


def test_prepare_release_requests_merged_pull_requests_in_the_source_bound_draft(tmp_path, monkeypatch):
    repository = 'example/TigerBrowser'
    project = tmp_path / 'pythonlib'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[tool.poetry]\nversion = "0.5.7"\n', encoding='utf-8')
    github_output = tmp_path / 'output'
    for key, value in {'GITHUB_SHA': SHA, 'GITHUB_REF': 'refs/heads/main',
                       'GITHUB_REPOSITORY': repository, 'GITHUB_OUTPUT': str(github_output)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(studio_release, 'ROOT', tmp_path)
    monkeypatch.setattr('sys.argv', ['studio_release', 'prepare', '--tag', TAG])
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: json.dumps([{'check_runs': [gate()]}]))

    def run(command, **kwargs):
        if command[0] == 'git':
            return subprocess.CompletedProcess(command, 1)
        assert command[:4] == ['gh', 'release', 'create', TAG]
        assert command[command.index('--target') + 1] == SHA
        assert '--draft' in command
        assert '--generate-notes' in command
        notes = command[command.index('--notes') + 1]
        assert SHA in notes and 'SHA-256' in notes
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    studio_release.main()
    assert github_output.read_text(encoding='utf-8').startswith('matrix=')


@pytest.mark.parametrize('change', [{}, {'isDraft': False}, {'tagName': 'studio-v0.5.6'},
                                   {'targetCommitish': 'b' * 40}, {'targetCommitish': 'main'}])
def test_upload_and_publish_cannot_change_another_or_published_release(change):
    release = {'isDraft': True, 'tagName': TAG, 'targetCommitish': SHA}
    release.update(change)
    if not change:
        require_draft(release, TAG, SHA)
    else:
        with pytest.raises(ValueError, match='draft'):
            require_draft(release, TAG, SHA)


@pytest.fixture
def installers(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'pythonlib/camoufox/studio/client_releases.py'
    contract = runpy.run_path(str(source))
    for platform, suffix in contract['SUFFIXES'].items():
        path = tmp_path / f'TigerBrowser-0.5.7-{platform}{suffix}'
        path.write_bytes(b'synthetic-installer-contract')
        contract['describe'](path, platform, '0.5.7')
    return tmp_path, contract


def test_all_platforms_and_matching_digests_are_required_before_publication(installers):
    directory, contract = installers
    records = verify_packages(directory, TAG, set(PLATFORMS))
    assert set(records) == set(contract['SUFFIXES'])
    assert (directory / 'manifest.json').is_file()
    path = directory / records['win-x86_64']['file']
    path.write_bytes(path.read_bytes() + b'corrupt')
    with pytest.raises(ValueError, match='digest'):
        verify_packages(directory, TAG, set(PLATFORMS))


def test_partial_packages_cannot_be_published_as_a_complete_release(installers):
    directory, _ = installers
    for path in directory.glob('*mac-arm64*'):
        path.unlink()
    with pytest.raises(ValueError, match='platform'):
        verify_packages(directory, TAG, set(PLATFORMS))


def test_unknown_assets_and_different_versions_are_rejected(installers):
    directory, _ = installers
    with pytest.raises(ValueError, match='version'):
        verify_packages(directory, 'studio-v0.5.8', set(PLATFORMS))
    (directory / 'unexpected.txt').write_text('synthetic', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected'):
        verify_packages(directory, TAG, set(PLATFORMS))
