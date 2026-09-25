"""Hard-block policy for destructive shell commands.

The configured blocklist is a flat substring list. These tests pin the
token-aware detector that understands PowerShell-native destructive forms,
their aliases and argument ordering, and prove it adds blocks without
introducing false positives on harmless usage.
"""
import tomllib
from pathlib import Path

import pytest

from config.permissions import (
    PermissionLevel,
    PermissionManager,
    ToolBlockedError,
    contains_block_phrase,
    destructive_command_reason,
    destructive_shell_invocation,
)
from config.settings import CONFIG_FILE
from tools.terminal import run_command


def _evaluate(tmp_settings, command, blocklist=None):
    if blocklist is not None:
        tmp_settings.terminal_blocklist = list(blocklist)
    manager = PermissionManager(tmp_settings)
    return manager.evaluate("terminal.execute", {"command": command}, PermissionLevel.SENSITIVE)


def _assert_blocked(tmp_settings, command, blocklist=None):
    decision = _evaluate(tmp_settings, command, blocklist)
    assert decision.blocked, f"expected hard block for {command!r}, got {decision!r}"
    assert decision.allow is False
    assert decision.requires_confirmation is False
    return decision


def _assert_not_blocked(tmp_settings, command, blocklist=None):
    decision = _evaluate(tmp_settings, command, blocklist)
    assert not decision.blocked, f"unexpected hard block for {command!r}: {decision!r}"
    return decision


def _assert_still_confirmed(tmp_settings, command, blocklist=None):
    """Dangerous but intentionally not hard-blocked: must stay SENSITIVE + confirm."""
    decision = _assert_not_blocked(tmp_settings, command, blocklist)
    assert decision.requires_confirmation is True, f"{command!r} should still require confirmation"
    assert decision.allow is False
    return decision


# ---------------------------------------------------------------------------
# 1 + 2. Remove-Item -Recurse / -Recurse -Force
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse",
    "Remove-Item -Recurse -Force",
    "Remove-Item -Recurse -Force C:\\temp\\build",
    "Remove-Item -Force -Recurse C:\\temp\\build",
    "Remove-Item C:\\temp\\build -Recurse",
    "Remove-Item -Path C:\\temp\\build -Recurse -Force",
    "remove-item -recurse -force .",
    "REMOVE-ITEM -RECURSE -FORCE .",
])
def test_powershell_remove_item_recurse_is_hard_blocked(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


# ---------------------------------------------------------------------------
# 3. argument-order variations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "Remove-Item C:\\temp -Force -Recurse",
    "Remove-Item -Force C:\\temp -Recurse",
    "Remove-Item C:\\temp -Recurse -Force",
    "Remove-Item -Recurse C:\\temp -Force",
    "rm -Force -Recurse C:\\temp",
    "rd -Force -Recurse C:\\temp",
])
def test_recursive_switch_is_found_in_any_argument_position(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse; Get-Date",
    "Get-Date; Remove-Item -Recurse x",
    "Get-Date | Remove-Item -Recurse x",
    "Get-Date && Remove-Item -Recurse x",
])
def test_destructive_invocation_is_found_after_a_command_separator(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


# ---------------------------------------------------------------------------
# 4. PowerShell aliases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "ri -rf .",
    "ri -Recurse -Force C:\\temp",
    "ri -r C:\\temp",
    "rm -rf C:\\temp",
    "rm -fr C:\\temp",
    "rm -r C:\\temp",
    "rm C:\\temp -Recurse",
    "rm x -Recurse",
    "rd /s C:\\temp",
    "rd /s /q C:\\temp",
    "rmdir /s C:\\temp",
    "del /s C:\\temp",
    "del /f /s C:\\temp",
    "erase C:\\temp -Recurse",
])
def test_powershell_and_cmd_aliases_are_hard_blocked(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


# ---------------------------------------------------------------------------
# 5. Clear-Content needs no recursive switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "Clear-Content -Path notes.txt",
    "Clear-Content notes.txt",
    "clear-content -Path C:\\temp\\notes.txt",
    "clc notes.txt",
    "clc -Path C:\\temp\\notes.txt",
])
def test_clear_content_is_hard_blocked(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


# ---------------------------------------------------------------------------
# 6. destructive paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "Remove-Item C:\\",
    "Remove-Item C:/",
    "Remove-Item D:\\",
    "Remove-Item -Path C:\\Windows",
    "Remove-Item C:\\Windows\\System32",
    'Remove-Item "C:\\Program Files"',
    "Remove-Item 'C:\\Program Files (x86)'",
    "Remove-Item C:\\ProgramData",
    "Remove-Item C:\\Users",
    "Remove-Item $Recycle.Bin",
    "Remove-Item C:\\$Recycle.Bin",
    "Remove-Item C:\\Users -Recurse -Force",
    "rm C:\\Users -Recurse",
    "Remove-Item C:\\",
    "rm -rf /",
    "del /f /q .",
    "rd /s .",
    "Remove-Item *",
    "Remove-Item .",
    "Remove-Item ..",
    "Remove-Item .\\",
])
def test_destructive_paths_are_hard_blocked(tmp_settings, command):
    _assert_blocked(tmp_settings, command)


