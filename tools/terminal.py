from __future__ import annotations

import subprocess

from config.permissions import ToolBlockedError, destructive_command_reason
from config.settings import Settings
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("terminal")

PS_PREFIX = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]


class TerminalError(Exception):
    pass


def run_command(command: str, working_dir: str | None = None, timeout: int = 60, settings: Settings | None = None) -> ToolResult:
    if not command or not command.strip():
        return ToolResult(False, error="Empty command.")
    if settings is not None:
        reason = destructive_command_reason(command, settings.terminal_blocklist)
        if reason:
            raise ToolBlockedError(f"Command blocked by destructive-command policy (matched: {reason}).")
    else:
        raise TerminalError("terminal.run_command requires settings for blocklist protection.")

    cwd = working_dir or str(settings.scratch_dir)
    try:
        proc = subprocess.run(
            PS_PREFIX + [command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return ToolResult(False, error=f"PowerShell not found: {exc}")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Command timed out after {timeout}s.")
    except OSError as exc:
        return ToolResult(False, error=f"Failed to run command: {exc}")

    output = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return ToolResult(False, error=stderr or output or f"Exit code {proc.returncode}.", output=output, exit_code=proc.returncode)
    return ToolResult(True, output=output, exit_code=0)