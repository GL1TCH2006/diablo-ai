import pytest

from config.permissions import PermissionLevel
from tools import build_registry
from tools.registry import ToolResult


@pytest.fixture
def registry(tmp_settings):
    return build_registry(tmp_settings)


def test_core_tools_registered(registry):
    names = registry.names()
    for expected in (
        "filesystem.read_file",
        "filesystem.write_file",
        "filesystem.delete_file",
        "filesystem.list_dir",
        "terminal.execute",
        "python.run_script",
        "applications.open",
        "system.info",
    ):
        assert expected in names


def test_unknown_tool_returns_none(registry):
    assert registry.get("no_such_tool") is None


def test_tool_describe_custom(registry):
    spec = registry.get("terminal.execute")
    text = spec.describe_args({"command": "dir"})
    assert "dir" in text


def test_tool_describe_default(registry):
    from tools.registry import ToolSpec

    spec = ToolSpec(name="x.fn", description="d", fn=lambda **kw: ToolResult(True))
    assert "x.fn" in spec.describe_args({"a": 1})


def test_permission_levels_present(registry):
    assert registry.get("system.info").level == PermissionLevel.SAFE
    assert registry.get("filesystem.delete_file").level == PermissionLevel.SENSITIVE
    assert registry.get("applications.open").level == PermissionLevel.NORMAL