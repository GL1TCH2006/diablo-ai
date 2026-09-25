from ai.offline_provider import OfflineProvider


def test_greeting_offline(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "user", "content": "hello"}])
    assert "Diablo is online" in response.text


def test_create_hello_emits_tool_calls(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "user", "content": "create a python file that prints hello world"}])
    assert len(response.tool_calls) == 2
    assert response.tool_calls[0].name == "filesystem.write_file"
    assert response.tool_calls[1].name == "python.run_script"


def test_open_emits_tool_call(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "user", "content": "open calculator"}])
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "applications.open"
    assert response.tool_calls[0].arguments.get("app") == "calculator"


def test_sysinfo_emits_tool_call(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "user", "content": "what is my cpu usage"}])
    assert response.tool_calls and response.tool_calls[0].name == "system.info"


def test_tool_result_reacts(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "tool", "content": "[python.run_script] success Hello World"}])
    assert "verified" in response.text


def test_free_chat_offline_is_honest(tmp_path):
    provider = OfflineProvider(scratch_dir=tmp_path / "workspace")
    response = provider.chat([{"role": "user", "content": "explain quantum computing in detail"}])
    assert "limited offline mode" in response.text.lower()