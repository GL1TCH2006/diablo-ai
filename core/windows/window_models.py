"""Window identity, and why an HWND is not one.

M2.16. Windows recycles window handles exactly as it recycles process ids. A bare
``hwnd`` therefore cannot, on its own, identify a window across time: the handle
that identified the user's editor this morning may belong to an unrelated
application this afternoon. Acting on it is how a "close that window" request
becomes closing something else.

There is no persistent window id in Win32. ``GetWindowThreadProcessId`` gives
the owning process, ``GetClassName`` and the title are mutable and frequently
empty, and nothing stamps a creation time onto a window. What *is* stable is the
owning **process**: :mod:`core.windows.processes` already anchors process
identity to ``(pid, created_at)``, and ``created_at`` comes from the kernel and
is not reused.

So a window's identity is anchored to its owning process rather than to its
handle:

    WindowIdentity = (hwnd, process identity, class name, instance)

The handle is carried because it is the only way to *address* a window, but it is
never trusted alone. :meth:`WindowIdentity.matches` requires all four parts, and
every mutating operation re-verifies the target immediately before acting on it.
A recycled handle owned by a different process fails the check and is reported
as a distinct outcome rather than acted upon.

:class:`WindowIdentity` is frozen and JSON-safe. No ``HWND`` handle value ever
crosses the capability boundary as a raw native resource - the ``hwnd`` field is
an opaque integer used only by the layer that produced it, exactly as a pid is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import WindowsInvalidRequestError

__all__ = ["WindowIdentity", "WindowInfo"]


@dataclass(frozen=True)
class WindowIdentity:
    """A logical, verifiable identity for one top-level window.

    ``hwnd``
        The native handle as an opaque integer. Needed to address the window and
        meaningless as an identifier on its own.
    ``process_identity``
        ``(pid, created_at)`` from :mod:`core.windows.processes`. This is the
        durable half: it is kernel-sourced and never reused.
    ``class_name``
        ``GetClassName``. Narrows the check against a handle that has been
        recycled to a window of a different kind.
    ``instance``
        ``GWL_HINSTANCE`` - the owning module's base address. A second cheap
        discriminator against handle reuse within the same process.

    A window whose ``created_at`` could not be read is marked with an empty
    string, which makes :meth:`matches` refuse it against any identified window.
    That is deliberate: an unanchored window is not safely targetable, and
    silently treating it as a match is how the wrong window gets closed.
    """

    hwnd: int
    process_identity: tuple[int, str]
    class_name: str = ""
    instance: int = 0

    @property
    def pid(self) -> int:
        return int(self.process_identity[0])

    @property
    def created_at(self) -> str:
        return str(self.process_identity[1])

    @property
    def anchored(self) -> bool:
        """Whether this identity is tied to a kernel-confirmed process.

        Only an anchored identity can be matched. An unanchored one is returned
        for display and diagnosis, never for acting on.
        """
        return bool(self.created_at) and self.hwnd > 0

    @property
    def key(self) -> str:
        """A stable printable form, for logs, evidence and model context.

        Includes the process creation time so two windows in the same process,
        or the same handle number reused later, never share a key.
        """
        pid, created = self.process_identity
        return f"{pid}:{created}:{self.class_name}:{self.instance}:{self.hwnd}"

    def matches(self, other: "WindowIdentity | None") -> bool:
        """Whether ``other`` is provably the same window.

        Requires the process anchor, the handle, the class and the instance to
        all agree. Anything less is a guess, and a guess is how the wrong window
        is closed.
        """
        if other is None:
            return False
        if not self.anchored or not other.anchored:
            return False
        return (
            self.hwnd == other.hwnd
            and self.class_name == other.class_name
            and self.instance == other.instance
            and int(self.process_identity[0]) == int(other.process_identity[0])
            and self.created_at == other.created_at
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": int(self.hwnd),
            "pid": self.pid,
            "identity_created_at": self.created_at,
            "class_name": self.class_name,
            "instance": int(self.instance),
            "window_key": self.key,
            "anchored": self.anchored,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WindowIdentity":
        """Rebuild an identity from the dict form the capability returns.

        Validation is strict rather than forgiving: a malformed identity must be
        refused, because accepting a partially-populated one would create a
        window reference that cannot be verified before it is acted on.
        """
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError("window identity must be an object.")
        try:
            hwnd = int(data.get("hwnd"))
            pid = int(data.get("pid"))
            instance = int(data.get("instance") or 0)
        except (TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(
                "window identity requires integer hwnd, pid and instance."
            ) from exc
        if hwnd <= 0 or pid < 0:
            raise WindowsInvalidRequestError(
                "window identity requires a positive hwnd and a non-negative pid.")
        return cls(
            hwnd=hwnd,
            process_identity=(pid, str(data.get("identity_created_at") or "")),
            class_name=str(data.get("class_name") or ""),
            instance=instance,
        )


@dataclass(frozen=True)
class WindowInfo:
    """Metadata for one top-level window.

    Every field is a plain value. The native handle appears only as the opaque
    integer inside :attr:`identity`, and no ``PyHANDLE`` or window object leaves
    this module.
    """

    identity: WindowIdentity
    title: str = ""
    visible: bool = False
    minimized: bool = False
    maximized: bool = False
    enabled: bool = True
    foreground: bool = False
    owner_pid: int = 0
    thread_id: int = 0
    process_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    captionable: bool = True

    @property
    def hwnd(self) -> int:
        return self.identity.hwnd

    @property
    def pid(self) -> int:
        return self.identity.pid

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.to_dict(),
            "title": self.title,
            "visible": self.visible,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "enabled": self.enabled,
            "foreground": self.foreground,
            "owner_pid": int(self.owner_pid),
            "thread_id": int(self.thread_id),
            "process_name": self.process_name,
            "rect": list(self.rect),
        }