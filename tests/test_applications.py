import pytest

from tools.applications import _resolve_app, open_app


def test_whitelist_known_path_resolves(tmp_settings):
    exe, display, source = _resolve_app("notepad", tmp_settings)
    assert exe is not None
    assert "notepad.exe" in exe.lower()
    assert source == "known path"


def test_whitelist_token_resolves_via_path(tmp_settings):
    exe, display, source = _resolve_app("calculator", tmp_settings)
    assert exe is not None
    assert source == "whitelist"


def test_unknown_app_not_resolved(tmp_settings, monkeypatch):
    monkeypatch.setattr("tools.applications._start_apps_lookup", lambda key: None)
    exe, display, source = _resolve_app("totally-not-real-app-xyz", tmp_settings)
    assert exe is None


def test_default_config_has_no_developer_specific_app_paths():
    """No committed absolute path may name a Windows user.

    An absolute path in shared config embeds one person's profile and silently
    breaks for everyone else. This reads the shipped TOML rather than a fixture,
    so the guarantee is about the file that actually ships.
    """
    import tomllib
    from config.settings import CONFIG_FILE

    data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    known = data.get("apps", {}).get("known_paths", {})
    assert known, "the known_paths table vanished; expected at least the machine-wide entry"

    for alias, path in known.items():
        lowered = str(path).lower().replace("\\", "/")
        # A real profile looks like C:/Users/<someone> or %USERPROFILE%.
        assert "/users/" not in lowered, (
            f"[apps.known_paths].{alias} names a Windows user profile: {path!r}")
        assert "%userprofile%" not in lowered, (
            f"[apps.known_paths].{alias} hardcodes a user profile variable: {path!r}")
        assert "%localappdata%" not in lowered, (
            f"[apps.known_paths].{alias} pins a per-user location; that belongs in "
            f"DISCOVERY_CANDIDATES, not in committed config: {path!r}")


def test_discovery_finds_a_per_user_install_without_any_config(monkeypatch, tmp_path):
    """A per-user install is found by probing, with no configured path.

    This is the behaviour the removed "vs code" entry used to provide by accident,
    for exactly one Windows user. Here it is produced for whoever is asking.
    """
    from tools import applications

    exe = tmp_path / "Code.exe"
    exe.write_text("MZ", encoding="utf-8")
    monkeypatch.setattr(applications, "_DISCOVERY_CACHE", {})
    monkeypatch.setitem(
        applications._DISCOVERY_CANDIDATES, "vs code", (str(exe),))

    found = applications.discover_app_path("vs code")

    assert found == str(exe), (
        f"a real, existing candidate was not discovered: {found!r}")


def test_discovery_expands_environment_variables(monkeypatch, tmp_path):
    """A relocated profile is found through %LOCALAPPDATA%, not hardcoded."""
    from tools import applications

    root = tmp_path / "Profiles" / "someone-else"
    (root / "Programs" / "Microsoft VS Code").mkdir(parents=True)
    exe = root / "Programs" / "Microsoft VS Code" / "Code.exe"
    exe.write_text("MZ", encoding="utf-8")

    monkeypatch.setattr(applications, "_DISCOVERY_CACHE", {})
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setitem(
        applications._DISCOVERY_CANDIDATES, "vs code",
        (r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",))

    found = applications.discover_app_path("vs code")

    assert found == str(exe), (
        f"an existing %LOCALAPPDATA% install was not discovered: {found!r}")


def test_discovery_returns_none_when_nothing_matches(monkeypatch, tmp_path):
    """A miss is honest: no candidate exists, so no path is invented."""
    from tools import applications

    monkeypatch.setattr(applications, "_DISCOVERY_CACHE", {})
    monkeypatch.setitem(
        applications._DISCOVERY_CANDIDATES, "vs code",
        (str(tmp_path / "nope" / "Code.exe"),))

    assert applications.discover_app_path("vs code") is None


def test_discovery_does_not_hide_a_configured_path(monkeypatch, tmp_settings):
    """A configured known_path still wins. Existing behaviour is unchanged."""
    from tools import applications

    monkeypatch.setattr(applications, "_DISCOVERY_CACHE", {})
    monkeypatch.setitem(
        applications._DISCOVERY_CANDIDATES, "notepad", (r"%WINDIR%\notepad.exe",))

    exe, display, source = _resolve_app("notepad", tmp_settings)

    assert source == "known path", (
        f"a configured known path was displaced by discovery: {source}")
    assert exe is not None and "notepad.exe" in exe.lower()


def test_resolve_app_reports_discovery_as_its_own_source(monkeypatch, tmp_settings):
    """Discovery is visible in the tool output rather than masquerading as config."""
    from tools import applications

    discovered = tmp_settings.scratch_dir / "discovered.exe"
    discovered.write_text("MZ", encoding="utf-8")
    monkeypatch.setattr(applications, "_DISCOVERY_CACHE", {})
    monkeypatch.setattr(applications.shutil, "which", lambda _t: None)
    monkeypatch.setattr(applications, "_start_apps_lookup", lambda key: None)
    monkeypatch.setitem(
        applications._DISCOVERY_CANDIDATES, "zzz-unlisted-app", (str(discovered),))

    exe, display, source = _resolve_app("zzz-unlisted-app", tmp_settings)

    assert exe == str(discovered)
    assert source == "discovered path"


def test_open_unknown_app_errors(tmp_settings, monkeypatch):
    monkeypatch.setattr("tools.applications._start_apps_lookup", lambda key: None)
    result = open_app("totally-not-real-app-xyz", tmp_settings)
    assert not result.success
    assert "not found" in result.error.lower()