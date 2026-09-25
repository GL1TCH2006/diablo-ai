import pytest

from config.permissions import ToolBlockedError
from tools.terminal import run_command


def test_auto_allowed_command_runs(tmp_settings):
    result = run_command("python --version", working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)
    assert result.success
    assert "Python" in result.output


def test_failing_command_reports_error(tmp_settings):
    result = run_command("this-command-does-not-exist-xyz", working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)
    assert result.success is False
    assert result.error


def test_blocklist_raises(tmp_settings):
    with pytest.raises(ToolBlockedError):
        run_command("shutdown /s /f /t 0", working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)


def test_blocklist_matches_multiple_phrases(tmp_settings):
    for command in ("del /s /q C:\\Windows", "rmdir /s /q C:\\Temp", "format D: /q"):
        with pytest.raises(ToolBlockedError):
            run_command(command, working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)


def test_empty_command_rejected(tmp_settings):
    result = run_command("   ", working_dir=str(tmp_settings.scratch_dir), settings=tmp_settings)
    assert not result.success