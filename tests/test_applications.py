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


def test_open_unknown_app_errors(tmp_settings, monkeypatch):
    monkeypatch.setattr("tools.applications._start_apps_lookup", lambda key: None)
    result = open_app("totally-not-real-app-xyz", tmp_settings)
    assert not result.success
    assert "not found" in result.error.lower()