"""M2.15 - native Windows process capability.

The backend behind the ``process.*`` operations. Everything here goes through
pywin32 or a documented ``ctypes`` entry point into ``ntdll``/``kernel32``.
**Nothing shells out to PowerShell.** The previous ``system.info`` implementation
spawned a CIM script per call, paying a PowerShell startup to obtain information
the OS will hand over directly and in a fraction of the time.

How the process table is read
-----------------------------
One ``NtQuerySystemInformation(SystemProcessInformation)`` call returns the entire
table - pid, parent pid, image name, thread count, handle count and working set -
in a single round trip. That is the same call ``Get-Process`` is built on. It is
used rather than opening a handle per process because a handle-per-process sweep
is both slower and, for protected processes, noisier: it produces an access
denied per process instead of one clean snapshot.

Per-process detail that the table does not carry - executable path, command line,
owner, exact creation time - is fetched only for a specific process, on a handle
opened for that purpose and closed in a ``finally``.

Security properties, all enforced here rather than trusted to callers
---------------------------------------------------------------------
**No arbitrary handles escape.** A handle is opened, used, closed. No ``PyHANDLE``
is returned, stored on a result, or exposed to the model;
:class:`~core.windows.models.ProcessInfo` carries integers and strings only.

**Self-termination is refused.** :meth:`ProcessLayer.terminate_process` raises
before opening any handle. A hard rule, not a setting.

**PID 0/1/2/4 are refused** whatever the OS would permit. PID 0 is System Idle
and PID 4 is System; neither is ever something a caller means.

**Protected processes degrade, they do not lie.** A process that cannot be opened
is returned with ``protected=True`` and an ``unavailable_fields`` tuple naming
exactly what was unreadable. It is never given plausible-looking blanks.

**Identity is a pair.** A PID alone is not an identity: Windows recycles them.
Termination re-reads the target's creation time after the wait and reports a
recycled PID as its own outcome instead of crediting a kill with the wrong process.

Cancellation
------------
Checked before any work, and again between the terminate call and each
confirmation poll. A pre-cancelled request opens no handle; a cancellation during
the wait stops waiting but does **not** withdraw a termination already requested,
because that call was already in flight when the user asked to stop.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any, Callable

from core.logging_setup import get_logger

from .errors import (
    WindowsAccessDeniedError,
    WindowsCancelledError,
    WindowsInvalidRequestError,
    WindowsNotFoundError,
    WindowsOperationFailedError,
    WindowsProcessGoneError,
    WindowsProcessNotFoundError,
    WindowsProtectedProcessError,
)
from .models import ProcessInfo, TerminationResult

log = get_logger("windows.processes")

__all__ = ["ProcessLayer", "WINDOWS_AVAILABLE", "pywin32_error"]

#: PIDs refused on principle, whatever the OS would allow.
_PROTECTED_PIDS = frozenset({0, 1, 2, 4})

_QUERY_LIMITED = 0x1000        # PROCESS_QUERY_LIMITED_INFORMATION
_QUERY_INFORMATION = 0x0400     # PROCESS_QUERY_INFORMATION
_VM_READ = 0x0010              # PROCESS_VM_READ
_TERMINATE = 0x0001            # PROCESS_TERMINATE
_SYNCHRONIZE = 0x00100000      # SYNCHRONIZE

#: ntdll information classes used here.
_SYSTEM_PROCESS_INFORMATION = 5
_PROCESS_BASIC_INFORMATION = 0
_PROCESS_COMMAND_LINE_INFORMATION = 60

_STILL_ACTIVE = 259
_ACCESS_DENIED_ERRNOS = frozenset({5, 13})
_INVALID_PARAMETER = 87


def pywin32_error(exc: BaseException, *, context: str = "") -> BaseException:
    """Translate a native error into this layer's typed vocabulary.

    Decided by the numeric error code, never by matching message text, so a
    permission refusal stays a permission outcome whatever the message says.
    """
    detail = {"context": context} if context else {}
    args = getattr(exc, "args", ()) or ()
    code = args[0] if args and isinstance(args[0], int) else None
    if code in _ACCESS_DENIED_ERRNOS:
        return WindowsAccessDeniedError(str(exc), diagnostics=detail)
    if code == _INVALID_PARAMETER:
        return WindowsInvalidRequestError(str(exc), diagnostics=detail)
    if code in (2, 3):
        return WindowsProcessNotFoundError(str(exc), diagnostics=detail)
    return WindowsOperationFailedError(str(exc), diagnostics=detail)


def _load_pywin32() -> tuple[Any, Any, Any, Any]:
    """Import the native modules, or explain precisely why they are missing."""
    try:
        import win32api
        import win32con
        import win32process
        import win32security
    except ImportError as exc:  # pragma: no cover - host dependent
        raise WindowsNotFoundError(
            "pywin32 is required for the native Windows layer. "
            "Install it with: pip install pywin32",
            diagnostics={"error": str(exc)},
        ) from exc
    return win32api, win32con, win32process, win32security


try:
    _load_pywin32()
    WINDOWS_AVAILABLE = True
except Exception:  # noqa: BLE001 - pragma: no cover - host dependent
    WINDOWS_AVAILABLE = False


class _UnicodeString(ctypes.Structure):
    """``UNICODE_STRING``: a length pair plus a *pointer*, not inline text."""

    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _SystemProcess(ctypes.Structure):
    """One entry of ``SYSTEM_PROCESS_INFORMATION`` (winternl.h).

    ``ImageName`` is a ``UNICODE_STRING`` whose ``Buffer`` points at memory
    *inside* this same buffer. Declaring it as an inline char array - which is
    the obvious-looking mistake - shortens every entry by 15 bytes on x64 and
    silently shifts every field after it, producing plausible-looking nonsense.
    ``ctypes`` handles the 8-byte alignment the pointer fields need.
    """

    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_size_t),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_ulonglong),
        ("UserTime", ctypes.c_ulonglong),
        ("KernelTime", ctypes.c_ulonglong),
        ("ImageName", _UnicodeString),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def _filetime_to_datetime(value: int) -> str:
    """Windows FILETIME (100ns since 1601) to a readable local timestamp."""
    if not value:
        return ""
    try:
        from datetime import datetime, timedelta, timezone
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        stamp = epoch + timedelta(microseconds=int(value) // 10)
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - never let a timestamp break a listing
        return ""


def _mb(value: Any) -> float:
    return round(int(value or 0) / (1024 * 1024), 2)


def _stem(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    base = os.path.basename(text)
    return base[:-4] if base.lower().endswith(".exe") else base


def _normalise_name(name: str) -> str:
    text = str(name or "").strip().lower()
    return text[:-4] if text.endswith(".exe") else text


class ProcessLayer:
    """Native process operations. No handles escape this object."""

    def __init__(self, *, poll_interval: float = 0.05, default_timeout: float = 5.0) -> None:
        self._poll_interval = max(0.01, float(poll_interval))
        self._default_timeout = max(0.0, float(default_timeout))
        self._closed = False
        self._current_pid = os.getpid()
        self._ntdll = ctypes.WinDLL("ntdll")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare_prototypes()
        self._exit_codes: dict[int, int] = {}

    def _declare_prototypes(self) -> None:
        """Pin the signatures of the two kernel32 entry points we call directly.

        Without these, ctypes marshals arguments as ``c_int`` and a 64-bit handle
        value is silently truncated. That does not raise - it returns garbage, or
        fails, which is considerably worse than an error.
        """
        kernel32 = self._kernel32
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._ntdll.NtQuerySystemInformation.argtypes = [
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
        ]
        self._ntdll.NtQuerySystemInformation.restype = wintypes.LONG
        self._ntdll.NtQueryInformationProcess.argtypes = [
            ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        self._ntdll.NtQueryInformationProcess.restype = wintypes.LONG

    @staticmethod
    def _as_handle(handle: Any) -> int:
        """A pywin32 ``PyHANDLE`` as a plain int.

        ``PyHANDLE`` cannot be marshalled by ctypes at all - neither as
        ``wintypes.HANDLE`` nor as ``c_void_p`` - because it is not an integer to
        the marshaller. ``int()`` on it is the documented conversion, and the
        pinned ``argtypes`` above then keep the 64-bit value intact.
        """
        return int(handle)

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------ util

    def _check_cancelled(self, cancelled: Callable[[], bool] | None) -> None:
        if callable(cancelled) and cancelled():
            raise WindowsCancelledError(
                "Cancelled before the process operation completed.",
                diagnostics={"cancellation_terminated": False},
            )

    @staticmethod
    def _normalise_pid(pid: Any) -> int:
        # bool is an int subclass, and a non-integral float truncates. Both would
        # act on a process the caller did not name.
        if isinstance(pid, bool):
            raise WindowsInvalidRequestError(f"pid must be an integer, got {pid!r}")
        if isinstance(pid, float) and not pid.is_integer():
            raise WindowsInvalidRequestError(
                f"pid must be a whole number, got {pid!r}; truncating it would "
                f"target process {int(pid)}")
        try:
            value = int(pid)
        except (TypeError, ValueError):
            raise WindowsInvalidRequestError(f"pid must be an integer, got {pid!r}") from None
        if value < 0:
            raise WindowsInvalidRequestError(f"pid must not be negative, got {value}")
        return value

    @staticmethod
    def _check_not_protected(pid: int) -> None:
        if pid in _PROTECTED_PIDS:
            raise WindowsProtectedProcessError(
                f"Refused to act on protected process id {pid}.",
                diagnostics={"pid": pid})

    @staticmethod
    def _check_not_self(pid: int, current_pid: int) -> None:
        if pid == current_pid:
            raise WindowsInvalidRequestError(
                "Refused: Diablo will not terminate its own process.",
                diagnostics={"pid": pid, "current_pid": current_pid})

    # ------------------------------------------------------- process table

    def _process_table(self) -> list[ProcessInfo]:
        """The whole process table from one native call.

        Fields this cannot supply are filled from the table only: executable
        path, command line and owner need a handle, and are left empty with
        ``unavailable_fields`` naming them. The caller sees exactly what it got.
        """
        _load_pywin32()
        # First call sizes the buffer; the result is STATUS_INFO_LENGTH_MISMATCH.
        needed = wintypes.ULONG(0)
        status = self._ntdll.NtQuerySystemInformation(
            _SYSTEM_PROCESS_INFORMATION, None, 0, ctypes.byref(needed))
        if status != 0 and needed.value == 0:
            raise WindowsOperationFailedError(
                f"NtQuerySystemInformation could not size the process buffer (NTSTATUS 0x{status & 0xFFFFFFFF:08x}).",
                diagnostics={"ntstatus": status & 0xFFFFFFFF},
            )
        # Generous headroom: processes appear between sizing and the read.
        buf = ctypes.create_string_buffer(int(needed.value) + 65536)
        status = self._ntdll.NtQuerySystemInformation(
            _SYSTEM_PROCESS_INFORMATION, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        if status != 0:
            raise WindowsOperationFailedError(
                f"NtQuerySystemInformation failed (NTSTATUS 0x{status & 0xFFFFFFFF:08x}).",
                diagnostics={"ntstatus": status & 0xFFFFFFFF},
            )

        out: list[ProcessInfo] = []
        offset = 0
        base = ctypes.addressof(buf)
        total = int(needed.value)
        while offset < total:
            try:
                entry = _SystemProcess.from_address(base + offset)
            except ValueError:
                break
            pid = int(entry.UniqueProcessId or 0)
            if pid:
                name = _unicode_name(entry.ImageName)
                parent = int(entry.InheritedFromUniqueProcessId or 0) or None
                out.append(ProcessInfo(
                    pid=pid,
                    name=name,
                    parent_pid=parent,
                    created_at=_filetime_to_datetime(int(entry.CreateTime)),
                    working_set_mb=_mb(entry.WorkingSetSize),
                    peak_working_set_mb=_mb(entry.PeakWorkingSetSize),
                    private_mb=_mb(entry.PrivatePageCount),
                    thread_count=int(entry.NumberOfThreads or 0),
                    handle_count=int(entry.HandleCount or 0),
                    session_id=int(entry.SessionId or 0),
                    unavailable_fields=("executable_path", "command_line", "owner"),
                ))
            nxt = int(entry.NextEntryOffset or 0)
            if nxt <= 0:
                break
            offset += nxt
        return out

    # ------------------------------------------------------------ enumeration

    def enumerate_processes(self, *, cancelled: Callable[[], bool] | None = None,
                            ) -> list[ProcessInfo]:
        """Every visible process.

        Cancellation raises rather than returning a partial list, so a caller can
        never mistake "stopped early" for "this is all of them".
        """
        self._check_cancelled(cancelled)
        table = self._process_table()
        self._check_cancelled(cancelled)
        return table

    def get_process(self, pid: int, *, cancelled: Callable[[], bool] | None = None,
                    with_details: bool = True) -> ProcessInfo:
        """One process by PID.

        Raises rather than returning a falsy value, so "not found" cannot be
        confused with "found, but I could not read it".
        """
        pid = self._normalise_pid(pid)
        self._check_cancelled(cancelled)
        self._check_not_protected(pid)
        for info in self._process_table():
            if info.pid == pid:
                if not with_details:
                    return info
                return self._with_details(info)
        raise WindowsProcessNotFoundError(
            f"No process with id {pid}.", diagnostics={"pid": pid})

    def find_processes(self, name: str = "", parent_pid: int | None = None,
                       *, limit: int = 200, cancelled: Callable[[], bool] | None = None,
                       with_details: bool = False) -> list[ProcessInfo]:
        """Filtered process list.

        ``name`` matches case-insensitively against the image name with or
        without ``.exe``, so a caller need not know which form Windows reports.
        Details are off by default: enriching every match would open a handle per
        match, which is slow and noisy, and most callers only need the table.
        """
        if limit <= 0:
            return []
        wanted = _normalise_name(name)
        parent = self._normalise_pid(parent_pid) if parent_pid is not None else None
        out: list[ProcessInfo] = []
        for info in self.enumerate_processes(cancelled=cancelled):
            if parent is not None and info.parent_pid != parent:
                continue
            if wanted and not _matches_name(info, wanted):
                continue
            out.append(self._with_details(info) if with_details else info)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------- per-detail

    def _with_details(self, info: ProcessInfo) -> ProcessInfo:
        """Enrich a table entry with path, command line and owner.

        Best-effort by field. A refusal marks the process protected and names
        what was unreadable; it never substitutes plausible blanks.
        """
        unavailable = list(info.unavailable_fields)
        protected = False
        try:
            handle = self._open(info.pid, _QUERY_LIMITED)
        except WindowsAccessDeniedError:
            protected = True
            unavailable = ["executable_path", "command_line", "owner"]
            return ProcessInfo(
                pid=info.pid, name=info.name, parent_pid=info.parent_pid,
                created_at=info.created_at, working_set_mb=info.working_set_mb,
                peak_working_set_mb=info.peak_working_set_mb,
                private_mb=info.private_mb, thread_count=info.thread_count,
                handle_count=info.handle_count, session_id=info.session_id,
                protected=True, unavailable_fields=tuple(unavailable))
        except WindowsProcessNotFoundError:
            raise
        try:
            executable = self._read_executable_limited(handle)
            command_line = self._read_command_line(handle)
            owner = self._read_owner(handle)
        finally:
            self._close_handle(handle)
        for name, value in (("executable_path", executable),
                           ("command_line", command_line),
                           ("owner", owner)):
            if value and name in unavailable:
                unavailable.remove(name)
        return ProcessInfo(
            pid=info.pid, name=info.name or _stem(executable),
            parent_pid=info.parent_pid, executable_path=executable,
            command_line=command_line, owner=owner, created_at=info.created_at,
            working_set_mb=info.working_set_mb,
            peak_working_set_mb=info.peak_working_set_mb,
            private_mb=info.private_mb, thread_count=info.thread_count,
            handle_count=info.handle_count, session_id=info.session_id,
            protected=protected, unavailable_fields=tuple(unavailable))

    def _open(self, pid: int, access: int) -> int:
        win32api, _con, _wp, _sec = _load_pywin32()
        try:
            return win32api.OpenProcess(access, False, pid)
        except Exception as exc:  # noqa: BLE001
            args = getattr(exc, "args", ()) or ()
            code = args[0] if args and isinstance(args[0], int) else None
            if code in _ACCESS_DENIED_ERRNOS:
                raise WindowsAccessDeniedError(
                    f"Windows refused access to process {pid}.",
                    diagnostics={"pid": pid, "winerror": code}) from exc
            raise pywin32_error(exc, context=f"OpenProcess({pid})") from exc

    def _read_executable_limited(self, handle: int) -> str:
        """Image path via ``QueryFullProcessImageNameW``, which needs only LIMITED."""
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        try:
            ok = self._kernel32.QueryFullProcessImageNameW(
                self._as_handle(handle), 0, buf, ctypes.byref(size))
        except Exception as exc:  # noqa: BLE001
            log.debug("QueryFullProcessImageNameW failed: %s", exc)
            return ""
        return buf.value if ok else ""

    def _read_command_line(self, handle: int) -> str:
        """Read a command line without WMI.

        ``NtQueryInformationProcess`` class 60 with a LIMITED handle is the
        documented route and costs no COM object per call. The buffer begins with
        a ``UNICODE_STRING`` whose ``Buffer`` is a pointer into that same buffer,
        so the struct is copied out of the raw address rather than off the
        Python string object.
        """
        buf = ctypes.create_unicode_buffer(32768)
        returned = wintypes.ULONG(0)
        try:
            status = self._ntdll.NtQueryInformationProcess(
                self._as_handle(handle), _PROCESS_COMMAND_LINE_INFORMATION,
                buf, ctypes.sizeof(buf), ctypes.byref(returned))
        except Exception as exc:  # noqa: BLE001
            log.debug("NtQueryInformationProcess(cmdline) failed: %s", exc)
            return ""
        if status != 0:
            return ""
        header = ctypes.string_at(ctypes.addressof(buf), ctypes.sizeof(_UnicodeString))
        us = _UnicodeString.from_buffer_copy(header)
        length = int(us.Length or 0) // 2
        if length <= 0 or length > 32000 or not us.Buffer:
            return ""
        try:
            return ctypes.wstring_at(us.Buffer, length)
        except Exception:  # noqa: BLE001
            return ""

    def _read_owner(self, handle: int) -> str:
        _api, con, _wp, win32security = _load_pywin32()
        try:
            token = win32security.OpenProcessToken(handle, con.TOKEN_QUERY)
        except Exception:  # noqa: BLE001
            return ""
        try:
            sid, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
            name, domain, _type = win32security.LookupAccountSid(None, sid)
            return f"{domain}\\{name}" if domain else str(name)
        except Exception:  # noqa: BLE001
            return ""
        finally:
            try:
                token.Close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ termination

    def terminate_process(self, pid: int, *, timeout: float | None = None,
                          cancelled: Callable[[], bool] | None = None) -> TerminationResult:
        """Request a termination, then confirm it.

        The contract, in order:

        1. Refuse self-termination and protected PIDs. No handle opened.
        2. Read the target's identity **before** acting on it.
        3. ``TerminateProcess``. A failure here raises; it is never a silent no-op.
        4. Poll for exit up to ``timeout``, re-checking identity each poll, so a
           recycled PID cannot be mistaken for the exit we caused.
        5. Return ``requested`` and ``confirmed`` as separate facts.

        A process still running after the timeout yields ``confirmed=False`` with
        the reason. It never yields success because an API call was made.
        """
        pid = self._normalise_pid(pid)
        self._check_not_protected(pid)
        self._check_not_self(pid, self._current_pid)
        budget = self._default_timeout if timeout is None else max(0.0, float(timeout))

        # Pre-cancelled: do no work at all.
        self._check_cancelled(cancelled)

        before = self.get_process(pid, cancelled=cancelled)
        identity = before.identity

        handle = None
        requested = False
        exit_code: int | None = None
        confirmed = False
        reason = ""
        try:
            handle = self._open(pid, _TERMINATE | _QUERY_LIMITED | _SYNCHRONIZE)
            win32api = _load_pywin32()[0]
            win32api.TerminateProcess(handle, 1)
            requested = True

            started = time.monotonic()
            # The handle is deliberately held across the wait. Termination is
            # asynchronous, so the exit code is STILL_ACTIVE on the first read
            # and the pid no longer opens once the process is gone - which is why
            # reading it after the loop could only ever return nothing.
            while True:
                if callable(cancelled) and cancelled():
                    raise WindowsCancelledError(
                        "Cancelled while waiting for termination to be confirmed.",
                        diagnostics={"pid": pid, "cancellation_terminated": False,
                                     "identity_created_at": identity[1]})
                if (time.monotonic() - started) >= budget:
                    reason = ("termination requested but the exit was not observed "
                              "within the timeout")
                    break
                # Wait 0 briefly: the authoritative "is it gone" signal, and it
                # works even for a process whose pid has been recycled.
                if self._kernel32.WaitForSingleObject(self._as_handle(handle), 0) != 258:
                    code = _load_pywin32()[2].GetExitCodeProcess(handle)
                    exit_code = None if code == _STILL_ACTIVE else int(code)
                    confirmed = True
                    reason = "process exited after termination was requested"
                    break
                time.sleep(self._poll_interval)

            if not reason:
                # The handle says it exited; confirm against the process table so
                # the claim is about the identity we asked about.
                state = self._observe_exit(pid, identity)
                if state is None:
                    confirmed = True
                    exit_code = None
                    reason = ("process exited; the pid was recycled to a different "
                              "process, so no exit code was read")
        except WindowsAccessDeniedError as exc:
            raise WindowsProtectedProcessError(
                f"Windows refused to terminate process {pid}; it is likely a "
                f"protected, system, or higher-privilege process.",
                diagnostics={"pid": pid, **exc.diagnostics}) from exc
        except WindowsProcessNotFoundError as exc:
            # Vanished between inspection and the terminate call. Its own outcome,
            # not a success.
            raise WindowsProcessGoneError(
                f"Process {pid} exited before termination could be requested.",
                diagnostics={"pid": pid}) from exc
        finally:
            self._close_handle(handle)

        return TerminationResult(
            pid=pid,
            identity=identity,
            requested=requested,
            confirmed=confirmed,
            exit_code=exit_code,
            waited_seconds=time.monotonic() - started if requested else 0.0,
            reason=reason,
            detail={"identity_verified": True, "timeout_seconds": budget,
                    "target_name": before.name},
        )

    def _observe_exit(self, pid: int, identity: tuple[int, str]) -> bool | None:
        """``True`` gone, ``False`` still running, ``None`` pid recycled.

        ``None`` is separated from ``True`` because a recycled id means the id is
        no longer evidence about our target.
        """
        try:
            current = self.get_process(pid, with_details=False)
        except (WindowsProcessNotFoundError, WindowsProcessGoneError):
            return True
        except WindowsAccessDeniedError:
            return False
        if not identity[1] or not current.created_at:
            # No creation time to compare: treat as still running rather than
            # claiming an exit that cannot be attributed to this identity.
            return False
        return False if current.created_at == identity[1] else None

    def _read_exit_code(self, pid: int, identity: tuple[int, str]) -> int | None:
        handle = None
        try:
            handle = self._open(pid, _QUERY_LIMITED | _SYNCHRONIZE)
            code = _load_pywin32()[2].GetExitCodeProcess(handle)
            return None if code == _STILL_ACTIVE else int(code)
        except Exception:  # noqa: BLE001 - the exit code is a nicety, not the claim
            return None
        finally:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: int | None) -> None:
        if handle is None:
            return
        try:
            _load_pywin32()[0].CloseHandle(handle)
        except Exception:  # noqa: BLE001 - a failed close must not mask the result
            pass


def _unicode_name(us: Any) -> str:
    """Decode a ``UNICODE_STRING`` whose buffer points into the same allocation."""
    length = int(getattr(us, "Length", 0) or 0) // 2
    pointer = getattr(us, "Buffer", None)
    if length <= 0 or not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer, length)
    except Exception:  # noqa: BLE001 - a name is never worth failing a listing for
        return ""


def _matches_name(info: ProcessInfo, wanted: str) -> bool:
    """Whether a process's image name matches the requested name.

    Both sides are normalised the same way. The search term has already had its
    extension stripped by :func:`_normalise_name`; the candidate has not, because
    the process table reports ``python.exe`` while a caller asks for ``python``.
    Comparing them raw matches nothing at all.
    """
    if not wanted:
        return True
    candidates = {
        _normalise_name(info.name or ""),
        _normalise_name(_stem(info.executable_path)),
    }
    return any(c == wanted for c in candidates if c)
