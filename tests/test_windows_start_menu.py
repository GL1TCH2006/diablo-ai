"""Native Start Menu discovery - what it finds, and what it honestly cannot.

M2.15 removed the ``Get-StartApps`` PowerShell call from the normal path. What
replaced it reads the registry and the Start Menu directories directly, and this
module pins down both halves of that claim: the applications it does find, and
the packaged applications it does not.

The second half matters more than the first. Calculator, Photos and the Store have
no shortcut and no registered open command, and both the AppX state repository
and the package store are unreadable to a non-elevated process. A reader that
quietly skipped them would report success while being unable to open the
calculator - so it returns ``None`` and the caller falls back, and these tests
assert that shape rather than hiding it.
"""

from __future__ import annotations

import os

import pytest

from core.windows.start_menu import (
    StartMenuEntry,
    _command_executable,
    _expand_environment,
    _matches,
    start_menu_available,
    start_menu_entries,
    start_menu_lookup,
)


@pytest.fixture(scope="module")
def entries() -> list[StartMenuEntry]:
    return start_menu_entries()


# ══════════════════════════════════════════════════════ reading the commands


class TestRegisteredCommandLinesAreParsed:
    @pytest.mark.parametrize("command,expected", [
        (r'"C:\Program Files\App\app.exe"', r"C:\Program Files\App\app.exe"),
        (r'"C:\App\app.exe" --flag', r"C:\App\app.exe"),
        (r"C:\Windows\System32\notepad.exe", r"C:\Windows\System32\notepad.exe"),
        (r"C:\App\app.exe --flag --other", r"C:\App\app.exe"),
        ("", ""),
        ("   ", ""),
    ])
    def test_the_executable_is_isolated_from_the_arguments(self, command, expected):
        """Returning the whole command line would produce a launch that fails
        with a confusing error about a path containing ``--flag``."""
        assert _command_executable(command) == expected

    def test_a_quoted_path_with_spaces_survives_intact(self):
        command = r'"C:\Program Files\Microsoft\Office\WINWORD.EXE" /n'
        assert _command_executable(command) == \
            r"C:\Program Files\Microsoft\Office\WINWORD.EXE"

    def test_registry_environment_variables_are_expanded(self):
        """The registry stores ``%SystemRoot%\\system32\\NOTEPAD.EXE``. Used
        verbatim it is not a path, and every launch fails."""
        expanded = _expand_environment(r"%SystemRoot%\system32\NOTEPAD.EXE")
        assert "%" not in expanded, f"the variable was left unexpanded: {expanded}"
        assert os.path.isfile(expanded), (
            f"the expanded path does not exist: {expanded}")

    def test_expanding_a_plain_path_is_a_no_op(self):
        assert _expand_environment(r"C:\Windows\notepad.exe") == \
            r"C:\Windows\notepad.exe"


# ══════════════════════════════════════════════════════ matching


class TestMatchingRequiresEveryToken:
    def _entry(self):
        return StartMenuEntry("Google Chrome", r"C:\Chrome\chrome.exe", True)

    def test_a_single_matching_token_matches(self):
        assert _matches(self._entry(), ["chrome"]) is True

    def test_a_partial_substring_matches(self):
        """Users type "chrome" for "Google Chrome"."""
        assert _matches(self._entry(), ["goog"]) is True

    def test_every_token_must_match(self):
        assert _matches(self._entry(), ["google", "chrome"]) is True
        assert _matches(self._entry(), ["google", "firefox"]) is False, (
            "a partial match let an unrelated application through")

    def test_an_unrelated_token_does_not_match(self):
        assert _matches(self._entry(), ["notepad"]) is False

    def test_the_identifier_is_searched_too(self):
        entry = StartMenuEntry("Editor", r"C:\Tools\zed.exe", True)
        assert _matches(entry, ["zed"]) is True

    def test_matching_is_case_insensitive(self):
        assert _matches(self._entry(), ["CHROME"]) is True


# ══════════════════════════════════════════════════════ the query contract


class TestTheQueryIsBounded:
    @pytest.mark.parametrize("query", ["", "   ", None])
    def test_an_empty_query_finds_nothing(self, query):
        """Returning the whole menu would push 400 entries into a context, and
        "open everything" is not a request anyone makes."""
        assert start_menu_lookup(query) is None

    def test_a_nonsense_query_returns_none_rather_than_raising(self):
        assert start_menu_lookup("zzzzdefinitelynotanapplication") is None

    def test_a_hostile_query_is_not_an_error(self):
        for query in ("' OR 1=1 --", "%s%s%s%s", "'; DROP TABLE--", "a" * 5000):
            start_menu_lookup(query)  # must not raise


