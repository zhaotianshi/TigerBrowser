"""Kernel-owned process lifetimes for explicit team hosts, including manager crashes."""

import os
import json
import signal
import shutil
import tempfile
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from ..manifest import _host_id
from ..store import StoreError

MARKER = 'CAMOUFOX_TEAM_PID_NAMESPACE'
PARENT = 'CAMOUFOX_TEAM_PARENT_NAMESPACE'
HOST_PROC = 'CAMOUFOX_TEAM_HOST_PROC'
_job = None


def namespace_pidfd(raw):
    try:
        if len(raw) > 16384:
            raise ValueError
        pid = json.loads(raw)['child-pid']
        if type(pid) is not int or pid <= 0:
            raise ValueError
        return os.pidfd_open(pid)
    except (ValueError, KeyError, TypeError, OSError):
        raise StoreError('team_process_boundary_unavailable') from None


def ensure(arguments):
    if os.name != 'nt' and os.environ.get(MARKER) != '1':
        raise SystemExit(_supervise([sys.executable, '-m', 'camoufox', *arguments]))
    return enter()


def _supervise(arguments):
    reader, writer = os.pipe()
    process = None
    child = None
    pending = set()
    previous = {}
    def forward(number, _):
        if child is None:
            pending.add(number)
            return
        try:
            signal.pidfd_send_signal(child, number)
        except ProcessLookupError:
            pass
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, forward)
        with linux_boundary(arguments, info_fd=writer) as enclosed:
            process = subprocess.Popen(enclosed, pass_fds=(writer,), start_new_session=True)
            os.close(writer)
            writer = None
            with os.fdopen(reader, 'rb') as stream:
                reader = None
                raw = stream.read(16385)
            child = namespace_pidfd(raw)
            for number in pending:
                forward(number, None)
            # Only signal the exact namespace init. Killing the bwrap monitor or
            # exiting this wrapper first would abort otherwise clean shutdown.
            return process.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        for descriptor in (reader, writer, child):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


@contextmanager
def linux_boundary(arguments, *, info_fd=None):
    binary = shutil.which('bwrap')
    if not binary:
        raise StoreError('team_process_boundary_unavailable')
    with tempfile.TemporaryDirectory(prefix='camoufox-team-boundary-') as root:
        host_proc = Path(root) / 'host-proc'
        host_proc.mkdir(mode=0o700)
        yield [binary, *(['--info-fd', str(info_fd)] if info_fd is not None else []),
               '--unshare-user', '--unshare-pid', '--as-pid-1', '--die-with-parent',
               '--bind', '/', '/', '--ro-bind', '/proc', str(host_proc),
               '--proc', '/proc', '--dev-bind', '/dev', '/dev',
               '--cap-drop', 'ALL', '--setenv', MARKER, '1', '--setenv', PARENT,
               str(Path('/proc/self/ns/pid').stat().st_ino), '--setenv', HOST_PROC, str(host_proc),
               '--', *arguments]


def _linux_process(path):
    fields = path.read_text().rpartition(')')[2].split()
    return fields[0], int(fields[19])


def enter():
    if os.name == 'nt':
        global _job
        if _job is None:
            _job = WindowsJob()
        return {**_job.record, 'host_id': _host_id(create=True)}
    if os.getpid() != 1 or os.environ.get(MARKER) != '1':
        raise StoreError('team_process_boundary_unavailable')
    host_proc = Path(os.environ[HOST_PROC])
    pid = int(os.readlink(host_proc / 'self'))
    _, created = _linux_process(host_proc / str(pid) / 'stat')
    return {'kind': 'linux-pid-namespace', 'host_id': _host_id(create=True), 'pid': pid, 'created': created,
            'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'namespace': Path('/proc/self/ns/pid').stat().st_ino,
            'parent_namespace': int(os.environ[PARENT]), 'uid': os.getuid()}


