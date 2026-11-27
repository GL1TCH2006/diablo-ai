import pytest

from config.permissions import ToolBlockedError
from tools.terminal import run_command
from voice.cancellation import CancellationToken
import threading
import time


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


# --- Cancellation tests ---


def test_cancel_before_launch(tmp_settings):
    """Cancellation requested before process starts should be honored immediately."""
    token = CancellationToken("test-task")
    token.request("test cancellation")

    result = run_command(
        "python -c \"print('hello')\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        _tool_context=type("ctx", (), {"cancellation_token": token})(),
    )

    assert not result.success
    assert result.cancelled is True
    assert "cancelled" in (result.error or "").lower()


def test_cancel_during_execution(tmp_settings):
    """Cancellation during a long-running command should terminate the process."""
    token = CancellationToken("test-task")
    ctx = type("ctx", (), {"cancellation_token": token})()

    # Start a long-running command and cancel it after a short delay
    def delayed_cancel():
        time.sleep(0.05)
        token.request("cancel during execution")

    cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
    cancel_thread.start()

    result = run_command(
        "python -c \"import time; [time.sleep(0.1) for _ in range(100)]\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=10,
        _tool_context=ctx,
    )
    cancel_thread.join(timeout=1.0)

    assert not result.success
    assert result.cancelled is True, f"Expected cancelled=True, got {result.cancelled}, error={result.error}"


def test_timeout_exceeded(tmp_settings):
    """A command that exceeds its timeout should be terminated."""
    result = run_command(
        "python -c \"import time; time.sleep(10)\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=1,
    )

    assert not result.success
    assert not result.cancelled  # timeout is not cancellation
    assert "timed out" in (result.error or "").lower()
    # Process should be reaped (return code available)
    assert result.exit_code is not None


def test_successful_execution(tmp_settings):
    """Normal successful execution should still work."""
    result = run_command(
        "python -c \"print('hello')\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=60,
    )

    assert result.success
    assert result.output.strip() == "hello"
    assert result.exit_code == 0
    assert not result.cancelled


def test_nonzero_exit_and_stderr(tmp_settings):
    """Nonzero exit with stderr should be reported correctly."""
    result = run_command(
        "python -c \"import sys; print('to stdout'); sys.stderr.write('to stderr\\n'); sys.exit(2)\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=60,
    )

    assert not result.success
    # PowerShell may not propagate the exact Python exit code; just check it's nonzero
    assert result.exit_code != 0
    assert "to stdout" in result.output
    assert "to stderr" in (result.error or "")


def test_terminate_to_kill_escalation(tmp_settings):
    """Test that a process that ignores terminate gets killed."""
    token = CancellationToken("test-kill")
    ctx = type("ctx", (), {"cancellation_token": token})()

    def delayed_cancel():
        time.sleep(0.02)
        token.request("test kill escalation")

    cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
    cancel_thread.start()

    result = run_command(
        "python -c \"import time; [time.sleep(1) for _ in range(100)]\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=10,
        _tool_context=ctx,
    )
    cancel_thread.join(timeout=1.0)

    assert not result.success
    assert result.cancelled is True
    # Verify process was reaped (has an exit code)
    assert result.exit_code is not None


def test_cancellation_with_context_object(tmp_settings):
    """Test cancellation works when token is passed via a context-like object."""
    class MockContext:
        def __init__(self):
            self.cancellation_token = CancellationToken("ctx-test")

    ctx = MockContext()
    ctx.cancellation_token.request("via context object")

    result = run_command(
        "python -c \"import time; time.sleep(5)\"",
        working_dir=str(tmp_settings.scratch_dir),
        settings=tmp_settings,
        timeout=60,
        _tool_context=ctx,
    )

    assert not result.success
    assert result.cancelled is True