from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("python_tool")


def run_script(script: str, args: list | None = None, timeout: int = 120) -> ToolResult:
    """Run a Python script with the Diablo interpreter.

    IMPORTANT: subprocess execution is NOT a security sandbox. A Python
    script has the same privileges as Diablo. Access is controlled by the
    permission system and working-directory restrictions, not by isolation.
    """
    target = Path(script)
    if not target.exists():
        return ToolResult(False, error=f"Script not found: {target}")
    if not target.is_file():
        return ToolResult(False, error=f"Not a file: {target}")

    argv = [sys.executable, str(target)] + (list(args) if args else [])
    try:
        proc = subprocess.run(
            argv,
            cwd=str(target.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Script timed out after {timeout}s.")
    except OSError as exc:
        return ToolResult(False, error=f"Failed to run script: {exc}")

    output = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return ToolResult(False, output=output, error=stderr or f"Exit code {proc.returncode}.", exit_code=proc.returncode)
    return ToolResult(True, output=output, exit_code=0)