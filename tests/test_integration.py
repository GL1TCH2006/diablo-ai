"""End-to-end test: USER REQUEST -> AGENT -> TOOL -> EXECUTION -> RESULT -> VERIFIED."""

from ai.offline_provider import OfflineProvider
from ai.provider import AIProvider, AIResponse, ToolCall
from config.permissions import PermissionManager
from core.agent import Agent
from core.events import EventBus
from memory.conversation import ConversationStore
from memory.preferences import PreferencesStore
from tools import build_registry


class StubProvider(AIProvider):
    name = "stub"
    model_label = "stub/test"
    available = True

    def __init__(self, script_path: str):
        self._script = script_path
        self.turns = 0

    def chat(self, messages, tools=None, model=None):
        self.turns += 1
        if messages and messages[-1].get("role") == "tool":
            if "[python.run_script]" in messages[-1].get("content", ""):
                return AIResponse(text="Script executed and verified.", model="stub")
        return AIResponse(
            text="",
            tool_calls=[ToolCall(name="python.run_script", arguments={"script": self._script})],
            model="stub",
        )

    def is_available(self):
        return True


def _agent_with(provider, tmp_settings, confirmer=None):
    bus = EventBus()
    registry = build_registry(tmp_settings)
    conversation = ConversationStore(tmp_settings.memory_dir)
    preferences = PreferencesStore(tmp_settings.memory_dir)
    permissions = PermissionManager(tmp_settings, preferences)
    return Agent(
        provider=provider,
        settings=tmp_settings,
        registry=registry,
        permissions=permissions,
        events=bus,
        confirmer=confirmer,
        memory_conversation=conversation,
        memory_preferences=preferences,
    )


def test_greeting_through_agent(tmp_settings):
    agent = _agent_with(OfflineProvider(scratch_dir=tmp_settings.scratch_dir), tmp_settings)
    reply = agent.respond("Diablo, hello")
    assert "Diablo is online" in reply


def test_hello_world_full_workflow(tmp_settings):
    agent = _agent_with(OfflineProvider(scratch_dir=tmp_settings.scratch_dir), tmp_settings)
    reply = agent.respond("Create a Python file that prints Hello World")

    target = tmp_settings.scratch_dir / "hello_world.py"
    assert target.exists()
    assert 'print("Hello World")' in target.read_text(encoding="utf-8")
    assert "verified" in reply.lower()
    assert "Hello World" in reply


def test_llm_loop_with_tool_call_round_trip(tmp_settings):
    script = tmp_settings.scratch_dir / "helper.py"
    script.write_text('print("OK")\n', encoding="utf-8")
    provider = StubProvider(str(script))
    agent = _agent_with(provider, tmp_settings)
    reply = agent.respond("worker, run my helper script please")
    assert provider.turns >= 2
    assert "verified" in reply.lower()


def test_conversation_is_persisted(tmp_settings):
    agent = _agent_with(OfflineProvider(scratch_dir=tmp_settings.scratch_dir), tmp_settings)
    agent.respond("Diablo, hello")
    entries = ConversationStore(tmp_settings.memory_dir).recent(5)
    assert any(e.get("role") == "user" for e in entries)
    assert any(e.get("role") == "assistant" for e in entries)


def test_level2_deny_flow(tmp_settings):
    from core.executor import Executor

    script = tmp_settings.scratch_dir / "demo.txt"
    script.write_text("temp", encoding="utf-8")

    registry = build_registry(tmp_settings)
    permissions = PermissionManager(tmp_settings, PreferencesStore(tmp_settings.memory_dir))
    calls: list[tuple[str, str]] = []

    def confirmer(tool, description):
        calls.append((tool, description))
        return "deny"

    outcome = Executor(registry, permissions, EventBus(), confirmer).run(
        "filesystem.delete_file", {"path": str(script)}
    )
    assert outcome.allowed is False
    assert outcome.executed is False
    assert outcome.reason == "Denied by user."
    assert len(calls) == 1 and calls[0][0] == "filesystem.delete_file"
    assert script.exists()


def test_level2_allow_flow_before_delete(tmp_settings):
    from core.executor import Executor

    script = tmp_settings.scratch_dir / "demo2.txt"
    script.write_text("temp", encoding="utf-8")

    registry = build_registry(tmp_settings)
    permissions = PermissionManager(tmp_settings, PreferencesStore(tmp_settings.memory_dir))
    outcome = Executor(registry, permissions, EventBus(), lambda tool, desc: "allow").run(
        "filesystem.delete_file", {"path": str(script)}
    )
    assert outcome.executed is True and outcome.success is True
    assert not script.exists()