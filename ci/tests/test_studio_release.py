"""Publication requires a verified source, an owned draft and complete installer records."""

import runpy
from pathlib import Path

import pytest

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
