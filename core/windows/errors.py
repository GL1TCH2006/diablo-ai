"""M2.15 - error vocabulary for the native Windows layer.

Every failure here carries a **stable code**, and :mod:`integration.contracts`
maps those codes onto :class:`~integration.contracts.CapabilityStatus` by exact
match. Nothing anywhere inspects an error *message* to decide a status, which is
the ratified cross-layer rule: an infrastructure failure must never be reported
as caller-attributable invalid input.

The distinctions that matter, and why
------------------------------------
``WindowsNotFoundError``
    The layer itself cannot run: pywin32 missing, or the process-enumeration API
    failed. This is ``UNAVAILABLE``, not a caller error.

``WindowsProcessNotFoundError``
    The requested PID does not exist, **or existed and has already exited**. This
    is ``INVALID_REQUEST`` - the caller named something that is not there - and
    it is deliberately distinct from a race during termination, below.

``WindowsProcessGoneError``
    The process vanished between being found and being acted on. This is the
    race case and gets its own status vocabulary downstream rather than being
    folded into "not found", because the caller's request *was* valid.

``WindowsAccessDeniedError``
    The OS refused access: a protected system process, a process owned by another
    account, or one running elevated. ``PERMISSION_REQUIRED``, never a generic
    failure. This is what makes "conservative about critical processes" an
    enforced property rather than an intention.

``WindowsCancelledError``
    Cancellation observed. Carries ``terminated`` in diagnostics so a caller can
    distinguish a cancellation that actually took effect from one that could not,
    matching the ratified Blender cancellation contract.

``WindowsInvalidRequestError``
    Caller-attributable: a malformed PID, a missing required parameter, an
    out-of-range value. The only error for which the caller is to blame.

``WindowsOperationFailedError``
    A real infrastructure failure that is none of the above.
"""

from __future__ import annotations

__all__ = [
    "WindowsError",
    "WindowsNotFoundError",
    "WindowsProcessNotFoundError",
    "WindowsProcessGoneError",
    "WindowsAccessDeniedError",
    "WindowsCancelledError",
    "WindowsInvalidRequestError",
    "WindowsOperationFailedError",
    "WindowsProtectedProcessError",
    "WindowsWindowGoneError",
    "WindowsWindowIdentityMismatchError",
    "WindowsWindowNotFoundError",
    "windows_error_status_table",
]


class WindowsError(Exception):
    """Base for every native Windows layer failure.

    ``code`` is the stable identity used for status mapping. It is never derived
    from a message.
    """

    code = "windows_error"
    message = "Windows operation failed."

    def __init__(self, message: str = "", *, diagnostics: dict | None = None) -> None:
        super().__init__(message or self.message)
        self.diagnostics: dict = dict(diagnostics or {})

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "diagnostics": dict(self.diagnostics),
        }

    @property
    def terminated(self) -> bool | None:
        """Whether a cancellation actually took effect, when that is meaningful.

        ``None`` means the error is not about termination. Read from diagnostics
        rather than assumed, so a cancellation that could not stop its target is
        never reported as one that did - the same rule the Blender layer follows.
        """
        value = self.diagnostics.get("cancellation_terminated")
        return bool(value) if "cancellation_terminated" in self.diagnostics else None


class WindowsNotFoundError(WindowsError):
    """The Windows layer itself is unavailable on this machine."""

    code = "windows_layer_unavailable"
    message = "Native Windows layer is unavailable."


class WindowsProcessNotFoundError(WindowsError):
    """No process with the given PID exists."""

    code = "process_not_found"
    message = "No such process."


class WindowsProcessGoneError(WindowsError):
    """The process existed but vanished before the operation completed."""

    code = "process_gone"
    message = "The process exited before the operation completed."


class WindowsAccessDeniedError(WindowsError):
    """The OS refused access to the process."""

    code = "windows_access_denied"
    message = "Access to the process was denied by Windows."


class WindowsProtectedProcessError(WindowsError):
    """The target is a system or critical process and was refused on purpose.

    Distinguished from a plain access denial so the refusal can be reported as a
    deliberate decision rather than an OS accident.
    """

    code = "windows_protected_process"
    message = "Refused: the target is a protected or critical system process."


