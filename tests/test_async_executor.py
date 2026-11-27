"""Unit tests for ``core.async_executor.AsyncExecutor`` (M3.1 untracked module).

All tests are bounded and read-only: they use fake tools, no real subprocesses,
no network, no paid APIs, and no temporary directories.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from core.async_executor import AsyncExecutor
from config.permissions import PermissionLevel, PermissionManager
from config.settings import Settings
from core.cancellation import CancellationToken
from core.events import EventBus
from tools.registry import ToolRegistry, ToolSpec, ToolResult

# Captured keyword arguments the last time ``fake.capture`` was invoked. Used to
# assert that AsyncExecutor injects tool timeouts into the call arguments.
captured_tool_kwargs: dict = {}


def capturing_tool(**kwargs):
    captured_tool_kwargs.clear()
    captured_tool_kwargs.update(kwargs)
    return ToolResult(True, output="captured")


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def ok_tool(**kwargs):
        return ToolResult(True, output="ok output")

    def fail_tool(**kwargs):
        return ToolResult(False, error="tool reported failure")

    def slow_tool(**kwargs):
        time.sleep(0.1)
        return ToolResult(True, output="slow done")

    registry.register(ToolSpec(description="test tool", name="fake.ok", fn=ok_tool, level=PermissionLevel.SAFE))
    registry.register(ToolSpec(description="test tool", name="fake.fail", fn=fail_tool, level=PermissionLevel.SAFE))
    registry.register(ToolSpec(description="test tool", name="fake.slow", fn=slow_tool, level=PermissionLevel.SAFE))
    registry.register(ToolSpec(description="test tool", name="fake.capture", fn=capturing_tool, level=PermissionLevel.SAFE))
    return registry


def _make_executor(**kwargs) -> AsyncExecutor:
    settings = Settings()
    settings.max_result_chars = 8000
    return AsyncExecutor(
        registry=_make_registry(),
        permissions=PermissionManager(settings),
        events=EventBus(),
        max_concurrent=kwargs.pop("max_concurrent", 2),
        max_result_chars=kwargs.pop("max_result_chars", 8000),
    )


def test_run_tool_executes_known_tool():
    executor = _make_executor()
    outcome = asyncio.run(executor.run_tool("fake.ok", {}))
    assert outcome.success is True
    assert outcome.executed is True
    assert outcome.status == "success"
    assert outcome.output == "ok output"
    assert outcome.cancelled is False


def test_run_tool_unknown_tool_reports_error():
    executor = _make_executor()
    outcome = asyncio.run(executor.run_tool("fake.unknown", {}))
    assert outcome.success is False
    assert outcome.status == "error"
    assert "Unknown tool" in outcome.reason


def test_run_tool_failing_tool_reports_failure():
    executor = _make_executor()
    outcome = asyncio.run(executor.run_tool("fake.fail", {}))
    assert outcome.success is False
    assert outcome.status == "error"
    assert outcome.error == "tool reported failure"


def test_run_tool_honours_preexisting_cancellation():
    executor = _make_executor()
    token = CancellationToken(task_id="task-1")
    token.request("user stop")
    outcome = asyncio.run(executor.run_tool("fake.ok", {}, cancellation_token=token))
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"


def test_sync_run_wrapper_works_without_running_loop():
    executor = _make_executor()
    # No running asyncio loop in a plain test, so ``run()`` takes the
    # ``asyncio.run`` path rather than the thread-pool path.
    outcome = executor.run("fake.ok", {})
    assert outcome.success is True
    assert outcome.output == "ok output"


def test_sync_run_wrapper_unknown_tool():
    executor = _make_executor()
    outcome = executor.run("fake.missing", {})
    assert outcome.success is False
    assert outcome.status == "error"


def test_bounded_concurrency_respects_semaphore():
    executor = _make_executor(max_concurrent=2)
    start = time.perf_counter()

    async def run_all():
        return await asyncio.gather(
            executor.run_tool("fake.slow", {}),
            executor.run_tool("fake.slow", {}),
            executor.run_tool("fake.slow", {}),
        )

    outcomes = asyncio.run(run_all())
    elapsed = time.perf_counter() - start
    assert all(o.success for o in outcomes)
    assert len(outcomes) == 3
    # 3 x 0.1s tools at max_concurrent=2 -> ~0.2s, well under sequential 0.3s.
    assert elapsed < 0.35, f"concurrency not bounded: {elapsed:.2f}s"


def test_timeout_is_injected_into_call_args():
    executor = _make_executor()
    asyncio.run(executor.run_tool("fake.capture", {}))
    kwargs = captured_tool_kwargs
    assert "timeout" in kwargs, f"timeout not injected: {kwargs!r}"
    assert kwargs["timeout"] == 60  # tool_generic_timeout default for unknown tools


def test_executor_has_bounded_semaphore():
    executor = _make_executor(max_concurrent=7)
    assert executor._max_concurrent == 7
    assert executor._semaphore._value == 7


def test_shutdown_does_not_raise():
    executor = _make_executor()
    executor.shutdown()


def test_task_provider_is_used_for_task_id():
    executor = _make_executor()
    provider_called = {}

    def provider():
        provider_called["yes"] = True
        return "task-42"

    executor.set_task_provider(provider)
    # run_tool reads the provider via _task_id; with no running loop it uses
    # asyncio.run, which still invokes the provider inside the coroutine.
    asyncio.run(executor.run_tool("fake.ok", {}))
    assert provider_called.get("yes") is True


def test_cancel_all_marks_task_tokens_cancelled():
    executor = _make_executor()
    token = CancellationToken(task_id="task-9")
    executor._task_cancellation_tokens["task-9"] = token
    assert token.is_cancelled() is False
    executor.cancel_all()
    assert token.is_cancelled() is True


def test_cancel_task_returns_false_for_unknown_task():
    executor = _make_executor()
    assert executor.cancel_task("no-such-task") is False
