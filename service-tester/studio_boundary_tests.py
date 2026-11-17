"""Opt-in real kernel containment checks; detached children must die with their manager."""

import json
import os
import subprocess
import sys
import signal
import time
import socket
from pathlib import Path
from queue import Queue
from threading import Thread
from contextlib import nullcontext
import pytest

from camoufox.studio import recovery
from camoufox.studio.store import ProfileStore
from camoufox.studio.team.boundary import linux_boundary


def test_crashed_manager_contains_detached_descendants_and_recovers(tmp_path):
    ProfileStore(tmp_path)
    program = '''
import json, os, subprocess, sys
from pathlib import Path
from uuid import uuid4
from camoufox.studio.team.boundary import enter
from camoufox.studio.workspace import WorkspaceLock
boundary = enter()
lock = WorkspaceLock(Path(sys.argv[1]), boundary=boundary, execution_owner=str(uuid4()))
lock.acquire()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=os.name != 'nt',
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
print(json.dumps({'ready': True, 'child': child.pid}), flush=True)
sys.stdin.buffer.read(1)
os._exit(17)
'''
    arguments = [sys.executable, '-c', program, str(tmp_path)]
    enclosing = linux_boundary(arguments) if os.name != 'nt' else nullcontext(arguments)
    with enclosing as arguments:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            lines = Queue()
            Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            line = lines.get(timeout=15)
            assert json.loads(line)['ready'] is True
            assert recovery.inspect(tmp_path)['recoverable'] is False
            process.stdin.write(b'x')
            process.stdin.flush()
            assert process.wait(timeout=15) == 17
            assert recovery.inspect(tmp_path)['recoverable'] is True
            assert recovery.apply(tmp_path)['recovered_runs'] == 0
            assert not (tmp_path / 'workspace.lock').exists()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)


def test_real_linux_browser_crash_recovery_preserves_identity_and_site_data(tmp_path):
    if os.name != 'posix':
        pytest.fail('This isolated native-browser crash check requires Linux.')
    binary = os.environ.get('CAMOUFOX_STUDIO_BINARY')
    if not binary or not Path(binary).is_file():
        pytest.fail('Set CAMOUFOX_STUDIO_BINARY to the installed native Camoufox binary.')
    store = ProfileStore(tmp_path)
    store.save_settings({'executable_path': binary})
    store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'synthetic-admin-password')
    profile = store.save_profile({'name': 'Crash validation', 'os': 'linux', 'fingerprint': {
        'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080}})
    program = '''
import asyncio, json, os, sys, time
from uuid import uuid4
from camoufox.studio.team.boundary import enter
from camoufox.studio.team.service import TeamService
from camoufox.studio.access import MemberStore
async def main():
    service=TeamService(sys.argv[1],browser_mode='remote',execution_boundary=enter())
    await service.open()
    access=MemberStore(service.store,service.store.login('root','synthetic-admin-password'))
    command=await service.submit(access,sys.argv[2],'start',{'request_id':str(uuid4())})
    await asyncio.wait_for(service.tasks[command['id']],90)
    session=service.engine.sessions[sys.argv[2]]
    if sys.argv[3]=='write':
        await session.context.add_cookies([{'name':'crash-validation','value':'synthetic',
            'url':'http://crash-validation.invalid','expires':time.time()+3600}])
        assert any(c['name']=='crash-validation' for c in await session.context.cookies())
        # Commit the browser's asynchronous cookie store before testing crash
        # preservation; an unflushed memory-only write is not durable state.
        stopped=await service.submit(access,sys.argv[2],'stop',
            {'request_id':str(uuid4()),'run_id':command['run_id']})
        await service.tasks[stopped['id']]
        command=await service.submit(access,sys.argv[2],'start',{'request_id':str(uuid4())})
        await asyncio.wait_for(service.tasks[command['id']],90)
    else:
        present=any(c['name']=='crash-validation' and c['value']=='synthetic' for c in await session.context.cookies())
        if not present:
            await service.close()
            raise AssertionError('Committed site state was lost after recovery.')
    print(json.dumps({'ready':True}),flush=True)
    await asyncio.to_thread(sys.stdin.buffer.read,1)
    if sys.argv[3]=='write':
        os._exit(17)
    await service.close()
asyncio.run(main())
'''
    for mode in ('write', 'read'):
        arguments = [sys.executable, '-c', program, str(tmp_path), profile['id'], mode]
        with linux_boundary(arguments) as enclosed:
            process = subprocess.Popen(enclosed, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                lines = Queue()
                errors = []
                Thread(target=lambda stream=process.stderr, output=errors:
                       output.extend(iter(stream.readline, b'')), daemon=True).start()
                def read_ready(stream=process.stdout, output=lines):
                    for line in iter(stream.readline, b''):
                        if line.startswith(b'{'):
                            output.put(line)
                            return
                    output.put(b'')
                Thread(target=read_ready, daemon=True).start()
                assert json.loads(lines.get(timeout=90))['ready'] is True
                current_identity = store.get_profile(profile['id'])['identity']
                if mode == 'write':
                    identity = current_identity
                    assert recovery.inspect(tmp_path)['recoverable'] is False
                else:
                    assert current_identity == identity
                process.stdin.write(b'x')
                process.stdin.flush()
                assert process.wait(timeout=30) == (17 if mode == 'write' else 0)
                if mode == 'write':
                    assert recovery.inspect(tmp_path)['recoverable'] is True
                    assert recovery.apply(tmp_path)['recovered_runs'] == 1
                assert not (tmp_path / 'workspace.lock').exists()
            except Exception as error:
                detail = b''.join(errors)[-16384:].decode('utf-8', 'replace')
                raise AssertionError('Native crash validation failed: ' + detail) from error
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=15)


@pytest.mark.parametrize('requested_signal', [signal.SIGINT, signal.SIGTERM])
def test_linux_cli_interrupt_waits_for_clean_workspace_release(tmp_path, requested_signal):
    if os.name != 'posix':
        pytest.fail('Run the Linux CLI signal regression on Linux.')
    store = ProfileStore(tmp_path)
    store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'synthetic-admin-password')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    process = subprocess.Popen([sys.executable, '-m', 'camoufox', 'studio-team', '--data-dir', str(tmp_path),
                                '--origin', origin],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / 'workspace.lock/owner.json').is_file():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        process.send_signal(requested_signal)
        process.wait(timeout=15)
        assert not (tmp_path / 'workspace.lock').exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)
