from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(argv: list[str], cwd: str, timeout: int = 120) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
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