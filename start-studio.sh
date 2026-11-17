#!/usr/bin/env bash
set -euo pipefail

studio_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
studio_python="$studio_root/.venv-studio/bin/python"
if [[ ! -x "$studio_python" ]]; then
    printf '%s\n' 'Studio is not installed. Run python3 scripts/install-studio-ubuntu.py from this checkout.' >&2
    exit 1
fi
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    printf '%s\n' 'Open Studio from a graphical desktop session with DISPLAY or WAYLAND_DISPLAY.' >&2
    exit 1
fi
unset PYTHONHOME
export PYTHONPATH="$studio_root/pythonlib"
cd -- "$studio_root"
exec "$studio_python" -m camoufox.studio.desktop "$@"
