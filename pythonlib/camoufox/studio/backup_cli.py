"""Explicit local maintenance commands; secrets are read from a separate protected key file."""

import json
import os
from pathlib import Path

import click

from .manifest import register
from .secrets import SecretStoreError
from .store import StoreError
from .workspace import WorkspaceLockError


def _backup():
    try:
        from . import backup
    except ModuleNotFoundError as error:
        if error.name == 'cryptography':
            raise click.ClickException("Install backup dependencies: pip install -e './pythonlib[team]'") from None
        raise
    return backup


def _key_file(path, *locations):
    backup = _backup()
    path = Path(path).absolute()
    backup._regular(path)
    path = path.resolve()
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise backup.BackupError('backup_key_permissions')
    if any(path.is_relative_to(location) for location in locations):
        raise backup.BackupError('backup_key_must_be_stored_separately')
    if path.stat().st_size != 16:
        raise backup.BackupError('invalid_backup_key')
    return path.read_bytes()


def _run(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except (ValueError, StoreError, SecretStoreError, WorkspaceLockError) as error:
        raise click.ClickException(str(error)) from None
    except OSError:
        raise click.ClickException('maintenance_filesystem_error') from None


def install(cli):
    @cli.command(name='studio-attest')
    @click.option('--data-dir', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--profile', 'profile_id', required=True)
    @click.option('--confirm-current-runtime', is_flag=True, required=True)
    def attest(data_dir, profile_id, confirm_current_runtime):
        """Record optional native diagnostics after two starts and four matching reads."""
        import asyncio
        from . import attestation
        from .team.boundary import ensure
        from .engine import StudioError

        if not confirm_current_runtime:
            raise click.ClickException('confirmation_required')
        arguments = ['studio-attest', '--data-dir', str(data_dir), '--profile', profile_id,
                     '--confirm-current-runtime']
        def accept():
            try:
                boundary = ensure(arguments)
                return asyncio.run(attestation.register(data_dir, profile_id, boundary=boundary))
            except StudioError as error:
                raise click.ClickException(str(error)) from None
        click.echo(json.dumps(_run(accept)))

    @cli.command(name='studio-recover')
    @click.option('--data-dir', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--apply', 'apply_recovery', is_flag=True,
                  help='Recover only a proven stopped team boundary; revoke logins and retain ownership evidence.')
    def recover(data_dir, apply_recovery):
        """Inspect crash ownership, or explicitly recover after every old child exited."""
        from . import recovery
        method = recovery.apply if apply_recovery else recovery.inspect
        click.echo(json.dumps(_run(method, data_dir)))

    @cli.command(name='studio-backup-key')
    @click.option('--output', required=True, type=click.Path(path_type=Path),
                  help='New key file in a protected vault directory, separate from backups and workspaces.')
    def key(output):
        """Generate a backup key without writing its value to the terminal."""
        backup = _backup()
        def create():
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(backup.Cobblestone128Encryptor.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        _run(create)
        click.echo('Backup key created. Keep it in a protected vault; Windows requires restrictive NTFS ACLs.')

    @cli.command(name='studio-backup')
    @click.option('--data-dir', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--output', required=True, type=click.Path(path_type=Path))
    @click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--for-migration', is_flag=True,
                  help='Record actual source artifacts and extension paths for a stopped server handover.')
    def create(data_dir, output, key_file, for_migration):
        """Create a verified encrypted snapshot while the workspace is stopped."""
        backup = _backup()
        key = _run(_key_file, key_file, data_dir.resolve(), output.resolve().parent)
        result = _run(backup.create_snapshot, data_dir, output, key, for_migration=for_migration)
        click.echo(json.dumps({'snapshot_id': result['snapshot_id'], 'state': 'verified'}))

    @cli.command(name='studio-restore')
    @click.option('--archive', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--destination', required=True, type=click.Path(path_type=Path))
    @click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def restore(archive, destination, key_file):
        """Restore to a new container/workspace directory; execution stays quarantined."""
        backup = _backup()
        key = _run(_key_file, key_file, archive.resolve().parent, destination.resolve())
        result = _run(backup.restore_snapshot, archive, destination, key)
        click.echo(json.dumps({'workspace': str(result), 'state': 'quarantined'}))

    @cli.command(name='studio-runtime-register')
    @click.option('--data-dir', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--confirm-current-runtime', is_flag=True, required=True,
                  help='Record present headless artifacts for stopped backup or migration; routine launch does not read this record.')
    def runtime(data_dir, confirm_current_runtime):
        """Record or update stopped artifacts used only by backup and migration."""
        if not confirm_current_runtime:
            raise click.ClickException('confirmation_required')
        click.echo(json.dumps(_run(register, data_dir)))

    @cli.command(name='studio-activate-restore')
    @click.option('--source', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--restored', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--retire-source', is_flag=True, required=True,
                  help='Permanently block the exact original workspace before activating this copy.')
    def activate(source, restored, retire_source):
        """Perform an exclusive same-host transfer, never activate a parallel clone."""
        backup = _backup()
        if not retire_source:
            raise click.ClickException('confirmation_required')
        _run(backup.activate_transfer, source, restored)
        click.echo('Restore activated for the registered headless runtime; the source workspace is retired.')

    @cli.command(name='studio-migration-prepare')
    @click.option('--restored', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--output', required=True, type=click.Path(path_type=Path))
    @click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--mode', type=click.Choice(['desktop', 'headless']), default='desktop', show_default=True)
    @click.option('--executable-path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--addon-path', nargs=2, multiple=True, metavar='OLD NEW',
                  help='Map a saved custom extension path to an identical installed extension directory.')
    @click.option('--accept-platform-change', is_flag=True,
                  help='Accept an OS change with the same CPU architecture and saved browser version.')
    @click.option('--confirm-current-runtime', is_flag=True, required=True,
                  help='Accept host and native font differences for this migration request.')
    def migration_prepare(restored, output, key_file, mode, executable_path, addon_path,
                          accept_platform_change, confirm_current_runtime):
        """Prepare an encrypted request on the new server; execution remains quarantined."""
        _backup()
        from . import migration
        if not confirm_current_runtime:
            raise click.ClickException('confirmation_required')
        key = _run(_key_file, key_file, restored.resolve(), output.resolve().parent)
        result = _run(migration.prepare, restored, output, key, mode=mode, executable_path=executable_path,
                      addon_paths=addon_path,
                      accept_platform_change=accept_platform_change)
        click.echo(json.dumps({**result, 'state': 'quarantined'}))

    @cli.command(name='studio-migration-retire')
    @click.option('--source', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--request', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--output', required=True, type=click.Path(path_type=Path))
    @click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--retire-source', is_flag=True, required=True,
                  help='Permanently block this stopped source before issuing evidence for one destination.')
    def migration_retire(source, request, output, key_file, retire_source):
        """Retire the old server workspace and publish an encrypted activation receipt."""
        _backup()
        from . import migration
        if not retire_source:
            raise click.ClickException('confirmation_required')
        key = _run(_key_file, key_file, source.resolve(), request.resolve().parent, output.resolve().parent)
        result = _run(migration.retire, source, request, output, key)
        click.echo(json.dumps({'snapshot_id': result['snapshot_id'], 'target_id': result['target_id'],
                               'state': 'source_retired'}))

    @cli.command(name='studio-migration-activate')
    @click.option('--restored', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option('--receipt', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def migration_activate(restored, receipt, key_file):
        """Activate the prepared destination after verifying its source-retirement receipt."""
        _backup()
        from . import migration
        key = _run(_key_file, key_file, restored.resolve(), receipt.resolve().parent)
        result = _run(migration.activate, restored, receipt, key)
        click.echo(json.dumps({'workspace': str(result), 'state': 'active'}))
