"""Maintenance commands keep key bytes out of arguments and require explicit transfer confirmation."""

import os
import subprocess
import sys

from click.testing import CliRunner

from camoufox.__main__ import cli


def test_key_generation_is_exclusive_and_never_prints_the_key(tmp_path):
    target = tmp_path / 'key'
    runner = CliRunner()
    result = runner.invoke(cli, ['studio-backup-key', '--output', str(target)])
    assert result.exit_code == 0, result.output
    key = target.read_bytes()
    assert len(key) == 16 and key.hex() not in result.output
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600
    result = runner.invoke(cli, ['studio-backup-key', '--output', str(target)])
    assert result.exit_code != 0 and target.read_bytes() == key


def test_transfer_and_runtime_registration_require_explicit_confirmation(tmp_path):
    runner = CliRunner()
    for arguments in (
        ['studio-runtime-register', '--data-dir', str(tmp_path)],
        ['studio-activate-restore', '--source', str(tmp_path), '--restored', str(tmp_path)],
    ):
        result = runner.invoke(cli, arguments)
        assert result.exit_code != 0 and 'Missing option' in result.output
    assert not list(tmp_path.iterdir())


def test_server_migration_requires_runtime_acceptance_and_source_retirement(tmp_path):
    key = tmp_path / 'key'
    key.write_bytes(b'x' * 16)
    request = tmp_path / 'request'
    request.write_bytes(b'not-yet-authenticated')
    for arguments in (
        ['studio-migration-prepare', '--restored', str(tmp_path), '--output', str(tmp_path / 'output'),
         '--key-file', str(key)],
        ['studio-migration-retire', '--source', str(tmp_path), '--request', str(request),
         '--output', str(tmp_path / 'output'), '--key-file', str(key)],
    ):
        result = CliRunner().invoke(cli, arguments)
        assert result.exit_code != 0 and 'Missing option' in result.output
    assert not (tmp_path / 'output').exists()


def test_general_cli_does_not_require_the_optional_cryptography_extra():
    code = '''
import sys
class NoCrypto:
    def find_spec(self, name, *args):
        if name == 'cryptography' or name.startswith('cryptography.'):
            raise ModuleNotFoundError("optional dependency unavailable", name='cryptography')
sys.meta_path.insert(0, NoCrypto())
from camoufox.__main__ import cli
from click.testing import CliRunner
assert CliRunner().invoke(cli, ['--help']).exit_code == 0
result = CliRunner().invoke(cli, ['studio-backup-key', '--output', 'not-created'])
assert result.exit_code != 0 and 'Install backup dependencies' in result.output
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_key_cannot_live_in_the_archive_directory(tmp_path):
    from camoufox.studio.backup_cli import _key_file

    key = tmp_path / 'key'
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(b'x' * 16)
    from camoufox.studio.backup import BackupError
    import pytest

    with pytest.raises(BackupError, match='stored_separately'):
        _key_file(key, tmp_path)