# ---------------------------------------------------------------------------
# 7. harmless usage must NOT match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    # single-item removal stays confirmation-gated, not hard blocked
    "Remove-Item notes.txt",
    "Remove-Item -Path notes.txt",
    "Remove-Item -LiteralPath notes.txt",
    "Remove-Item .\\notes.txt",
    "Remove-Item build\\out.tmp",
    "Remove-Item *.log",
    "Remove-Item",
    "rm notes.txt",
    "del notes.txt",
    "Remove-Item -WhatIf -Recurse:$false x",
    "Remove-Item C:\\PROJECTS\\temp\\scratch",
    "Remove-Item C:\\Users\\raumaan\\Documents\\tmp",
    # recursive reads and copies are not removals
    "Get-ChildItem -Recurse -Filter *.py",
    "Get-ChildItem -Recurse",
    "Copy-Item -Recurse src dst",
    "Move-Item -Recurse a b",
    "Compress-Archive -Path src -Recurse -DestinationPath out.zip",
    # creation and writes
    "New-Item -ItemType Directory -Force out",
    "Set-Content -Path notes.txt -Value hello",
    "Get-Content notes.txt",
    "Add-Content -Path notes.txt -Value hello",
    # the verb only appears as data
    "Select-String -Path src -Pattern Remove-Item",
    "Get-Process rm",
    "echo done",
    "python build.py \"Remove-Item -Recurse x\"",
])
def test_harmless_usage_is_not_hard_blocked(tmp_settings, command):
    _assert_still_confirmed(tmp_settings, command)


@pytest.mark.parametrize("command", [
    "where rm",
    "where.exe del",
    "Get-Command Remove-Item",
])
def test_lookup_commands_are_never_hard_blocked(tmp_settings, command):
    _assert_not_blocked(tmp_settings, command)


@pytest.mark.parametrize("command", [
    "git rm -rf --cached build",
    "git rm notes.txt",
    "npm rm some-package",
    "pip uninstall requests",
])
def test_other_tools_rm_subcommand_is_not_matched_by_the_detector(command):
    # The detector must not treat another tool's own "rm"/"uninstall" as a
    # shell removal. (The configured phrase list may still block some of these
    # on its own; that is pre-existing behaviour and is asserted separately.)
    assert destructive_shell_invocation(command) is None


def test_git_rm_blocked_by_shipped_phrase_list_is_preserved(tmp_settings):
    # Pre-existing behaviour: the shipped substring list contains "rm -rf".
    decision = _evaluate(tmp_settings, "git rm -rf --cached build",
                         blocklist=_shipped_blocklist())
    assert decision.blocked
    assert "blocked phrase" in decision.reason


def test_read_only_auto_allow_still_works(tmp_settings):
    for command in ("git status", "dir", "ls", "python --version"):
        decision = _evaluate(tmp_settings, command)
        assert decision.allow is True, command
        assert decision.requires_confirmation is False, command


# ---------------------------------------------------------------------------
# 8. existing cmd/POSIX hard blocks are unchanged
# ---------------------------------------------------------------------------

LEGACY_BLOCKLIST = [
    "format", "shutdown", "reboot", "diskpart", "bcdedit", "del /s", "rm -rf",
    "rmdir /s", "rd /s", "erase", "deltree",
]


