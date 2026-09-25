from __future__ import annotations

import json
import subprocess

from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("system")

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$procs = @(Get-CimInstance Win32_Processor)
$cpuAvg = $null
if ($procs.Count -gt 0) {
  $loads = @($procs | Where-Object { $_.LoadPercentage -ne $null } | Select-Object -ExpandProperty LoadPercentage)
  if ($loads.Count -gt 0) { $cpuAvg = [math]::Round(($loads | Measure-Object -Average).Average, 0) }
  elseif ($null -ne $procs[0].LoadPercentage) { $cpuAvg = [math]::Round([double]$procs[0].LoadPercentage, 0) }
}
$memUsed = [math]::Round(([double]($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)) / 1MB, 1)
$memTotal = [math]::Round([double]$os.TotalVisibleMemorySize / 1MB, 1)
[pscustomobject]@{
  cpu        = if ($null -eq $cpuAvg) { $null } else { [int]$cpuAvg }
  mem_used_gb = $memUsed
  mem_total_gb = $memTotal
  os         = "$($os.Caption) $($os.OSArchitecture)"
  kernel     = $os.Version
} | ConvertTo-Json -Compress
"""


def system_info(components: list | None = None) -> ToolResult:
    wanted = set(components or ["cpu", "memory", "os"])
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=25,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="System query timed out after 25s.")
    except OSError as exc:
        return ToolResult(False, error=f"Failed to query system: {exc}")

    if proc.returncode != 0:
        return ToolResult(False, error=(proc.stderr or proc.stdout or f"Exit code {proc.returncode}.").strip()[:500])

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ToolResult(False, error=f"Could not parse system output: {proc.stdout[:200]}")

    parts = []
    if "cpu" in wanted and data.get("cpu") is not None:
        parts.append(f"CPU: {data['cpu']}%")
    if "memory" in wanted:
        parts.append(f"Memory: {data.get('mem_used_gb')}/{data.get('mem_total_gb')} GB used")
    if "os" in wanted and data.get("os"):
        parts.append(f"OS: {data['os']}")
    if not parts:
        return ToolResult(False, error=f"No requested components returned: {list(wanted)}")
    return ToolResult(True, output=", ".join(parts))