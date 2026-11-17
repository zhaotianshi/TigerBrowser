"""Durable workspace ownership without a Qt or network dependency."""

import json
import os
from pathlib import Path
from uuid import uuid4


class WorkspaceLockError(RuntimeError):
    """The workspace cannot safely be claimed or released."""


class WorkspaceLock:
    """Reserve the legacy lock path until the owner confirms clean shutdown."""

    def __init__(self, root: Path, *, boundary=None, execution_owner=None):
        self.path = Path(root).expanduser().resolve() / 'workspace.lock'
        self._identity = None
        self._owner = None
        self.boundary = boundary
        self.execution_owner = execution_owner

    def acquire(self):
        if self._identity is not None:
            raise WorkspaceLockError('此实例已持有工作区锁，不能重复获取。')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A directory at the old QLockFile path also blocks legacy desktops.
            # Keep it after a crash: a dead manager does not prove its browsers died.
            self.path.mkdir(mode=0o700)
        except FileExistsError:
            raise WorkspaceLockError(
                '工作区已被占用，或上次退出未完成。请先确认原管理器、浏览器和代理均已停止；'
                '不要在进程仍运行时删除 workspace.lock。'
            ) from None
        except OSError:
            raise WorkspaceLockError('无法创建工作区锁，请检查本地目录及其写入权限。') from None
        owner = {'version': 1, 'pid': os.getpid(), 'owner': uuid4().hex}
        if self.boundary is not None:
            owner.update(version=2, boundary=self.boundary, execution_owner=self.execution_owner)
        try:
            with (self.path / 'owner.json').open('x', encoding='utf-8') as stream:
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
            stat = self.path.stat()
        except OSError:
            raise WorkspaceLockError('无法保存工作区占用记录，已保留锁供恢复检查。') from None
        self._identity = stat.st_dev, stat.st_ino
        self._owner = owner

    def release(self):
        if self._identity is None:
            return
        try:
            stat = self.path.lstat()
            owner_path = self.path / 'owner.json'
            if (self.path.is_symlink() or (stat.st_dev, stat.st_ino) != self._identity
                    or owner_path.is_symlink()
                    or json.loads(owner_path.read_text(encoding='utf-8')) != self._owner):
                raise ValueError('ownership changed')
            owner_path.unlink()
            self.path.rmdir()
        except (OSError, ValueError):
            raise WorkspaceLockError('工作区占用记录已变化或无法删除，请检查后再恢复使用。') from None
        self._identity = self._owner = None
