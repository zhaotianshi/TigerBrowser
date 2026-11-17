"""Bounded per-profile file exchange; directory descriptors fence links and path races."""

import errno
import os
import stat
from contextlib import contextmanager
from uuid import uuid4

from ..backup import BackupError, checked_name
from ..engine import StudioError

LIMIT = 8 * 1024 * 1024


class PosixDirectory:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def names(self):
        return os.listdir(self.descriptor)

    def info(self, name):
        return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)

    def open(self, name, *, create=False):
        flags = ((os.O_WRONLY | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY | os.O_NONBLOCK)
        return os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.descriptor)

    def commit(self, descriptor):
        os.fsync(descriptor)
        os.fsync(self.descriptor)

    def remove(self, name, descriptor):
        current, original = self.info(name), os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise StudioError('unsafe_file')
        os.unlink(name, dir_fd=self.descriptor)


def filename(name):
    try:
        checked_name(name)
    except BackupError:
        raise StudioError('invalid_file_name') from None
    if '/' in name or len(name.encode('utf-8')) > 200 or name.endswith(('.part', '.tmp')):
        raise StudioError('invalid_file_name')
    return name


@contextmanager
def directory(profile, kind):
    if kind not in ('Uploads', 'Downloads'):
        raise StudioError('invalid_file_directory')
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(profile, flags)
        try:
            descriptor = os.open(kind, flags, dir_fd=parent)
            try:
                yield PosixDirectory(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
    except OSError as error:
        code = ('file_not_found' if error.errno == errno.ENOENT else
                'unsafe_file' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'file_io_failed')
        raise StudioError(code) from None


def listing(profile):
    with directory(profile, 'Downloads') as descriptor:
        result = []
        names = descriptor.names()
        if len(names) > 1000:
            raise StudioError('file_capacity')
        for name in sorted(names):
            try:
                filename(name)
            except StudioError:
                continue
            try:
                info = descriptor.info(name)
            except StudioError as error:
                if str(error) == 'unsafe_file':
                    continue
                raise
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= LIMIT:
                result.append({'name': name, 'size': info.st_size})
        return result


def download(profile, name):
    name = filename(name)
    with directory(profile, 'Downloads') as parent:
        descriptor = parent.open(name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
                raise StudioError('unsafe_file')
            data = stream.read(LIMIT + 1)
            after = os.fstat(stream.fileno())
            if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise StudioError('file_changed')
            return data


def upload(profile, name, data):
    name = filename(name)
    if not 0 < len(data) <= LIMIT:
        raise StudioError('file_capacity')
    # A server-generated prefix avoids overwriting a browser-visible file or interpreting its name as a path.
    name = str(uuid4()) + '-' + name
    with directory(profile, 'Uploads') as parent:
        names = parent.names()
        if len(names) >= 128:
            raise StudioError('file_capacity')
        used = sum(parent.info(item).st_size for item in names)
        if used + len(data) > 128 * 1024 * 1024:
            raise StudioError('file_capacity')
        descriptor = parent.open(name, create=True)
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(data)
                stream.flush()
                parent.commit(stream.fileno())
        except BaseException:
            parent.remove(name, descriptor)
            raise
        finally:
            os.close(descriptor)
    return {'name': name, 'size': len(data)}
