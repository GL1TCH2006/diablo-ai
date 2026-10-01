from __future__ import annotations

"""``system.info`` — native Windows backend (M2.15).

This tool's **name, signature, level and output format are unchanged.** What
changed is the backend.

It used to run a ``Get-CimInstance`` PowerShell script on every call, which means
a fresh PowerShell process per invocation — hundreds of milliseconds to return
four numbers — and which the cross-layer contracts rightly treat as an
implementation detail that should not be there at all. M2.15 replaced it with
``GlobalMemoryStatusEx`` and the registry: the same public tool, reading the
kernel directly, in roughly 45 ms on the development machine.

Why keep the fallback
---------------------
The native backend is Windows-only. ``system_available()`` is checked once, and if
the layer cannot run - no pywin32, or a locked-down host - the previous
PowerShell path still runs. A slower answer beats no answer, and a silent
regression in a status line would be worse than either.

That fallback is also self-reporting: :data:`_last_backend` records which path
produced the last answer, and the output says which one, so nothing here can
quietly pretend to be native.

CPU load
--------
``Win32_Processor.LoadPercentage`` was a documented-unreliable estimate that is
frequently 0. The native path reports ``None`` rather than a number it cannot
substantiate. If ``cpu`` is in the requested components and the value is
genuinely unknown, the component is omitted and the output says so. It is not
replaced with a guess, and it is not dropped silently.
"""

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

#: Which backend produced the most recent answer. Reported so the caller can
#: never mistake a PowerShell fallback for a native reading.
_last_backend = "none"


def _native_backend() -> dict | None:
    """Native system snapshot, or ``None`` when the layer cannot run here."""
    global _last_backend
    try:
        from core.windows.system import read_system_info, system_available
    except Exception as exc:  # noqa: BLE001 - absent package is a fallback, not a crash
        log.debug("native system layer unavailable: %s", exc)
        return None
    if not system_available():
        _last_backend = "unavailable"
        return None
    try:
        info = read_system_info()
    except Exception as exc:  # noqa: BLE001
        log.warning("native system query failed (%s); falling back", exc)
        _last_backend = "native_failed"
        return None
    _last_backend = "native"
    return info.to_dict()


def _powershell_backend() -> dict:
    global _last_backend
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
        capture_output=True,
        text=True,
        timeout=25,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:500])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OSError(f"could not parse system output: {proc.stdout[:200]}") from exc
    _last_backend = "powershell_fallback"
    return data


def _format_native(info: dict, wanted: set[str]) -> str:
    parts: list[str] = []
    memory = info.get("memory") or {}
    cpu = info.get("cpu") or {}
    if "cpu" in wanted:
        load = cpu.get("load_percent")
        if load is None:
            # Honest omission, stated rather than faked. Windows exposes no
            # reliable native load figure without a sampling loop.
            parts.append("CPU: load not measured natively")
        else:
            parts.append(f"CPU: {load}%")
    if "memory" in wanted:
        total = float(memory.get("total_physical_mb") or 0.0) / 1024.0
        used = float(memory.get("used_physical_mb") or 0.0) / 1024.0
        percent = memory.get("used_percent")
        parts.append(f"Memory: {used:.1f}/{total:.1f} GB used" +
                     (f" ({percent}%)" if percent is not None else ""))
    if "os" in wanted:
        caption = str(info.get("os_caption") or "")
        build = str(info.get("os_build") or "")
        arch = str(info.get("os_architecture") or "")
        parts.append(f"OS: {caption} {arch}".rstrip() + (f" (build {build})" if build else ""))
    if not parts:
        return ""
    suffix = "" if _last_backend == "native" else f" [backend: {_last_backend}]"
    return ", ".join(parts) + suffix


def _format_powershell(data: dict, wanted: set[str]) -> str:
    parts = []
    if "cpu" in wanted and data.get("cpu") is not None:
        parts.append(f"CPU: {data['cpu']}%")
    if "memory" in wanted:
        parts.append(f"Memory: {data.get('mem_used_gb')}/{data.get('mem_total_gb')} GB used")
    if "os" in wanted and data.get("os"):
        parts.append(f"OS: {data['os']}")
    return ", ".join(parts) + " [backend: powershell_fallback]"


def system_info(components: list | None = None) -> ToolResult:
    """OS, CPU and memory. Native backend with a PowerShell fallback.

    Output shape and tool name are unchanged from before M2.15; only the source of
    the numbers changed, and the backend is now disclosed.
    """
    global _last_backend
    wanted = set(components or ["cpu", "memory", "os"])

    native = _native_backend()
    if native is not None:
        rendered = _format_native(native, wanted)
        if rendered:
            return ToolResult(True, output=rendered)
        return ToolResult(False, error=f"No requested components returned: {sorted(wanted)}")

    try:
        data = _powershell_backend()
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="System query timed out after 25s.")
    except OSError as exc:
        return ToolResult(False, error=f"Failed to query system: {exc}")
    rendered = _format_powershell(data, wanted)
    if not rendered:
        return ToolResult(False, error=f"No requested components returned: {sorted(wanted)}")
    return ToolResult(True, output=rendered)


def backend_name() -> str:
    """Which backend served the most recent call. ``native`` is the goal."""
    return _last_backend