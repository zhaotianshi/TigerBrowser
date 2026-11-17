"""Publish complete, source-bound Studio installers without Actions artifact storage."""

import argparse
import json
import os
import runpy
import subprocess
import tomllib
from pathlib import Path

PLATFORMS = {
    'win-x86_64': 'windows-latest',
    'mac-arm64': 'macos-15',
    'mac-x86_64': 'macos-15-intel',
    'lin-x86_64': 'ubuntu-24.04',
}
ROOT = Path(__file__).resolve().parents[1]


def require_source(tag, version, ref, sha, checks):
    if ref != 'refs/heads/main' or tag != f'studio-v{version}':
        raise ValueError('Release main with the exact package version tag studio-vVERSION.')
    if not any(check.get('name') == 'All tests passed' and check.get('head_sha') == sha
               and check.get('status') == 'completed' and check.get('conclusion') == 'success'
               and check.get('app', {}).get('slug') == 'github-actions' for check in checks):
        raise ValueError('Run full CI successfully on this exact main commit before publishing.')


def require_draft(release, tag, sha):
    if (not release.get('isDraft') or release.get('tagName') != tag
            or release.get('targetCommitish') != sha):
        raise ValueError('Only this source commit\'s matching draft may be changed.')


def verify_packages(directory, tag, platforms):
    contract = runpy.run_path(str(ROOT / 'pythonlib/camoufox/studio/client_releases.py'))
    records = contract['write_manifest'](directory)
    if set(records) != set(platforms):
        raise ValueError('The release platform set is incomplete or unexpected.')
    expected = {'manifest.json'}
    for platform, record in records.items():
        if tag != 'studio-v' + record['version']:
            raise ValueError('Installer version does not match the release tag.')
        name = f"TigerBrowser-{record['version']}-{platform}{contract['SUFFIXES'][platform]}"
        if record['file'] != name or record['size'] >= 2 * 1024 ** 3:
            raise ValueError('Invalid release asset name or size.')
        expected.update((name, name + '.json'))
    if any(path.is_file() and path.name not in expected for path in directory.iterdir()):
        raise ValueError('Unexpected release asset.')
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'upload', 'publish'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--platform', choices=PLATFORMS)
    parser.add_argument('--directory', type=Path, default=Path('dist/studio-client'))
    args = parser.parse_args()
    tag, sha = args.tag, os.environ['GITHUB_SHA']
    if args.operation == 'prepare':
        version = tomllib.loads((ROOT / 'pythonlib/pyproject.toml').read_text(encoding='utf-8'))['tool']['poetry']['version']
        pages = json.loads(subprocess.check_output([
            'gh', 'api', '--paginate', '--slurp',
            f"repos/{os.environ['GITHUB_REPOSITORY']}/commits/{sha}/check-runs?filter=latest&per_page=100",
        ], text=True))
        checks = [check for page in pages for check in page['check_runs']]
        require_source(tag, version, os.environ['GITHUB_REF'], sha, checks)
        existing = subprocess.run(['git', 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}'],
                                  capture_output=True, text=True)
        if existing.returncode == 0 and existing.stdout.strip() != sha:
            raise ValueError('Existing tag belongs to a different source commit.')
        subprocess.run(['gh', 'release', 'create', tag, '--target', sha, '--draft', '--generate-notes',
                        '--title', f'TigerBrowser {version}', '--notes',
                        f'Client installers for source {sha}. Each installer has a SHA-256/protocol record.'], check=True)
        matrix = {'include': [{'platform': platform, 'runner': runner} for platform, runner in PLATFORMS.items()]}
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('matrix=' + json.dumps(matrix) + '\n')
        return
    release = json.loads(subprocess.check_output([
        'gh', 'release', 'view', tag, '--json', 'isDraft,tagName,targetCommitish',
    ], text=True))
    require_draft(release, tag, sha)
    if args.operation == 'upload':
        if not args.platform:
            parser.error('upload requires --platform')
        records = verify_packages(args.directory, tag, {args.platform})
        name = records[args.platform]['file']
        subprocess.run(['gh', 'release', 'upload', tag, str(args.directory / name),
                        str(args.directory / (name + '.json')), '--clobber'], check=True)
    else:
        args.directory.mkdir(parents=True)
        subprocess.run(['gh', 'release', 'download', tag, '--dir', str(args.directory)], check=True)
        verify_packages(args.directory, tag, set(PLATFORMS))
        subprocess.run(['gh', 'release', 'upload', tag, str(args.directory / 'manifest.json'), '--clobber'], check=True)
        subprocess.run(['gh', 'release', 'edit', tag, '--draft=false', '--latest'], check=True)


if __name__ == '__main__':
    main()
