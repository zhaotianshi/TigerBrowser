#!/usr/bin/env python3
"""Build Studio's pinned KasmVNC runtime on Ubuntu 24.04 x86_64."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / 'scripts/data/studio-kasmvnc.json'
PACKAGES = (
    'build-essential', 'autoconf', 'automake', 'libtool', 'pkg-config', 'flex', 'bison',
    'cmake', 'ninja-build', 'nasm', 'patch', 'xutils-dev', 'xtrans-dev',
    'x11proto-dev', 'libpixman-1-dev', 'libxfont-dev', 'libxkbfile-dev',
    'libxshmfence-dev', 'libepoxy-dev', 'libgbm-dev', 'libgl1-mesa-dev', 'mesa-common-dev',
    'libudev-dev', 'libpciaccess-dev', 'libdrm-dev', 'libxau-dev',
    'libxdmcp-dev', 'libxext-dev', 'libxtst-dev', 'libxdamage-dev', 'libxfixes-dev',
    'libxrender-dev', 'libxres-dev', 'libgnutls28-dev', 'libpng-dev',
    'libtiff-dev', 'libgif-dev', 'libavcodec-dev', 'libssl-dev',
    'libxrandr-dev', 'libxcursor-dev', 'libavformat-dev', 'libswscale-dev',
    'libva-dev', 'libfreetype-dev', 'libfontenc-dev', 'libx11-dev',
    'libcrypt-dev', 'libpam0g-dev', 'libxcvt-dev', 'xfonts-utils', 'xkb-data',
    'x11-xkb-utils',
)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install_system_dependencies():
    command = [] if os.geteuid() == 0 else ['sudo']
    subprocess.run([*command, 'apt-get', 'update', '--error-on=any'], check=True)
    subprocess.run([*command, 'apt-get', 'install', '-y', '--no-install-recommends', *PACKAGES], check=True)


def fetch(source, cache):
    target = cache / source['sha256']
    if target.exists():
        if digest(target) != source['sha256']:
            raise ValueError(f'Cached source digest mismatch: {target}')
        return target
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
        temporary = Path(output.name)
        try:
            with urllib.request.urlopen(source['url'], timeout=90) as response:
                shutil.copyfileobj(response, output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if digest(temporary) != source['sha256']:
            raise ValueError(f'Source digest mismatch: {source["url"]}')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def unpack(archive, target, *, strip_root=True):
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = []
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe source archive path.')
            parts = path.parts[1:] if strip_root else path.parts
            if not parts:
                continue
            members.append(member.replace(name=str(PurePosixPath(*parts))))
        source.extractall(target, members=members, filter='data')


def runtime_manifest(pins, tools, compiler):
    return {
        'schema': 1,
        'version': pins['version'],
        'upstream_revision': pins['upstream_revision'],
        'cache_fix_revision': pins['cache_fix_revision'],
        'private_ipc_fix_sha256': pins['private_ipc_fix_sha256'],
        'ffmpeg_loader_fix_sha256': pins['ffmpeg_loader_fix_sha256'],
        'binaries': {name: digest(tools / name) for name in ('Xkasmvnc', 'kasmvncpasswd')},
        'sources': pins['sources'],
        'compiler': compiler,
    }


def build(pins, cache, work, destination, jobs):
    archives = {name: fetch(source, cache) for name, source in pins['sources'].items()}
    for name, archive in archives.items():
        unpack(archive, work / name, strip_root=pins['sources'][name].get('strip_root', True))
    source = work / 'kasmvnc'
    fix = work / 'cache-fix'
    for name in ('EncCache.h', 'EncCache.cxx'):
        shutil.copyfile(fix / 'common/rfb' / name, source / 'common/rfb' / name)
    shutil.copytree(fix / 'tests/enccache', source / 'tests/enccache')
    prefix = work / 'dependencies'
    environment = os.environ.copy()
    environment.update({
        'CC': 'gcc', 'CXX': 'g++',
        'CMAKE_BUILD_PARALLEL_LEVEL': str(jobs), 'MAKEFLAGS': f'-j{jobs}',
        'PKG_CONFIG_PATH': ':'.join(str(prefix / path) for path in ('lib/pkgconfig', 'lib64/pkgconfig', 'share/pkgconfig')),
        'CPATH': str(prefix / 'include'),
        'LDFLAGS': f'-L{prefix}/lib -L{prefix}/lib64',
    })
    log = work / 'build.log'

    def run(arguments, cwd=work, *, extra_env=None):
        print(' '.join(str(argument) for argument in arguments), flush=True)
        with log.open('a') as output:
            result = subprocess.run([str(argument) for argument in arguments], cwd=cwd,
                                    env=environment | (extra_env or {}),
                                    stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f'Build command failed ({result.returncode}); inspect {log}')

    def cmake(name, *options):
        path = work / name
        run(['cmake', '-S', path, '-B', path / 'build', '-G', 'Ninja',
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_POSITION_INDEPENDENT_CODE=ON',
             f'-DCMAKE_INSTALL_PREFIX={prefix}', *options])
        run(['cmake', '--build', path / 'build'])
        run(['cmake', '--install', path / 'build'])

    patches = {
        'private_ipc_fix_sha256': PINS.with_name('studio-kasmvnc-private-runtime.patch'),
        'ffmpeg_loader_fix_sha256': PINS.with_name('studio-kasmvnc-ffmpeg-loader.patch'),
    }
    for field, patch in patches.items():
        if digest(patch) != pins[field]:
            raise ValueError(f'Runtime patch digest mismatch: {patch.name}')
        run(['patch', '-Np1', '-i', patch], cwd=source)
    for compiler in ('gcc', 'g++'):
        probe = work / f'private-ipc-{compiler}'
        run([compiler, '-Wall', '-Wextra', '-Werror', '-I', source / 'common/network',
             PINS.with_name('studio-kasmvnc-private-ipc.c'), '-o', probe])
        run([probe])
    loader_probe = work / 'ffmpeg-loader-test'
    run(['g++', '-std=c++20', '-O2', '-Wall', '-Wextra', '-I', source / 'common',
         PINS.with_name('studio-kasmvnc-ffmpeg-test.cxx'), source / 'common/rfb/ffmpeg.cxx',
         '-Wl,--wrap=dlopen', '-rdynamic', '-ldl', '-o', loader_probe])
    run([loader_probe])
    for missing in range(1, 5):
        run([loader_probe, str(missing)])
    cache_test = work / 'cache-test'
    run(['cmake', '-S', source / 'tests/enccache', '-B', cache_test, '-DENABLE_ASAN=ON'])
    run(['cmake', '--build', cache_test])
    run(['ctest', '--test-dir', cache_test, '--output-on-failure'],
        extra_env={'ASAN_OPTIONS': 'detect_leaks=1'})
    cmake('jpeg', '-DENABLE_SHARED=OFF', '-DWITH_TOOLS=OFF', '-DWITH_TESTS=OFF')
    run(['./configure', '--enable-static', '--disable-shared', '--enable-threading',
         f'--prefix={prefix}'], cwd=work / 'webp')
    run(['make', 'install'], cwd=work / 'webp')
    cmake('tbb', '-DTBB_TEST=OFF', '-DBUILD_SHARED_LIBS=OFF', '-DCMAKE_CXX_FLAGS=-Wno-stringop-overflow')
    cmake('cpuid', '-DLIBCPUID_ENABLE_TESTS=OFF', '-DLIBCPUID_BUILD_DRIVERS=OFF', '-DBUILD_SHARED_LIBS=OFF')
    cmake('fmt', '-DFMT_TEST=OFF', '-DBUILD_SHARED_LIBS=OFF')
    shutil.copytree(work / 'yuv/include', prefix / 'include', dirs_exist_ok=True)
    run(['make', '-f', 'linux.mk', 'CFLAGS=-O2 -fomit-frame-pointer -fPIC',
         'CXXFLAGS=-O2 -fomit-frame-pointer -fPIC'], cwd=work / 'yuv')
    shutil.copyfile(work / 'yuv/libyuv.a', prefix / 'lib/libyuv.a')
    run(['cmake', '-S', source, '-B', source, '-DCMAKE_BUILD_TYPE=Release',
         '-DENABLE_GNUTLS=OFF', '-DENABLE_DEBUG_ENCODERS=OFF',
         f'-DCMAKE_PREFIX_PATH={prefix}', f'-DCMAKE_LIBRARY_PATH={prefix}/lib;{prefix}/lib64',
         f'-DCMAKE_INCLUDE_PATH={prefix}/include',
         f'-DCMAKE_EXE_LINKER_FLAGS=-L{prefix}/lib -L{prefix}/lib64'])
    run(['cmake', '--build', source])
    xorg = source / 'unix/xserver'
    shutil.copytree(work / 'xorg', xorg, dirs_exist_ok=True)
    run(['patch', '-Np1', '-i', source / 'unix/xserver21.patch'], cwd=xorg)
    run(['autoreconf', '-fi'], cwd=xorg)
    run(['./configure', '--disable-config-udev', '--disable-dmx', '--disable-dri',
         '--disable-dri2', '--disable-kdrive', '--disable-static', '--disable-xephyr',
         '--disable-xinerama', '--disable-xnest', '--disable-xorg', '--disable-xvfb',
         '--disable-xwayland', '--disable-xwin', '--enable-glx', '--without-dtrace',
         '--with-sha1=libcrypto', '--with-xkb-bin-directory=/usr/bin',
         '--with-xkb-output=/var/lib/xkb', '--with-xkb-path=/usr/share/X11/xkb',
         '--with-default-font-path=built-ins'], cwd=xorg)
    # The upstream build removes this GCC false-positive fatal warning as well.
    for makefile in xorg.rglob('Makefile'):
        text = makefile.read_text()
        makefile.write_text(text.replace('-Werror=array-bounds', ''))
    run(['make', f'KASMVNC_COMMIT_ID={pins["version"].split("+", 1)[1]}'], cwd=xorg)
    destination.mkdir()
    for original, name in ((xorg / 'hw/vnc/Xvnc', 'Xkasmvnc'),
                           (source / 'unix/kasmvncpasswd/kasmvncpasswd', 'kasmvncpasswd'),
                           (source / 'unix/vncconfig/vncconfig', 'kasmvncconfig')):
        shutil.copyfile(original, destination / name)
        (destination / name).chmod(0o755)
        run(['strip', destination / name])
        linked = subprocess.check_output(['ldd', destination / name], text=True)
        if 'not found' in linked:
            raise ValueError(f'Missing runtime library: {linked}')
    version = subprocess.check_output([destination / 'Xkasmvnc', '-version'],
                                      stderr=subprocess.STDOUT, text=True)
    if f'KasmVNC {pins["version"].replace("+", ".")}' not in version:
        raise ValueError('The built display server reports an unexpected version.')
    compiler = subprocess.check_output(['g++', '--version'], text=True).splitlines()[0]
    metadata = runtime_manifest(pins, destination, compiler)
    (destination / 'studio-kasmvnc-runtime.json').write_text(json.dumps(metadata, indent=2) + '\n')
    sources = destination / 'sources'
    (sources / 'archives').mkdir(parents=True)
    (sources / 'scripts/data').mkdir(parents=True)
    for name, archive in archives.items():
        shutil.copyfile(archive, sources / 'archives' / pins['sources'][name]['sha256'])
    shutil.copyfile(__file__, sources / 'scripts/install-studio-kasmvnc.py')
    shutil.copyfile(PINS, sources / 'scripts/data/studio-kasmvnc.json')
    for patch in patches.values():
        shutil.copyfile(patch, sources / 'scripts/data' / patch.name)
    for name in ('studio-kasmvnc-private-ipc.c', 'studio-kasmvnc-ffmpeg-test.cxx'):
        shutil.copyfile(PINS.with_name(name), sources / 'scripts/data' / name)
    shutil.copyfile(source / 'LICENSE.TXT', destination / 'LICENSE.KasmVNC.txt')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True, help='New directory for the two tools and provenance.')
    parser.add_argument('--cache-dir', type=Path, default=Path.home() / '.cache/camoufox-studio/kasmvnc')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--install-system-deps', action='store_true', help='Install Ubuntu build/runtime libraries with sudo apt.')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'linux' or sys.version_info < (3, 12):
            raise ValueError('Run with Python 3.12+ inside Ubuntu 24.04 x86_64.')
        release = platform.freedesktop_os_release()
        if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04' or platform.machine() != 'x86_64':
            raise ValueError('The certified build target is Ubuntu 24.04 x86_64.')
        if not 1 <= args.jobs <= 8:
            raise ValueError('--jobs must be between 1 and 8.')
        destination = args.prefix.expanduser().resolve()
        if destination.exists():
            raise ValueError('--prefix must name a new directory; existing runtimes are never overwritten.')
        cache = args.cache_dir.expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(cache).free < 2 * 1024**3:
            raise ValueError('At least 2 GiB of free build space is required.')
        if args.install_system_deps:
            install_system_dependencies()
        pins = json.loads(PINS.read_text())
        # Retain a failed build for diagnostics; remove successful intermediate objects.
        work = Path(tempfile.mkdtemp(prefix='build-', dir=cache))
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.parent / (destination.name + '.building')
        if stage.exists():
            raise ValueError(f'Unfinished runtime directory exists: {stage}')
        build(pins, cache, work, stage, args.jobs)
        stage.rename(destination)
        shutil.rmtree(work)
        print(f'export CAMOUFOX_KASMVNC_TOOLS={destination}')
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f'KasmVNC installation failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
