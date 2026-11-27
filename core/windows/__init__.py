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
M2.15 provided **process and system** capability only. M2.16 added the rest of
desktop control in four packages, each resting on the one before:

* **M2.16.1** window enumeration, identity and focus;
* **M2.16.2** target-aware coordinates and pointer input;
* **M2.16.3** keyboard input;
* **M2.16.4** display topology and **bounded, explicit** screen capture.

UI Automation, OCR, vision and the autonomous desktop workflows that would build
on them are deliberately still absent - they belong to M2.16.5 and later.
``Agent.resume()`` and cross-restart autonomy are M2.17 and likewise absent.

Screen capture in particular is not surveillance: it requires an explicit target,
runs only when asked, writes nothing to disk, and hands back metadata rather than
pixels to anything above the native boundary. See :mod:`core.windows.capture`.

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
from .coordinates import (
    ResolvedPoint,
    ScreenPoint,
    VirtualScreen,
    WindowPoint,
    require_anchored,
)
from .capture import CaptureLayer, CaptureTarget, ScreenCapture
from .displays import (
    DisplayIdentity,
    DisplayInfo,
    DisplayLayer,
    DisplayNotFoundError,
    DisplayTarget,
    displays_available,
    read_virtual_desktop,
)
from .input import CURSOR_TOLERANCE, InputDispatchResult, InputLayer, Keys
from .uia import (
    DISCOVERY_LIMITS,
    UIALayer,
    UIElementDescriptor,
    UIElementIdentity,
    uia_available,
)
from .visual import SOURCES as VISUAL_SOURCES
from .visual import VisualTarget
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
    "InputLayer",
    "Keys",
    "InputDispatchResult",
    "CURSOR_TOLERANCE",
    "ScreenPoint",
    "WindowPoint",
    "ResolvedPoint",
    "VirtualScreen",
    "require_anchored",
    "Rect",
    "Size",
    "Point",
    "DisplayIdentity",
    "DisplayInfo",
    "DisplayLayer",
    "DisplayTarget",
    "DisplayNotFoundError",
    "CaptureLayer",
    "CaptureTarget",
    "ScreenCapture",
    "displays_available",
    "read_virtual_desktop",
    "UIALayer",
    "UIElementIdentity",
    "UIElementDescriptor",
    "uia_available",
    "DISCOVERY_LIMITS",
    "VisualTarget",
    "VISUAL_SOURCES",
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