@pytest.mark.parametrize("command", [
    "shutdown /s",
    "shutdown /s /f /t 0",
    "reboot",
    "format C:",
    "format D: /q",
    "diskpart",
    "bcdedit /set",
    "del /s C:\\x",
    "rm -rf /",
    "rmdir /s /q C:\\Windows",
    "rd /s /q C:\\Temp",
    "erase C:\\x",
    "deltree C:\\x",
])
def test_legacy_blocklist_entries_still_hard_block(tmp_settings, command):
    decision = _assert_blocked(tmp_settings, command, blocklist=LEGACY_BLOCKLIST)
    assert "destructive-command policy" in decision.reason


@pytest.mark.parametrize("command", [
    "git status",
    "dir",
    "python --version",
    "where notepad",
    "pip install requests",
    "pytest -q",
])
def test_legacy_blocklist_does_not_block_ordinary_commands(tmp_settings, command):
    _assert_not_blocked(tmp_settings, command, blocklist=LEGACY_BLOCKLIST)


def test_phrase_matching_semantics_are_preserved(tmp_settings):
    # contains_block_phrase keeps its exact substring behaviour.
    assert contains_block_phrase("shutdown /s", LEGACY_BLOCKLIST) == "shutdown"
    assert contains_block_phrase("git status", LEGACY_BLOCKLIST) is None
    # The detector alone (empty blocklist) still hard-blocks destructive forms.
    decision = _evaluate(tmp_settings, "Remove-Item -Recurse -Force x", blocklist=[])
    assert decision.blocked


# ---------------------------------------------------------------------------
# shipped configuration
# ---------------------------------------------------------------------------

def _shipped_blocklist() -> list[str]:
    data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return list(data.get("terminal", {}).get("blocklist", []))


def test_shipped_config_blocklist_is_non_empty():
    assert _shipped_blocklist(), "config/default_config.toml must ship a blocklist"


@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse -Force C:\\temp",
    "ri -rf .",
    "rm -fr /tmp",
    "del /f /q .",
    "Clear-Content -Path notes.txt",
    "clc notes.txt",
    "Remove-Item C:\\Users",
    "shutdown /s",
    "rmdir /s /q C:\\Windows",
])
def test_shipped_config_hard_blocks_destructive_commands(tmp_settings, command):
    _assert_blocked(tmp_settings, command, blocklist=_shipped_blocklist())


@pytest.mark.parametrize("command", [
    "Remove-Item notes.txt",
    "Get-ChildItem -Recurse",
    "git status",
])
def test_shipped_config_does_not_block_harmless_commands(tmp_settings, command):
    _assert_not_blocked(tmp_settings, command, blocklist=_shipped_blocklist())


# ---------------------------------------------------------------------------
# terminal tool defence in depth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse -Force .",
    "ri -rf .",
    "Clear-Content -Path x",
    "del /f /q .",
    "shutdown /s /f /t 0",
])
def test_terminal_tool_raises_on_destructive_commands(tmp_settings, command):
    with pytest.raises(ToolBlockedError):
        run_command(command, working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)


def test_terminal_tool_still_runs_harmless_commands(tmp_settings):
    result = run_command("python --version", working_dir=str(tmp_settings.scratch_dir),
                         settings=tmp_settings)
    assert result.success


# ---------------------------------------------------------------------------
# detector contract
# ---------------------------------------------------------------------------

def test_reason_is_deterministic():
    command = "Remove-Item -Force -Recurse C:\\temp"
    reasons = {destructive_shell_invocation(command) for _ in range(25)}
    assert len(reasons) == 1
    assert destructive_shell_invocation("") is None
    assert destructive_shell_invocation("   ") is None
    assert destructive_shell_invocation(None) is None


def test_reason_combines_phrase_and_invocation():
    reason = destructive_command_reason("shutdown /s", LEGACY_BLOCKLIST)
    assert reason and "shutdown" in reason
    reason = destructive_command_reason("ri -rf .", LEGACY_BLOCKLIST)
    assert reason and "destructive invocation" in reason
    assert destructive_command_reason("git status", LEGACY_BLOCKLIST) is None
    assert destructive_command_reason("git status", []) is None


def test_denial_reason_is_reported_in_the_decision(tmp_settings):
    decision = _assert_blocked(tmp_settings, "ri -rf .")
    assert "destructive-command policy" in decision.reason
    assert "destructive invocation" in decision.reason


def test_session_block_still_applies_to_safe_commands(tmp_settings):
    manager = PermissionManager(tmp_settings)
    manager.session_block("terminal.execute")
    decision = manager.evaluate("terminal.execute", {"command": "Get-Date"},
                                PermissionLevel.SENSITIVE)
    assert decision.blocked
