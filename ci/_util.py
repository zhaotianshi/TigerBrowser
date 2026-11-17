"""Shared plumbing for the pipeline: paths, subprocess, JSON and logging.

Deliberately dependency-free (stdlib only) so the early steps can run on a bare
runner before anything is installed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "ci"

# Where a run scatters its intermediate state. Overridable so a local operator
# can keep several runs side by side, and so a sharded CI job can hand each
# shard its own directory.
WORK_DIR = Path(os.environ.get("CI_WORK_DIR", os.environ.get("HARNESS_WORK_DIR", REPO_ROOT / ".ci-work")))
RESULTS_DIR = Path(os.environ.get("CI_RESULTS_DIR", os.environ.get("HARNESS_EVIDENCE_DIR", WORK_DIR / "results")))

# Kept under the old name so harness code and existing call sites read the same.
EVIDENCE_DIR = RESULTS_DIR

# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_START = time.monotonic()


def log(msg: str, *, level: str = "INFO") -> None:
    # stderr, so a `--json` stdout stays machine-readable and every CLI here
    # composes with a pipe. Actions shows both streams either way.
    elapsed = time.monotonic() - _START
    print(f"[{elapsed:7.1f}s] {level:<5} {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    log(msg, level="FATAL")
    sys.exit(code)


# --------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def combined(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")


def run(
    cmd: Sequence[str] | str,
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    check: bool = False,
    capture: bool = True,
    tee: bool = False,
) -> Result:
    """Run a command. Never raises on a non-zero exit unless check=True."""
    shell = isinstance(cmd, str)
    printable = cmd if shell else " ".join(shlex.quote(c) for c in cmd)
    log(f"$ {printable}" + (f"  (cwd={cwd})" if cwd else ""))

    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            shell=shell,
            timeout=timeout,
            capture_output=capture and not tee,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        res = Result(124, out, err + f"\nTIMEOUT after {timeout}s")
        if check:
            die(f"command timed out: {printable}")
        return res

    res = Result(proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not res.ok:
        log(res.combined()[-4000:], level="ERROR")
        die(f"command failed ({res.code}): {printable}")
    return res


# --------------------------------------------------------------------------
# json / files
# --------------------------------------------------------------------------


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
