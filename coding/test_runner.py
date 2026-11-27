from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _bytecode_cache_prefix() -> str:
    """A throwaway location for CPython's bytecode cache.

    M2.14 Phase 10. A verifier must not be able to be fooled by a cache, and
    the default one can fool it. CPython validates a cached ``.pyc`` against
    the source's modification time in whole seconds *and* its byte size. An
    edit that preserves the file's length and lands in the same second as the
    previous run therefore looks unchanged, and the next run imports the stale
    module instead of the repaired one. For a human that is a confusing
    "my fix did nothing"; for the repair loop it is worse, because the model is
    told its correct repair failed and will try something else.

    Pointing ``PYTHONPYCACHEPREFIX`` at a scratch directory means the
    subprocess never consults the project's own cache, so what it imports is
    always what is on disk. Nothing in the project is deleted or modified; the
    caches it already has are simply not used for this run.
    """
    return tempfile.mkdtemp(prefix="diablo-pycache-")


def _run(argv: list[str], cwd: str, timeout: int = 120) -> tuple[bool, str, str]:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = _bytecode_cache_prefix()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return False, "", f"Timed out after {timeout}s."
    except OSError as exc:
        return False, "", f"Failed to run: {exc}"
    output = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, output, stderr or f"Exit code {proc.returncode}."
    return True, output, ""


def run_pytest(root: str, extra_args: list | None = None) -> tuple[bool, str, str]:
    return _run([sys.executable, "-m", "pytest", "-q"] + (extra_args or []), root, timeout=300)


def run_python_file(path: str) -> tuple[bool, str, str]:
    return _run([sys.executable, path], str(Path(path).parent), timeout=120)