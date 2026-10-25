"""Regression tests for workspace-relative path resolution in ``tools/__init__::_p()``.

Before the fix, ``_p(value)`` returned ``Path(value)`` unchanged, so a relative
path was interpreted against the *process* current working directory. That meant a
perfectly valid workspace-relative path could be classified as SENSITIVE (outside
``allowed_roots``) simply because the process CWD happened to be elsewhere.

After the fix, ``_p(value, settings)`` resolves a relative path against
``settings.workspace_root`` so classification is consistent regardless of where the
process was started from.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.settings import Settings
from tools import _p


def _make_settings(tmp_path: Path) -> Settings:
    root = tmp_path.resolve()
    return Settings(
        base_dir=root,
        workspace_root=root / "projects",
        allowed_roots=[root],
        raw={},
    )


# ── _p() unit behaviour ─────────────────────────────────────────────────────


def test_p_relative_resolves_against_workspace_root(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    result = _p("relative/path.txt", settings)
    assert result == settings.workspace_root / "relative" / "path.txt"


def test_p_relative_resolves_against_workspace_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process CWD must not influence resolution."""
    settings = _make_settings(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    # Pretend we are running from somewhere totally unrelated.
    monkeypatch.chdir(elsewhere)
    result = _p("myapp/file.txt", settings)
    assert result == settings.workspace_root / "myapp" / "file.txt"
    assert not result.is_relative_to(elsewhere)


def test_p_absolute_path_unchanged(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    abs_path = tmp_path / "absolute" / "file.txt"
    assert _p(str(abs_path), settings) == abs_path


def test_p_without_settings_is_unchanged() -> None:
    """Backward-compatible: no settings means just Path(value)."""
    assert _p("relative/path.txt") == Path("relative/path.txt")


def test_p_nonexistent_target_is_resolved_anyway(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    result = _p("does/not/exist.txt", settings)
    assert result == settings.workspace_root / "does" / "not" / "exist.txt"


def test_p_path_with_spaces(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    result = _p("my folder/file name.txt", settings)
    assert result == settings.workspace_root / "my folder" / "file name.txt"


# ── containment with relative paths ─────────────────────────────────────────


def test_relative_dot_dot_traversal_refused(tmp_path: Path) -> None:
    """``..`` resolves against workspace_root, not CWD.

    ``../outside.txt`` from workspace_root lands at tmp/outside.txt, which is
    inside allowed_roots (root) but NOT inside workspace_root (projects).
    Resolution is deterministic and CWD-independent.
    """
    settings = _make_settings(tmp_path)
    result = _p("../outside.txt", settings)
    expected = tmp_path / "outside.txt"
    assert result.resolve() == expected.resolve()
    # Not inside workspace_root (projects) because .. went up from it.
    assert not result.resolve().is_relative_to(settings.workspace_root.resolve())


def test_relative_dot_dot_staying_inside_allowed(tmp_path: Path) -> None:
    """Containment is about the destination, not the presence of '..'.

    ``sub/../file.txt`` resolves against workspace_root, and the final
    destination is workspace_root/file.txt.
    """
    settings = _make_settings(tmp_path)
    result = _p("sub/../file.txt", settings)
    # Normalise both sides for comparison because Path keeps '..' components.
    assert result.resolve() == (settings.workspace_root / "file.txt").resolve()


# ── end-to-end: tool level_for with relative path ──────────────────────────


def test_level_for_relative_path_is_safe_regardless_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative in-root path should be SAFE even when CWD is outside roots."""
    from config.permissions import path_is_within

    settings = _make_settings(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(elsewhere)

    # Relative path that lands inside workspace_root (and therefore inside root).
    resolved = _p("projects/inbox/note.txt", settings)
    assert path_is_within(resolved, settings.allowed_roots) is True

    # Relative path that escapes workspace_root through .. should still be
    # contained-checkable: resolution is against workspace_root, not CWD.
    resolved_out = _p("../outside.txt", settings)
    assert resolved_out.resolve() == (tmp_path / "outside.txt").resolve()


# ── windows-specific behaviour ─────────────────────────────────────────────


@pytest.mark.skipif(os.name != "nt", reason="Windows drive/sep behaviour")
def test_p_windows_drive_absolute_unchanged(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    result = _p("C:\\Windows\\System32\\notepad.exe", settings)
    assert result == Path("C:\\Windows\\System32\\notepad.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows drive/sep behaviour")
def test_p_windows_relative_still_resolves_against_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path)
    monkeypatch.chdir(r"C:\Windows")
    result = _p("relative\\file.txt", settings)
    assert result == settings.workspace_root / "relative" / "file.txt"
    assert str(result).startswith(str(settings.workspace_root))
