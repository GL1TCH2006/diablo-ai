"""M2.15 - Native Windows Systems Foundation.

A **capability layer** over Windows, not a change to how Diablo executes
anything. Everything below it - the Agent, the Executor, the permission ladder,
the VerificationLedger, the RecoveryPolicy, the cancellation token, the
capability bridge - is unchanged and still authoritative. This package is reached
only through ``capability.execute``, exactly like the Blender and Vision layers.

Layout
------
``errors``        Typed failures with stable codes mapped onto the ratified
                  ``CapabilityStatus`` vocabulary. No status is ever inferred
                  from an error message.
``models``        Frozen, serialisable result types. No native handles.
``processes``     Native process operations via pywin32 and documented
                  ``ntdll``/``kernel32`` entry points.
``system``        Native OS, CPU and memory metrics.
``capabilities``  The typed operation surface the model is allowed to see.

Scope
-----
M2.15 provides **process and system** capability only. Window enumeration, focus,
screen capture, mouse, keyboard and UI Automation are deliberately absent and
belong to M2.16; they need a window-ownership model this layer does not yet have.
``Agent.resume()`` and cross-restart autonomy are M2.17 and likewise absent.

Why it replaces a PowerShell backend rather than sitting beside it
-----------------------------------------------------------------
``tools/system.py`` spawned a PowerShell CIM script per call. The native layer
is the same public tool with a different backend, not a second tool. See
:mod:`tools.system` for the migration and its compatibility behaviour.
"""

from __future__ import annotations

from .capabilities import (
    MAX_PROCESS_RESULTS,
    MAX_WINDOW_RESULTS,
    WINDOW_RESULT_LIMIT,
    WINDOWS_OPERATIONS,
    WindowsCapabilityLayer,
    build_windows_capability,
)
from .errors import (
    WindowsAccessDeniedError,
    WindowsCancelledError,
    WindowsError,
    WindowsInvalidRequestError,
    WindowsNotFoundError,
    WindowsOperationFailedError,
    WindowsProcessGoneError,
    WindowsProcessNotFoundError,
    WindowsProtectedProcessError,
    WindowsWindowGoneError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
    register_with_contract_layer,
    windows_error_status_table,
)
from .models import (
    CpuInfo,
    MemoryInfo,
    ProcessInfo,
    SystemInfo,
    TerminationResult,
    process_identity,
)
from .processes import ProcessLayer
from .system import SystemLayer, read_memory, read_system_info, system_available
from .window_models import WindowIdentity, WindowInfo
from .windows import WINDOW_LIFECYCLE_OPERATIONS, WindowLayer, windows_available

#: Register the Windows error vocabulary with the cross-layer contract at import.
#: Done from this side (core) on purpose: the contract module must not import
#: S1 core, so the dependency runs core -> integration.
register_with_contract_layer()

__all__ = [
    "WINDOWS_OPERATIONS",
    "WINDOW_LIFECYCLE_OPERATIONS",
    "MAX_PROCESS_RESULTS",
    "MAX_WINDOW_RESULTS",
    "WINDOW_RESULT_LIMIT",
    "WindowsCapabilityLayer",
    "build_windows_capability",
    "ProcessLayer",
    "SystemLayer",
    "WindowLayer",
    "windows_available",
    "ProcessInfo",
    "TerminationResult",
    "SystemInfo",
    "MemoryInfo",
    "CpuInfo",
    "WindowInfo",
    "WindowIdentity",
    "process_identity",
    "read_memory",
    "read_system_info",
    "system_available",
    "WindowsError",
    "WindowsNotFoundError",
    "WindowsProcessNotFoundError",
    "WindowsProcessGoneError",
    "WindowsAccessDeniedError",
    "WindowsProtectedProcessError",
    "WindowsCancelledError",
    "WindowsInvalidRequestError",
    "WindowsOperationFailedError",
    "WindowsWindowNotFoundError",
    "WindowsWindowGoneError",
    "WindowsWindowIdentityMismatchError",
    "windows_error_status_table",
]