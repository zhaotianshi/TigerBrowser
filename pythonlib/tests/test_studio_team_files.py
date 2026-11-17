"""Linux file gateways accept only bounded, regular, private-profile files."""

import os

import pytest

from camoufox.studio.engine import StudioError
from camoufox.studio.team import files


@pytest.fixture
def profile(tmp_path):
    if not hasattr(os, 'O_NOFOLLOW'):
        pytest.fail('File isolation validation requires Linux.')
    root = tmp_path / 'profile'
    (root / 'Downloads').mkdir(parents=True)
    (root / 'Uploads').mkdir()
    return root


def test_chinese_upload_and_download_roundtrip(profile):
    result = files.upload(profile, '中文文件.txt', '中文内容'.encode('utf-8'))
    saved = profile / 'Uploads' / result['name']
    assert saved.read_bytes() == '中文内容'.encode('utf-8')
    assert saved.stat().st_mode & 0o777 == 0o600
    saved.rename(profile / 'Downloads' / '中文文件.txt')
    assert files.listing(profile) == [{'name': '中文文件.txt', 'size': 12}]
    assert files.download(profile, '中文文件.txt') == '中文内容'.encode('utf-8')


@pytest.mark.parametrize('name', ['../secret', '/secret', 'a/b', 'a\\b', 'CON', 'unfinished.part', '\udcff'])
def test_filename_rejects_paths_and_unfinished_downloads(profile, name):
    with pytest.raises(StudioError, match='invalid_file_name'):
        files.download(profile, name)


def test_non_utf8_download_name_does_not_break_the_listing(profile):
    name = os.fsencode(profile / 'Downloads') + b'/invalid-\xff.txt'
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    assert files.listing(profile) == []


def test_symlinks_hardlinks_and_fifo_are_not_downloads(profile, tmp_path):
    private = tmp_path / 'secret'
    private.write_bytes(b'not shared')
    (profile / 'Downloads' / 'link').symlink_to(private)
    os.link(private, profile / 'Downloads' / 'hardlink')
    os.mkfifo(profile / 'Downloads' / 'pipe')
    assert files.listing(profile) == []
    for name in ('link', 'hardlink', 'pipe'):
        with pytest.raises(StudioError, match='unsafe_file'):
            files.download(profile, name)


def test_directory_symlink_does_not_expose_host_files(profile, tmp_path):
    target = tmp_path / 'private'
    target.mkdir()
    (target / 'secret.txt').write_bytes(b'not shared')
    (profile / 'Downloads').rmdir()
    (profile / 'Downloads').symlink_to(target)
    with pytest.raises(StudioError, match='unsafe_file'):
        files.listing(profile)


def test_missing_file_has_safe_public_error(profile):
    with pytest.raises(StudioError, match='file_not_found'):
        files.download(profile, 'missing.txt')


def test_file_size_and_upload_quota_are_enforced(profile, monkeypatch):
    monkeypatch.setattr(files, 'LIMIT', 4)
    with pytest.raises(StudioError, match='file_capacity'):
        files.upload(profile, 'oversize.txt', b'12345')
    (profile / 'Downloads' / 'large.txt').write_bytes(b'12345')
    assert files.listing(profile) == []
    with pytest.raises(StudioError, match='unsafe_file'):
        files.download(profile, 'large.txt')
    for number in range(128):
        (profile / 'Uploads' / str(number)).write_bytes(b'')
    with pytest.raises(StudioError, match='file_capacity'):
        files.upload(profile, 'full.txt', b'1')


def test_interrupted_upload_removes_only_its_own_file(profile, monkeypatch):
    retained = profile / 'Uploads' / 'retained.txt'
    retained.write_bytes(b'original')
    def fail(descriptor):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(StudioError, match='file_io_failed'):
        files.upload(profile, 'interrupted.txt', b'new')
    assert list((profile / 'Uploads').iterdir()) == [retained]
    assert retained.read_bytes() == b'original'
