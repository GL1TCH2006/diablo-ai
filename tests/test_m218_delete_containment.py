"""Batch 6 Task 3 - delete_file must refuse out-of-root targets BEFORE mutating.

Audit finding M3: ``tools/filesystem.py`` unlinked first and only then let the
verifier report UNVERIFIED, so a direct library call deleted a file outside the
allowed roots. Containment is now checked before ``unlink()``.

These tests pin the new contract and deliberately check the thing that was
missing before: **the file is still on disk after the call**.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.settings import Settings
from core.verification import VerificationState
from tools import filesystem


def _settings(root: Path) -> Settings:
    return Settings(base_dir=root, workspace_root=root, allowed_roots=[root], raw={})


def _out_of_root_refusal(result) -> bool:
    return (not result.success) and "outside the allowed roots" in (result.error or "")


# ── the contract that did not exist before ────────────────────────────────


def test_an_out_of_root_file_is_still_on_disk_after_the_call(tmp_path: Path):
    """The core of M3: previously the file was deleted, then merely reported."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("still here\n", encoding="utf-8")

    result = filesystem.delete_file(str(victim), _settings(root))

    assert victim.exists(), (
        "delete_file removed a file outside the allowed roots; containment must be "
        "checked before unlink(), not after")
    assert result.success is False
    assert _out_of_root_refusal(result), result.error
    assert victim.read_text(encoding="utf-8") == "still here\n"


def test_the_refusal_message_says_nothing_was_deleted(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    victim = tmp_path / "elsewhere.txt"
    victim.write_text("x\n", encoding="utf-8")

    result = filesystem.delete_file(str(victim), _settings(root))

    assert "Nothing deleted" in result.error, result.error


def test_the_refusal_still_carries_an_unverified_verdict(tmp_path: Path):
    """Callers and the ledger must still see that nothing was independently checked."""
    root = tmp_path / "allowed"
    root.mkdir()
    victim = tmp_path / "elsewhere.txt"
    victim.write_text("x\n", encoding="utf-8")

    result = filesystem.delete_file(str(victim), _settings(root))

    assert result.verification is not None, "a refusal must still report a verdict"
    assert result.verification.state is VerificationState.UNVERIFIED
    assert result.verification.evidence["contained"] is False
    assert result.verification.evidence["observed_independently"] is False


# ── the behaviour that must NOT regress ───────────────────────────────────


def test_an_in_root_delete_still_succeeds_and_verifies_passed(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "doomed.txt"
    target.write_text("bye\n", encoding="utf-8")

    result = filesystem.delete_file(str(target), _settings(root))

    assert result.success is True, result.error
    assert not target.exists()
    assert result.verification.state is VerificationState.PASSED, (
        f"a genuine in-root delete must still verify PASSED, got "
        f"{result.verification.state.value}")


def test_d1_a_nonexistent_file_still_fails_with_not_found(tmp_path: Path):
    """D1 was ruled in Batch 4 and is not reopened here."""
    root = tmp_path / "allowed"
    root.mkdir()

    result = filesystem.delete_file(str(root / "ghost.txt"), _settings(root))

    assert result.success is False
    assert "not found" in result.error.lower(), result.error


def test_a_dot_dot_traversal_escape_stays_on_disk(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("secret\n", encoding="utf-8")
    escape = root / ".." / "outside" / "secret.txt"

    result = filesystem.delete_file(str(escape), _settings(root))

    assert victim.exists(), "a '..' escape deleted the file"
    assert result.success is False


@pytest.mark.skipif(os.name != "nt", reason="symlinks need a privilege on Windows")
def test_a_symlink_escape_stays_on_disk(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("secret\n", encoding="utf-8")
    link = root / "looks-inside.txt"
    try:
        link.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = filesystem.delete_file(str(link), _settings(root))

    assert victim.exists(), (
        "a symlink inside the root pointing outside it was followed and the real "
        "file deleted; path_is_within resolves links, so this must be refused")


# ── missing settings / roots fail closed ──────────────────────────────────


def test_missing_settings_fails_closed(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    victim = root / "target.txt"
    victim.write_text("x\n", encoding="utf-8")

    result = filesystem.delete_file(str(victim), None)

    assert victim.exists(), (
        "delete_file with no settings deleted a file; with no roots there is "
        "nothing to check containment against, so it must fail closed")
    assert result.success is False
    assert "no allowed roots" in result.error, result.error


def test_empty_allowed_roots_fails_closed(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    victim = root / "target.txt"
    victim.write_text("x\n", encoding="utf-8")
    settings = Settings(base_dir=root, workspace_root=root, allowed_roots=[], raw={})

    result = filesystem.delete_file(str(victim), settings)

    assert victim.exists()
    assert result.success is False


def test_a_directory_is_still_refused_without_touching_it(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    victim = root / "subdir"
    victim.mkdir()

    result = filesystem.delete_file(str(victim), _settings(root))

    assert victim.is_dir()
    assert result.success is False
    assert "directory" in result.error.lower()
