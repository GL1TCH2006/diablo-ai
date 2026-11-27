import pytest

from core.agent import _as_dict, _to_api_calls
from ai.provider import ToolCall


def test_to_api_calls_echoes_object_arguments():
    calls = [ToolCall(id="c0", name="applications.open", arguments={"app": "Calculator"})]
    api = _to_api_calls(calls)
    assert api == [{"function": {"name": "applications.open", "arguments": {"app": "Calculator"}}}]


def test_to_api_calls_accepts_string_arguments():
    calls = [ToolCall(id="c0", name="terminal.execute", arguments='{"command": "git status"}')]
    api = _to_api_calls(calls)
    assert api[0]["function"]["arguments"] == {"command": "git status"}


def test_to_api_calls_none_when_empty():
    assert _to_api_calls([]) is None


def test_as_dict_normalises():
    assert _as_dict({"a": 1}) == {"a": 1}
    assert _as_dict('{"a": 1}') == {"a": 1}
    assert _as_dict("not json") == {}
    assert _as_dict(None) == {}