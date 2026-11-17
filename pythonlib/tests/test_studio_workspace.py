"""Cross-process workspace ownership shared by desktop and non-Qt hosts."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from camoufox.studio.workspace import WorkspaceLock, WorkspaceLockError


def test_lock_is_exclusive_and_reusable_after_clean_release(tmp_path):
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.acquire()
    with pytest.raises(WorkspaceLockError):
        second.acquire()
    second.release()
    with pytest.raises(WorkspaceLockError):
        first.acquire()
    first.release()
    second.acquire()
    second.release()
    assert not (tmp_path / 'workspace.lock').exists()


def test_concurrent_acquisition_has_exactly_one_owner(tmp_path):
    barrier = Barrier(8)
    locks = [WorkspaceLock(tmp_path) for _ in range(8)]

    def acquire(lock):
        barrier.wait(timeout=5)
        try:
            lock.acquire()
        except WorkspaceLockError:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            assert sum(executor.map(acquire, locks)) == 1
    finally:
        for lock in locks:
            lock.release()


def test_owner_record_has_no_credentials_and_is_not_reclaimed_by_age(tmp_path):
    lock = WorkspaceLock(tmp_path)
    lock.acquire()
    try:
        record = json.loads((tmp_path / 'workspace.lock/owner.json').read_text())
        assert set(record) == {'version', 'pid', 'owner'}
        assert record['version'] == 1
        assert record['pid'] == os.getpid()
        assert len(record['owner']) == 32
        os.utime(tmp_path / 'workspace.lock', (1, 1))
        with pytest.raises(WorkspaceLockError):
            WorkspaceLock(tmp_path).acquire()
    finally:
        lock.release()


@pytest.mark.parametrize('kind', ['legacy', 'directory', 'malformed'])
def test_existing_markers_are_never_automatically_removed(tmp_path, kind):
    path = tmp_path / 'workspace.lock'
    if kind == 'directory':
        path.mkdir()
    else:
        path.write_text('999999999\npython\nlocalhost\n' if kind == 'legacy' else 'invalid')
    with pytest.raises(WorkspaceLockError):
        WorkspaceLock(tmp_path).acquire()
    assert path.exists()


def test_release_refuses_replaced_ownership(tmp_path):
    lock = WorkspaceLock(tmp_path)
    lock.acquire()
    owner = tmp_path / 'workspace.lock/owner.json'
    record = json.loads(owner.read_text())
    record['owner'] = 'different-owner'
    owner.write_text(json.dumps(record))
    with pytest.raises(WorkspaceLockError):
        lock.release()
    assert owner.exists()


@pytest.mark.parametrize('qt_first', [False, True])
def test_legacy_qt_and_new_host_cannot_share_workspace(tmp_path, qt_first):
    from PySide6.QtCore import QLockFile

    qt = QLockFile(str(tmp_path / 'workspace.lock'))
    qt.setStaleLockTime(0)
    lock = WorkspaceLock(tmp_path)
    try:
        if qt_first:
            assert qt.tryLock(0)
            with pytest.raises(WorkspaceLockError):
                lock.acquire()
        else:
            lock.acquire()
            os.utime(tmp_path / 'workspace.lock', (1, 1))
            assert not qt.tryLock(0)
            assert not qt.removeStaleLockFile()
            assert (tmp_path / 'workspace.lock/owner.json').is_file()
    finally:
        qt.unlock()
        lock.release()


def test_crashed_owner_leaves_a_recovery_barrier(tmp_path):
    code = (
        'from pathlib import Path; import os, sys; '
        'from camoufox.studio.workspace import WorkspaceLock; '
        'lock = WorkspaceLock(Path(sys.argv[1])); lock.acquire(); os._exit(17)'
    )
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 17, result.stderr
    with pytest.raises(WorkspaceLockError):
        WorkspaceLock(tmp_path).acquire()
    assert (tmp_path / 'workspace.lock/owner.json').exists()


def test_another_process_cannot_acquire_live_workspace(tmp_path):
    lock = WorkspaceLock(tmp_path)
    lock.acquire()
    code = '''
import sys
from pathlib import Path
from camoufox.studio.workspace import WorkspaceLock, WorkspaceLockError
try:
    WorkspaceLock(Path(sys.argv[1])).acquire()
except WorkspaceLockError:
    raise SystemExit(0)
raise SystemExit(1)
'''
    try:
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    finally:
        lock.release()


def test_lock_refuses_permission_failure_without_changing_existing_data(tmp_path, monkeypatch):
    original = Path.mkdir

    def fail(path, *args, **kwargs):
        if path.name == 'workspace.lock':
            raise PermissionError('private filesystem detail')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', fail)
    with pytest.raises(WorkspaceLockError) as raised:
        WorkspaceLock(tmp_path).acquire()
    assert 'private filesystem detail' not in str(raised.value)