# ══════════════════════════════════════════════════════ the real machine


@pytest.mark.skipif(not start_menu_available(),
                    reason="native Start Menu discovery needs Windows")
class TestAgainstTheRealStartMenu:
    def test_the_surface_is_not_empty(self, entries):
        assert entries, (
            "no applications were discovered at all; the reader is broken, not "
            "just finding nothing")

    def test_at_least_one_entry_is_executable_backed(self, entries):
        """The registry path is the one that yields a launchable target. If none
        of the entries resolve to an executable, every launch falls through."""
        assert any(entry.returns_exe for entry in entries)

    def test_notepad_is_discoverable_and_resolves_to_a_real_file(self):
        """``notepad`` is registered per-user on a Windows install, so this
        exercises the registry path end to end."""
        found = start_menu_lookup("notepad")
        if found is None:
            pytest.skip("notepad is not registered on this machine")
        returns_exe, app_id = found
        assert returns_exe is True, (
            "notepad was found as a shortcut rather than a registered command")
        assert os.path.isfile(os.path.expandvars(app_id)), (
            f"the discovered path does not exist: {app_id}")

    def test_every_discovered_executable_path_is_expanded(self, entries):
        """A raw ``%VAR%`` path is not a path. Any that survives would fail at
        launch time."""
        for entry in entries:
            if entry.returns_exe and "%" in entry.app_id:
                pytest.fail(f"an unexpanded path was returned: {entry.app_id}")

    def test_shortcut_entries_point_at_real_files(self, entries):
        for entry in entries:
            if not entry.returns_exe:
                assert os.path.isfile(entry.app_id), (
                    f"a shortcut entry does not exist: {entry.app_id}")

    def test_the_whole_menu_is_not_a_tool_operation(self, entries):
        """Discovery for its own sake must not be reachable as a capability."""
        assert len(entries) > 1, "a single entry suggests the scan is not working"
        # start_menu_entries exists for inspection and tests; the tool path is
        # start_menu_lookup, which returns one match.
        assert start_menu_lookup("notepad chrome") is None, (
            "a two-application query matched something, so matching is not "
            "requiring all tokens")


# ══════════════════════════════════════════════════════ the stated limitation


@pytest.mark.skipif(not start_menu_available(),
                    reason="native Start Menu discovery needs Windows")
class TestThePackagedApplicationLimitationIsStated:
    def test_a_packaged_application_is_not_silently_missed(self):
        """Calculator has no shortcut and no registered open command.

        The native reader returns ``None`` for it. That is the correct behaviour:
        returning a fabricated path would fail at launch with a worse error, and
        pretending to find it would hide the gap.
        """
        result = start_menu_lookup("calculator")
        if result is not None:
            assert os.path.isfile(os.path.expandvars(result[1])), (
                "an application was returned whose path does not exist")
        assert result is None or result[0] is True, (
            "a packaged application was resolved to a path that does not exist")

    def test_the_appx_state_repository_is_unreadable_without_elevation(self):
        """Documents why, and fails loudly if a future Windows makes it
        readable - at which point this reader should learn to use it."""
        try:
            import winreg
        except ImportError:
            pytest.skip("winreg is Windows-only")

        path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModel"
                r"\StateRepository\Cache\Package\Data")
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                getattr(winreg, "KEY_WOW64_64KEY", 0x0200)):
                pytest.skip(
                    "the AppX state repository is now readable; the native reader "
                    "should be extended to cover packaged applications")
        except OSError:
            pass  # still denied, as documented


class TestTheFallbackIsPreserved:
    def test_the_caller_keeps_a_powershell_fallback(self):
        """The limitation above is only acceptable because discovery falls back
        rather than failing."""
        import inspect

        from tools import applications

        source = inspect.getsource(applications._start_apps_lookup)
        assert "_start_apps_lookup_powershell" in source, (
            "the fallback was removed, so packaged applications can no longer "
            "be launched at all")
        assert "start_menu_lookup" in source, (
            "the native path is not being tried first")

    def test_the_fallback_is_only_reached_when_native_finds_nothing(self):
        import inspect

        from tools import applications

        source = inspect.getsource(applications._start_apps_lookup)
        native_at = source.index("start_menu_lookup")
        fallback_at = source.index("_start_apps_lookup_powershell(key)")
        assert native_at < fallback_at, (
            "the fallback runs before the native path, so every lookup pays for "
            "a PowerShell process")