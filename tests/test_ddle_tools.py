"""Tests for DDLE tools integration."""

from __future__ import annotations

from tools.ddle import start_task, task_status, list_tasks


def test_ddle_tools_import():
    """DDLE tools can be imported successfully."""
    assert start_task is not None
    assert task_status is not None
    assert list_tasks is not None


def test_list_tasks_returns_list():
    """list_tasks() returns a ToolResult (not an exception)."""
    result = list_tasks()
    assert result is not None
    assert hasattr(result, "success")
    assert hasattr(result, "output")
    assert hasattr(result, "error")


def test_task_status_missing():
    """task_status('nonexistent') returns an error ToolResult, does not raise."""
    result = task_status("nonexistent")
    assert result is not None
    assert result.success is False
    assert "not found" in result.error.lower() or "not found" in result.output.lower()


def test_start_task_empty_description():
    """start_task with empty description returns an error ToolResult."""
    result = start_task("")
    assert result is not None
    assert result.success is False