class WindowsCancelledError(WindowsError):
    """Cancellation was observed."""

    code = "windows_cancelled"
    message = "The Windows operation was cancelled."


class WindowsWindowNotFoundError(WindowsError):
    """No window matches the supplied identity.

    M2.16. Distinct from a destroyed window: the identity simply names nothing
    that exists now. It is a caller error in the sense that the reference is
    stale, and it is reported separately so the model can be told "that window is
    gone" rather than "something went wrong".
    """

    code = "window_not_found"
    message = "No window matches the supplied window identity."


class WindowsWindowGoneError(WindowsError):
    """The window existed but was destroyed before the operation completed.

    M2.16. The race that makes desktop automation dangerous: enumeration returns
    a window, and by the time the action is dispatched the user has closed it.
    Reported as its own outcome rather than silently succeeding.
    """

    code = "window_gone"
    message = "The window was destroyed before the operation completed."


class WindowsWindowIdentityMismatchError(WindowsError):
    """The handle now refers to a different window than the one requested.

    M2.16. This is the handle-reuse outcome, and the single most important refusal
    in the desktop layer: the caller asked to act on one window and the handle has
    since been recycled to an unrelated one. Acting anyway would mean closing or
    focusing something the user never named.
    """

    code = "window_identity_mismatch"
    message = ("The window handle now refers to a different window than the one "
               "requested.")


class WindowsInvalidRequestError(WindowsError):
    """Caller-attributable bad input."""

    code = "windows_invalid_request"
    message = "The Windows request was invalid."


class WindowsOperationFailedError(WindowsError):
    """An infrastructure failure that is none of the above."""

    code = "windows_operation_failed"
    message = "The Windows operation failed."


#: Stable code -> capability status. Declared here rather than in the
#: integration layer so the vocabulary lives with the errors it describes, and
#: registered with the contract layer so it cannot be added without stating what
#: status it means.
#:
#: Registration runs from this module, which is S1 core, into
#: :mod:`integration.contracts`. The dependency therefore runs core -> integration
#: and never the reverse; ``test_contract_module_has_no_s1_core_dependency``
#: forbids the contract module from importing core, and importing core from
#: there to reach these codes would violate it.
windows_error_status_table: dict[str, str] = {
    WindowsNotFoundError.code: "unavailable",
    WindowsCancelledError.code: "cancelled",
    WindowsAccessDeniedError.code: "permission_required",
    WindowsProtectedProcessError.code: "permission_required",
    WindowsInvalidRequestError.code: "invalid_request",
    WindowsProcessNotFoundError.code: "invalid_request",
    # A process that vanished mid-operation is a real outcome of a valid
    # request, not a caller error and not a generic failure. It is reported as a
    # failure with its own stable code so the distinction survives to the caller
    # without inventing a new CapabilityStatus the ratified vocabulary does not
    # have.
    WindowsProcessGoneError.code: "failure",
    # M2.16 window outcomes. A stale identity is a caller error (the reference
    # named nothing that exists), a destroyed window and a recycled handle are
    # real races, and all three are reported as failures with their own stable
    # codes so a "that window is gone" is never flattened into "something went
    # wrong".
    WindowsWindowNotFoundError.code: "invalid_request",
    WindowsWindowGoneError.code: "failure",
    WindowsWindowIdentityMismatchError.code: "invalid_request",
    WindowsOperationFailedError.code: "failure",
    WindowsError.code: "failure",
}


def register_with_contract_layer() -> int:
    """Register these codes with the cross-layer contract, idempotently.

    Safe to call more than once. A missing ``integration`` package is tolerated,
    so this module can still be imported on a host that has neither.
    """
    try:
        from integration.contracts import register_status_codes
    except Exception:  # noqa: BLE001 - the contract layer is optional here
        return 0
    added = register_status_codes(windows_error_status_table)
    try:
        from integration.contracts import _STATUS_BY_EXCEPTION_TYPE
        from .errors import WindowsError as _WE
    except Exception:  # noqa: BLE001
        return added
    if not any(exc_type is _WE for exc_type, _status in _STATUS_BY_EXCEPTION_TYPE):
        from integration.contracts import CapabilityStatus
        _STATUS_BY_EXCEPTION_TYPE.append((_WE, CapabilityStatus.FAILURE))
    return added
