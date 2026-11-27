from pathlib import Path

from config.permissions import PermissionLevel, PermissionManager, path_is_within
from memory.preferences import PreferencesStore


class _Prefs:
    def __init__(self, store):
        self._store = store

    def get_permission(self, tool):
        return self._store.get_permission(tool)


def _mgmt(tmp_settings, prefs=None):
    return PermissionManager(tmp_settings, prefs)


def test_terminal_blocked_by_blocklist(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("terminal.execute", {"command": "shutdown /s"}, PermissionLevel.SENSITIVE)
    assert decision.blocked
    assert decision.allow is False


def test_terminal_auto_allow_pattern(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("terminal.execute", {"command": "git status"}, PermissionLevel.SENSITIVE)
    assert decision.allow is True
    assert decision.requires_confirmation is False


def test_terminal_sensitive_requires_confirmation(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("terminal.execute", {"command": "pip install requests"}, PermissionLevel.SENSITIVE)
    assert decision.requires_confirmation is True
    assert decision.allow is False


def test_safe_level_auto(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("system.info", {}, PermissionLevel.SAFE)
    assert decision.allow is True
    assert not decision.requires_confirmation


def test_sensitive_level_requires_confirmation(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("filesystem.delete_file", {"path": "C:/x.txt"}, PermissionLevel.SENSITIVE)
    assert decision.requires_confirmation


def test_normal_level_auto(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("python.run_script", {"script": "ok.py"}, PermissionLevel.NORMAL)
    assert decision.allow is True
    assert not decision.requires_confirmation


def test_persistent_pref_deny(tmp_settings, tmp_path):
    store = PreferencesStore(tmp_path / "mem")
    store.set_permission("filesystem.write_file", "deny")
    decision = _mgmt(tmp_settings, _Prefs(store)).evaluate("filesystem.write_file", {"path": "a.py"}, PermissionLevel.NORMAL)
    assert decision.blocked


def test_persistent_pref_allow(tmp_settings, tmp_path):
    store = PreferencesStore(tmp_path / "mem")
    store.set_permission("filesystem.delete_file", "allow")
    decision = _mgmt(tmp_settings, _Prefs(store)).evaluate("filesystem.delete_file", {"path": "a.py"}, PermissionLevel.SENSITIVE)
    assert decision.allow is True
    assert not decision.requires_confirmation


def test_user_session_block(tmp_settings):
    manager = _mgmt(tmp_settings)
    manager.session_block("applications.open")
    decision = manager.evaluate("applications.open", {"app": "calc"}, PermissionLevel.NORMAL)
    assert decision.blocked


def test_blocked_config(tmp_settings):
    decision = _mgmt(tmp_settings).evaluate("filesystem.delete_file", {"path": "x"}, PermissionLevel.BLOCKED)
    assert decision.blocked


def test_level_for_resolver_clamps(tmp_settings, tmp_path):
    inside = str(tmp_path / "data" / "x.txt")
    outside = str(Path("C:/Windows/temp/e.txt"))
    decision = _mgmt(tmp_settings).evaluate("filesystem.write_file",
                                            {"path": inside}, PermissionLevel.SENSITIVE,
                                            level_for=lambda a: PermissionLevel.NORMAL if path_is_within(Path(a["path"]), tmp_settings.allowed_roots) else PermissionLevel.SENSITIVE)
    assert decision.allow is True
    decision_out = _mgmt(tmp_settings).evaluate("filesystem.write_file",
                                                {"path": outside}, PermissionLevel.SENSITIVE,
                                                level_for=lambda a: PermissionLevel.NORMAL if path_is_within(Path(a["path"]), tmp_settings.allowed_roots) else PermissionLevel.SENSITIVE)
    assert decision_out.requires_confirmation