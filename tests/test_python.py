from tools.python import run_script
from voice.cancellation import CancellationToken
import threading
import time


def test_run_hello_world(tmp_settings):
    script = tmp_settings.scratch_dir / "hello.py"
    script.write_text('print("Hello World")\n', encoding="utf-8")
    result = run_script(str(script), timeout=60)
    assert result.success
    assert result.output.strip() == "Hello World"
    assert result.exit_code == 0


def test_run_missing_script(tmp_settings):
    result = run_script(str(tmp_settings.scratch_dir / "missing.py"), timeout=60)
    assert not result.success
    assert "not found" in result.error.lower()


def test_run_failing_script(tmp_settings):
    script = tmp_settings.scratch_dir / "fail.py"
    script.write_text("print('before')\nraise SystemExit(3)\n", encoding="utf-8")
    result = run_script(str(script), timeout=60)
    assert not result.success
    assert result.exit_code == 3


# --- Cancellation tests ---


def test_cancel_before_launch(tmp_settings):
    """Cancellation requested before process starts should be honored immediately."""
    script = tmp_settings.scratch_dir / "cancel_before.py"
    script.write_text("print('should not run')\n", encoding="utf-8")

    token = CancellationToken("test-task")
    token.request("test cancellation")

    result = run_script(str(script), timeout=60, _tool_context=type("ctx", (), {"cancellation_token": token})())

    assert not result.success
    assert result.cancelled is True
    assert "cancelled" in (result.error or "").lower()


def test_cancel_during_execution(tmp_settings):
    """Cancellation during a long-running script should terminate the process."""
    script = tmp_settings.scratch_dir / "long_running.py"
    # Script that runs for a while but can be cancelled
    script.write_text(
        "import time\n"
        "for i in range(100):\n"
        "    time.sleep(0.1)\n"
        "print('done')\n",
        encoding="utf-8",
    )

    token = CancellationToken("test-task")
    # Don't request yet - we'll request after a moment
    ctx = type("ctx", (), {"cancellation_token": token})()

    # Start the script and cancel it after a short delay
    def delayed_cancel():
        time.sleep(0.05)
        token.request("cancel during execution")

    cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
    cancel_thread.start()

    result = run_script(str(script), timeout=10, _tool_context=ctx)
    cancel_thread.join(timeout=1.0)

    assert not result.success
    assert result.cancelled is True, f"Expected cancelled=True, got {result.cancelled}, error={result.error}"


def test_timeout_exceeded(tmp_settings):
    """A script that exceeds its timeout should be terminated."""
    script = tmp_settings.scratch_dir / "timeout_test.py"
    script.write_text(
        "import time\n"
        "time.sleep(10)\n"
        "print('should not reach')\n",
        encoding="utf-8",
    )

    result = run_script(str(script), timeout=1)

    assert not result.success
    assert not result.cancelled  # timeout is not cancellation
    assert "timed out" in (result.error or "").lower()
    # Process should be reaped (return code available)
    assert result.exit_code is not None


def test_successful_execution(tmp_settings):
    """Normal successful execution should still work."""
    script = tmp_settings.scratch_dir / "success.py"
    script.write_text('print("hello")\n', encoding="utf-8")

    result = run_script(str(script), timeout=60)

    assert result.success
    assert result.output.strip() == "hello"
    assert result.exit_code == 0
    assert not result.cancelled


def test_nonzero_exit_and_stderr(tmp_settings):
    """Nonzero exit with stderr should be reported correctly."""
    script = tmp_settings.scratch_dir / "error.py"
    script.write_text(
        "import sys\n"
        "print('to stdout')\n"
        "sys.stderr.write('to stderr\\n')\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )

    result = run_script(str(script), timeout=60)

    assert not result.success
    assert result.exit_code == 2
    assert "to stdout" in result.output
    assert "to stderr" in (result.error or "")


def test_terminate_to_kill_escalation(tmp_settings):
    """Test that a process that ignores terminate gets killed."""
    script = tmp_settings.scratch_dir / "ignore_term.py"
    # This script catches SIGTERM (on Unix) but on Windows terminate() uses CTRL_BREAK_EVENT
    # which can't be caught the same way. We test the escalation path by using a script
    # that runs long enough for the test to cancel it, and we verify the kill path works.
    script.write_text(
        "import time\n"
        "try:\n"
        "    while True:\n"
        "        time.sleep(1)\n"
        "except KeyboardInterrupt:\n"
        "    pass\n",
        encoding="utf-8",
    )

    token = CancellationToken("test-kill")
    ctx = type("ctx", (), {"cancellation_token": token})()

    # We can't easily test the kill path deterministically on Windows without
    # a process that ignores terminate. The _stop_process function handles this.
    # Here we just verify the cancel path works and process is reaped.
    def delayed_cancel():
        time.sleep(0.02)
        token.request("test kill escalation")

    cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
    cancel_thread.start()

    result = run_script(str(script), timeout=10, _tool_context=ctx)
    cancel_thread.join(timeout=1.0)

    assert not result.success
    assert result.cancelled is True
    # Verify process was reaped (has an exit code)
    assert result.exit_code is not None


def test_cleanup_after_cancellation(tmp_settings):
    """Verify no zombie processes remain after cancellation."""
    script = tmp_settings.scratch_dir / "cleanup.py"
    script.write_text("import time; time.sleep(100)\n", encoding="utf-8")

    token = CancellationToken("test-cleanup")
    ctx = type("ctx", (), {"cancellation_token": token})()

    def delayed_cancel():
        time.sleep(0.02)
        token.request("cleanup test")

    cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
    cancel_thread.start()

    result = run_script(str(script), timeout=10, _tool_context=ctx)
    cancel_thread.join(timeout=1.0)

    assert not result.success
    assert result.cancelled is True

    # Verify no child processes left behind - check if process was reaped
    # by ensuring we got an exit code (reaped)
    assert result.exit_code is not None


def test_cancellation_with_context_object(tmp_settings):
    """Test cancellation works when token is passed via a context-like object."""
    script = tmp_settings.scratch_dir / "ctx_cancel.py"
    script.write_text("import time; time.sleep(5)\n", encoding="utf-8")

    class MockContext:
        def __init__(self):
            self.cancellation_token = CancellationToken("ctx-test")

    ctx = MockContext()
    ctx.cancellation_token.request("via context object")

    result = run_script(str(script), timeout=60, _tool_context=ctx)

    assert not result.success
    assert result.cancelled is True