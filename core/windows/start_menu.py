"""Native Start Menu application discovery, replacing ``Get-StartApps``.

``Get-StartApps`` is a PowerShell cmdlet with no Win32 equivalent, but the data
it returns comes from two places that *can* be read directly:

* ``HKLM/HKCU\\Software\\Classes\\Applications\\*.exe\\shell\\open\\command``
  - registered per-application open commands, and
* ``.lnk`` files under the Start Menu directories.

Both are plain registry and filesystem reads: no process is spawned, and none of
the shell COM interfaces ``Get-StartApps`` needs to be initialised are touched.

What this cannot see
--------------------
**Packaged (AppX/MSIX) applications** - Calculator, Photos, the Store - have
neither a shortcut nor a registered open command. They are discoverable only
through the AppX state repository
(``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AppModel\\StateRepository``)
and the ``C:\\Program Files\\WindowsApps`` package store, and on this machine both
are unreadable without elevation:

* the state repository returns ``Access is denied`` for a non-elevated process,
* ``WindowsApps`` refuses directory enumeration outright.

A non-elevated process cannot enumerate packaged applications, and the shell
cannot be used to enumerate them either. This module therefore does not pretend
to: :func:`start_menu_lookup` returns ``None`` for a packaged app rather than
guessing, and :func:`tools.applications._start_apps_lookup` keeps the PowerShell
probe as a documented fallback for exactly this case. That is a real, stated
limitation of the native path, not a silent gap.

This is discovery only. Nothing here launches anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("start_menu")

try:  # pragma: no cover - the reader is probed, not assumed
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

__all__ = [
    "start_menu_lookup",
    "start_menu_available",
    "start_menu_entries",
    "StartMenuEntry",
]


class StartMenuEntry:
    """One discovered Start Menu application.

    ``returns_exe`` distinguishes the two kinds of identifier the Start Menu
    holds: a filesystem path to an executable, or a link that has to be resolved
    by something else. Only the first is directly launchable.
    """

    __slots__ = ("name", "app_id", "returns_exe")

    def __init__(self, name: str, app_id: str, returns_exe: bool) -> None:
        self.name = name
        self.app_id = app_id
        self.returns_exe = returns_exe

    def __repr__(self) -> str:
        return f"StartMenuEntry(name={self.name!r}, app_id={self.app_id!r})"


#: Where the Start Menu lives. The per-user directory is checked first: a
#: per-user install shadows a machine-wide one, exactly as the shell resolves it.
_START_MENU_DIRS: tuple[str, ...] = (
    r"%AppData%\Microsoft\Windows\Start Menu\Programs",
    r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
)

#: Registered "Open with" defaults, per hive.
_APPLICATIONS_KEY = r"SOFTWARE\Classes\Applications"


def start_menu_available() -> bool:
    """Whether native Start Menu discovery can run on this machine."""
    return winreg is not None


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(path))


def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    for raw in _START_MENU_DIRS:
        candidate = _expand(raw)
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _command_executable(command: str) -> str:
    """Extract the executable from a registered ``open\\command`` line.

    The stored form is a full command line, often with arguments and quotes,
    e.g. ``"C:\\Program Files\\App\\app.exe" --some-flag``. Returning a path that
    still contained the arguments would produce a launch that fails with a
    confusing error, so the leading executable is isolated here.
    """
    text = (command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        closing = text.find('"', 1)
        if closing > 0:
            return text[1:closing]
    # Unquoted: the executable ends at the first space. An unquoted command with
    # a spaced path is malformed in the registry anyway.
    return text.split(" ", 1)[0]


def _expand_environment(text: str) -> str:
    """Expand ``%VAR%`` inside a registry-stored path.

    The registry stores paths like ``%SystemRoot%\\system32\\NOTEPAD.EXE``. Used
    verbatim, that is not a path and every launch would fail - which is precisely
    what happened before this was handled.
    """
    return os.path.expandvars(text.strip())


def _iter_registry_applications() -> list[StartMenuEntry]:
    """Registered per-application open commands, from both hives."""
    if winreg is None:
        return []
    entries: list[StartMenuEntry] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _APPLICATIONS_KEY) as applications:
                count = winreg.QueryInfoKey(applications)[0]
                for index in range(count):
                    try:
                        exe_name = winreg.EnumKey(applications, index)
                    except OSError:
                        continue
                    command_key = (
                        rf"{_APPLICATIONS_KEY}\{exe_name}\shell\open\command")
                    try:
                        with winreg.OpenKey(hive, command_key) as command:
                            raw, _ = winreg.QueryValueEx(command, "")
                    except OSError:
                        continue
                    executable = _command_executable(str(raw))
                    if executable:
                        entries.append(
                            StartMenuEntry(exe_name, _expand_environment(executable),
                                           True))
        except OSError:
            # A missing hive is normal: HKCU\Software\Classes is absent on a
            # machine with no per-user registrations.
            continue
    return entries


def _iter_shortcut_files() -> list[StartMenuEntry]:
    """``.lnk`` files under the Start Menu directories.

    The link target is not read. Resolving it needs the shell COM interfaces,
    which is the dependency M2.15 exists to remove; the filename is the name the
    user sees, and the caller resolves the extension through
    ``settings.app_known_paths``.
    """
    entries: list[StartMenuEntry] = []
    for directory in _start_menu_dirs():
        try:
            for path in directory.rglob("*.lnk"):
                entries.append(StartMenuEntry(path.stem, str(path), False))
        except OSError as exc:
            log.debug("start menu scan failed for %s: %s", directory, exc)
            continue
    return entries


def _matches(entry: StartMenuEntry, tokens: list[str]) -> bool:
    """Whether every token appears in the entry's name or identifier.

    All tokens must match, so ``"notepad calc"`` finds nothing rather than the
    first partial hit. Substring rather than equality because users type
    ``"chrome"`` for "Google Chrome".

    Both sides are lowercased rather than relying on the caller. Callers pass
    whatever the user typed, and a token arriving as ``CHROME`` from a different
    entry point than this one would silently stop matching.
    """
    haystack = f"{entry.name} {entry.app_id}".lower()
    return all(token.lower() in haystack for token in tokens)


def start_menu_lookup(query: str, *, limit: int = 40) -> tuple[bool, str] | None:
    """Find a Start Menu application, or ``None`` when there is no match.

    Returns ``(returns_exe, app_id)``, matching the caller's existing contract.
    A registered open command wins over a shortcut: it is a concrete executable,
    where a ``.lnk`` is only a filename.

    An empty query returns ``None`` rather than the whole Start Menu. "Open
    everything" is not a thing anyone means, and returning 400 entries would push
    the entire menu into a model context.

    Packaged applications are not found here; see the module docstring.
    """
    tokens = [token for token in (query or "").lower().split() if token]
    if not tokens:
        return None

    for entry in _iter_registry_applications():
        if _matches(entry, tokens):
            return True, entry.app_id

    matches = [entry for entry in _iter_shortcut_files()
               if _matches(entry, tokens)][:limit]
    if matches:
        return False, matches[0].app_id
    return None


def start_menu_entries() -> list[StartMenuEntry]:
    """Every application this module can discover.

    Not a tool operation: this is the full menu, and there is no reason to push
    it into a model context. It exists so the discovery surface can be inspected
    and tested without a shell.
    """
    return _iter_registry_applications() + _iter_shortcut_files()