def stopped(record):
    if record.get('host_id') != _host_id():
        raise StoreError('recovery_host_mismatch')
    if record.get('kind') == 'windows-job' and os.name == 'nt':
        return WindowsJob.stopped(record)
    if record.get('kind') != 'linux-pid-namespace' or os.name != 'posix':
        raise StoreError('recovery_boundary_unavailable')
    try:
        if str(UUID(record['boot'])) != record['boot']:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StoreError('recovery_record_invalid') from None
    if (type(record.get('parent_namespace')) is not int or type(record.get('uid')) is not int
            or type(record.get('namespace')) is not int or record['namespace'] <= 0
            or type(record.get('pid')) is not int or record['pid'] <= 0
            or type(record.get('created')) is not int or record['created'] <= 0):
        raise StoreError('recovery_record_invalid')
    if record['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        return True
    if (record['parent_namespace'] != Path('/proc/self/ns/pid').stat().st_ino
            or record['uid'] != os.getuid()):
        raise StoreError('recovery_namespace_mismatch')
    # This exact process was verified as namespace PID 1 at registration. Its
    # exit completes kernel teardown of all descendants before it becomes a zombie.
    try:
        state, created = _linux_process(Path('/proc') / str(record['pid']) / 'stat')
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        raise StoreError('recovery_process_inspection_failed') from None
    return created != record['created'] or state == 'Z'


def _windows_api():
    import ctypes as c
    from ctypes import wintypes as w
    api = c.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ((c.c_void_p, w.LPCWSTR), w.HANDLE),
        'SetInformationJobObject': ((w.HANDLE, c.c_int, c.c_void_p, w.DWORD), w.BOOL),
        'AssignProcessToJobObject': ((w.HANDLE, w.HANDLE), w.BOOL),
        'GetCurrentProcess': ((), w.HANDLE),
        'OpenProcess': ((w.DWORD, w.BOOL, w.DWORD), w.HANDLE),
        'GetProcessTimes': ((w.HANDLE, *([c.POINTER(w.FILETIME)] * 4)), w.BOOL),
        'WaitForSingleObject': ((w.HANDLE, w.DWORD), w.DWORD),
        'OpenJobObjectW': ((w.DWORD, w.BOOL, w.LPCWSTR), w.HANDLE),
        'QueryInformationJobObject': ((w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p), w.BOOL),
        'CloseHandle': ((w.HANDLE,), w.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(api, name)
        method.argtypes, method.restype = arguments, result
    return api


def _creation(api, handle):
    import ctypes as c
    from ctypes import wintypes as w
    times = [w.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(c.byref(value) for value in times)):
        raise StoreError('recovery_process_inspection_failed')
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


class WindowsJob:
    """The non-inheritable manager handle owns its entire descendant job tree."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                        ('minimum', c.c_size_t), ('maximum', c.c_size_t), ('active', w.DWORD),
                        ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class Extended(c.Structure):
            _fields_ = [('basic', Limits), ('io', c.c_uint64 * 6),
                        ('memory', c.c_size_t * 4)]

        api = _windows_api()
        name = 'Global\\TigerBrowser.' + uuid4().hex
        c.set_last_error(0)
        handle = api.CreateJobObjectW(None, name)
        if not handle or c.get_last_error() == 183:
            if handle:
                api.CloseHandle(handle)
            raise StoreError('team_process_boundary_unavailable')
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway flags.
        process = api.GetCurrentProcess()
        try:
            created = _creation(api, process)
            if (not api.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits))
                    or not api.AssignProcessToJobObject(handle, process)):
                raise StoreError('team_process_boundary_unavailable')
        except BaseException:
            api.CloseHandle(handle)
            raise
        # Do not close this handle while the manager is alive: it includes the manager.
        self.handle = handle
        self.record = {'kind': 'windows-job', 'job': name, 'pid': os.getpid(), 'created': created}

    @staticmethod
    def stopped(record):
        import ctypes as c
        api = _windows_api()
        pid, created, name = record.get('pid'), record.get('created'), record.get('job')
        if (type(pid) is not int or pid <= 0 or type(created) is not int or created <= 0
                or not isinstance(name, str) or not name.startswith('Global\\TigerBrowser.')
                or len(name.removeprefix('Global\\TigerBrowser.')) != 32
                or any(char not in '0123456789abcdef' for char in name.removeprefix('Global\\TigerBrowser.'))):
            raise StoreError('recovery_record_invalid')
        process = api.OpenProcess(0x100000 | 0x1000, False, pid)
        if process:
            try:
                if _creation(api, process) == created:
                    state = api.WaitForSingleObject(process, 0)
                    if state == 258:
                        return False
                    if state != 0:
                        raise StoreError('recovery_process_inspection_failed')
            finally:
                api.CloseHandle(process)
        elif c.get_last_error() != 87:
            raise StoreError('recovery_process_inspection_failed')
        job = api.OpenJobObjectW(4, False, name)
        if not job:
            if c.get_last_error() == 2:
                return True
            raise StoreError('recovery_process_inspection_failed')
        try:
            class Accounting(c.Structure):
                _fields_ = [('times', c.c_int64 * 4), ('page_faults', c.c_uint32),
                            ('total', c.c_uint32), ('active', c.c_uint32), ('terminated', c.c_uint32)]
            accounting = Accounting()
            if not api.QueryInformationJobObject(job, 1, c.byref(accounting), c.sizeof(accounting), None):
                raise StoreError('recovery_process_inspection_failed')
            return accounting.active == 0
        finally:
            api.CloseHandle(job)
