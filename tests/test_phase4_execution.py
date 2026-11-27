from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.model_manager import build_backend
from ai.model_router import ModelRouter
from ai.ollama_provider import OllamaProvider
from ai.offline_provider import OfflineProvider
from ai.provider import AIProvider, AIProviderError, AIResponse, ModelInfo, ToolCall
from config.permissions import PermissionLevel, PermissionManager
from core.agent import Agent
from core.events import EventBus
from core.executor import Executor
from core.task_manager import StepStatus, TaskManager, TaskState
from core.task_store import TaskRecord, TaskStore
from core.verification import VerificationState
from coding.change_tracker import ChangeTracker
from memory.preferences import PreferencesStore
from memory.rules_store import RuleStore
from tools import build_registry
from tools.patches import patch_file
from tools.registry import ToolRegistry, ToolResult, ToolSpec


class ScriptedProvider(AIProvider):
    name = "scripted"
    model_label = "scripted/local"
    available = True

    def __init__(self, responses, models=None):
        self.responses = list(responses)
        self.models = list(models or [])
        self.models_used = []

    def is_available(self):
        return True

    def list_models(self):
        return self.models

    def chat(self, messages, tools=None, model=None):
        self.models_used.append(model)
        if not self.responses:
            return AIResponse(text="done", model=model)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(messages, model)
        return response


class _Token:
    def __init__(self):
        self.cancelled = False

    def is_cancelled(self):
        return self.cancelled


def _response(*calls, text=""):
    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    return ToolCall(name=name, arguments=args)


def _spec(name, fn, level=PermissionLevel.SAFE, required=None):
    properties = {key: {} for key in (required or [])}
    return ToolSpec(
        name=name,
        description=name,
        fn=fn,
        level=level,
        parameters={"type": "object", "properties": properties, "required": list(required or [])},
        required=list(required or []),
    )


def _registry(*specs):
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _agent(provider, settings, registry=None, project=None, confirmer=None, bus=None, model_name=""):
    settings.sessions_dir = settings.data_dir / "phase4-sessions"
    settings.memory_dir.mkdir(parents=True, exist_ok=True)
    preferences = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, preferences)
    return Agent(
        provider=provider,
        settings=settings,
        registry=registry or build_registry(settings),
        permissions=permissions,
        events=bus or EventBus(),
        confirmer=confirmer,
        project=project,
        memory_preferences=preferences,
        model_name=model_name,
    )


def test_plan_validation_is_atomic_before_tool_execution(tmp_settings):
    calls = []
    first = _spec("project.first", lambda **kwargs: calls.append(kwargs) or ToolResult(True), required=["value"])
    first.parameters["properties"]["value"] = {"type": "integer"}
    second = _spec("project.second", lambda **kwargs: calls.append(kwargs) or ToolResult(True), required=["value"])
    second.parameters["properties"]["value"] = {"type": "array"}
    registry = _registry(first, second)
    provider = ScriptedProvider([
        _response(_call("project.first", value=1), _call("project.second", value="not-an-array")),
    ])
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("perform ordered custom steps")
    record = agent._task_store.get(agent._tasks.task_id)
    assert "invalid execution plan" in reply.lower()
    assert calls == []
    assert record.status == "failed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.FAILED.value


def test_task_state_machine_and_verification_contract():
    tasks = TaskManager()
    tasks.begin_task("state test")
    assert tasks.state == TaskState.CREATED
    tasks.transition(TaskState.PLANNING)
    tasks.transition(TaskState.EXECUTING)
    tasks.set_verification(VerificationState.PASSED, "evidence")
    tasks.transition(TaskState.VERIFYING)
    tasks.transition(TaskState.COMPLETED)
    assert tasks.task_succeeded() is True
    with pytest.raises(ValueError):
        tasks.transition(TaskState.EXECUTING)


def test_multi_step_lifecycle_emits_permission_and_structured_events(tmp_settings):
    order = []
    registry = _registry(
        _spec("project.first", lambda **kwargs: order.append("first") or ToolResult(True, output="A"), required=["value"]),
        _spec("project.second", lambda **kwargs: order.append("second") or ToolResult(True, output="B"), level=PermissionLevel.SENSITIVE),
    )
    bus = EventBus()
    events = []
    for kind in ("task_started", "planning", "permission_required", "tool_started", "tool_completed", "verification_started", "verification_completed", "task_completed", "task_done"):
        bus.subscribe(kind, lambda payload, kind=kind: events.append((kind, payload)))
    provider = ScriptedProvider([
        _response(_call("project.first", value=1), _call("project.second")),
        _response(text="complete"),
    ])
    agent = _agent(provider, tmp_settings, registry=registry, bus=bus, confirmer=lambda tool, description: "allow")
    reply = agent.respond("run two ordered project steps")
    record = agent._task_store.get(agent._tasks.task_id)
    assert order == ["first", "second"]
    assert "complete" in reply
    assert record.status == "completed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.UNVERIFIED.value
    assert any(kind == "permission_required" and payload["tool"] == "project.second" for kind, payload in events)
    assert any(kind == "task_done" and payload["status"] == "completed" for kind, payload in events)


def test_partial_failure_can_recover_and_preserves_history(tmp_settings):
    second_calls = []
    project = tmp_settings.workspace_root / "project"
    project.mkdir()

    def second(**kwargs):
        second_calls.append(1)
        if len(second_calls) == 1:
            return ToolResult(False, error="temporary failure")
        return ToolResult(True, output="recovered")

    registry = _registry(
        _spec("project.first", lambda **kwargs: ToolResult(True, output="A"), required=["value"]),
        _spec("project.second", second),
    )
    provider = ScriptedProvider([
        _response(_call("project.first", value=1), _call("project.second")),
        _response(_call("project.second")),
        _response(text="recovered task"),
    ])
    agent = _agent(provider, tmp_settings, registry=registry, project=SimpleNamespace(name="project", path=str(project)))
    reply = agent.respond("perform a recoverable multi-step task")
    record = agent._task_store.inspect_task(agent._tasks.task_id)
    assert "recovered task" in reply
    assert record["status"] == "completed"
    assert record["metadata"]["lifecycle"]["recovery_attempts"] == 1
    assert record["metadata"]["lifecycle"]["partial"] is True
    assert [step["success"] for step in record["change_steps"]] == [True, False, True]
    assert second_calls == [1, 1]


def test_repeated_tool_failure_is_bounded(tmp_settings):
    calls = []
    registry = _registry(_spec("project.fail", lambda **kwargs: calls.append(1) or ToolResult(False, error="ValueError: broken")))
    provider = ScriptedProvider([
        _response(_call("project.fail")),
        _response(_call("project.fail")),
        _response(_call("project.fail")),
        _response(_call("project.fail")),
    ])
    tmp_settings.max_recovery_attempts = 2
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("execute the custom operation repeatedly")
    record = agent._task_store.get(agent._tasks.task_id)
    assert len(calls) == 3
    assert "bounded recovery" in reply
    assert record.status == "failed"


def test_concrete_verification_failure_controls_task_status(tmp_settings):
    registry = build_registry(tmp_settings)
    registry.get("python.run_script").fn = lambda **kwargs: ToolResult(True, output="WRONG OUTPUT", exit_code=0)
    provider = ScriptedProvider([])
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("Create a Python file that prints Hello World")
    record = agent._task_store.get(agent._tasks.task_id)
    assert "could not be verified" in reply.lower()
    assert record.status == "failed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.FAILED.value


def test_cancellation_after_side_effect_stops_remaining_steps(tmp_settings):
    token = _Token()
    calls = []
    registry = _registry(
        _spec("project.first", lambda **kwargs: token.__setattr__("cancelled", True) or calls.append("first") or ToolResult(True)),
        _spec("project.second", lambda **kwargs: calls.append("second") or ToolResult(True)),
    )
    provider = ScriptedProvider([_response(_call("project.first"), _call("project.second"))])
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("cancel after the first operation", token)
    record = agent._task_store.get(agent._tasks.task_id)
    assert calls == ["first"]
    assert "cancel" in reply.lower()
    assert record.status == "cancelled"
    assert record.metadata["lifecycle"]["partial"] is True


def test_pre_cancelled_request_persists_cancelled_task(tmp_settings):
    token = _Token()
    token.cancelled = True
    provider = ScriptedProvider([_response(text="must not run")])
    agent = _agent(provider, tmp_settings, registry=_registry())
    reply = agent.respond("cancel immediately", token)
    record = agent._task_store.get(agent._tasks.task_id)
    assert provider.models_used == []
    assert "cancel" in reply.lower()
    assert record.status == "cancelled"
    assert record.metadata["lifecycle"]["state"] == "cancelled"


def test_completed_task_reloads_with_git_snapshot_and_change_history(tmp_settings, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "a@b.c"), ("config", "user.name", "T")):
        import subprocess
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo), check=True, capture_output=True)
    provider = ScriptedProvider([
        _response(_call("terminal.execute", command="git status", working_dir=str(repo))),
        _response(text="status checked"),
    ])
    agent = _agent(provider, tmp_settings, registry=build_registry(tmp_settings), project=SimpleNamespace(name="repo", path=str(repo)))
    agent.respond("inspect the repository")
    task_id = agent._tasks.task_id
    reloaded = TaskStore(tmp_settings.sessions_dir).inspect_task(task_id)
    assert reloaded["status"] == "completed"
    assert reloaded["goal"] == "inspect the repository"
    assert reloaded["git"]["starting_commit"]
    assert reloaded["git"]["ending_commit"] == reloaded["git"]["starting_commit"]
    assert reloaded["snapshots"]
    assert reloaded["change_steps"][0]["tool"] == "terminal.execute"
    assert reloaded["outcome"] == "status checked"


def test_interrupted_task_is_reconciled_without_replay(tmp_settings, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sessions = tmp_settings.data_dir / "phase4-sessions"
    tracker = ChangeTracker(sessions)
    session = tracker.start("interrupted goal", str(root), session_id="interrupted-task")
    store = TaskStore(sessions)
    store.create(TaskRecord(
        task_id=session.id,
        project_name="project",
        project_root=str(root),
        title="Interrupted",
        goal=session.goal,
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    ))
    provider = ScriptedProvider([_response(text="new request complete")])
    agent = _agent(provider, tmp_settings, registry=_registry(), project=SimpleNamespace(name="project", path=str(root)))
    old = agent._task_store.get("interrupted-task")
    old_session = ChangeTracker.load_task(sessions, "interrupted-task")
    assert old.status == "failed"
    assert old.metadata["lifecycle"]["recovered"] is True
    assert old_session.status == "failed"
    agent.respond("please complete a new request")
    assert len(provider.models_used) == 1
    assert agent._task_store.inspect_task(agent._tasks.task_id)["status"] == "completed"


def test_provider_error_after_tool_side_effect_fails_task(tmp_settings):
    calls = []
    registry = _registry(_spec("project.side", lambda **kwargs: calls.append(1) or ToolResult(True)))
    provider = ScriptedProvider([
        _response(_call("project.side")),
        RuntimeError("backend disappeared"),
    ])
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("perform a side effect then fail")
    record = agent._task_store.inspect_task(agent._tasks.task_id)
    assert calls == [1]
    assert "unavailable" in reply
    assert record["status"] == "failed"
    assert record["change_steps"][0]["success"] is True


def test_all_local_models_failing_still_uses_offline_fallback(tmp_settings):
    class FailingProvider(ScriptedProvider):
        def chat(self, messages, tools=None, model=None):
            self.models_used.append(model)
            raise RuntimeError(f"{model} unavailable")

    provider = FailingProvider([], [
        ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",)),
        ModelInfo("qwen3:14b", parameter_size="14B", capabilities=("tools",)),
    ])
    agent = _agent(provider, tmp_settings, registry=_registry())
    reply = agent.respond("explain a general topic")
    assert "limited offline mode" in reply.lower()
    assert provider.models_used == ["qwen3:8b", "qwen3:14b"]


def test_debug_uses_model_fallback_path(tmp_settings):
    class DebugFallbackProvider(ScriptedProvider):
        def chat(self, messages, tools=None, model=None):
            self.models_used.append(model)
            if model == "qwen3:14b":
                raise RuntimeError("strong unavailable")
            return _response(text="fallback debug complete")

    provider = DebugFallbackProvider([], [
        ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",)),
        ModelInfo("qwen3:14b", parameter_size="14B", capabilities=("tools",)),
    ])
    agent = _agent(provider, tmp_settings, registry=_registry())
    reply = agent.respond("debug architecture concurrency framework recursion security race")
    assert "AI backend error" not in reply
    assert provider.models_used == ["qwen3:14b", "qwen3:8b"]


def test_tracker_persistence_failure_also_fails_task_store(tmp_settings, monkeypatch):
    registry = _registry(_spec("project.side", lambda **kwargs: ToolResult(True)))
    provider = ScriptedProvider([_response(_call("project.side")), _response(text="done")])
    agent = _agent(provider, tmp_settings, registry=registry)
    monkeypatch.setattr(agent._change_tracker, "finish", lambda *args, **kwargs: None)
    agent.respond("perform a tracked operation")
    record = agent._task_store.inspect_task(agent._tasks.task_id)
    assert record["status"] == "failed"
    assert record["metadata"]["lifecycle"]["verification"] == VerificationState.FAILED.value


def test_router_preserves_installed_model_names_with_underscores(tmp_settings):
    tmp_settings.models_fast = "custom_model:8b"
    router = ModelRouter(tmp_settings, catalog=[ModelInfo("custom_model:8b", parameter_size="8B", capabilities=("tools",))])
    assert router.is_available("custom_model:8b")
    assert router.choose().name == "custom_model:8b"
    assert router.choose(task_override="custom_model:8b").name == "custom_model:8b"
    tmp_settings.models_mode = "project"
    assert router.choose(project_pref="custom_model:8b").name == "custom_model:8b"


def test_model_router_uses_local_fallback_and_honors_explicit_pin(tmp_settings):
    models = [
        ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",)),
        ModelInfo("qwen3:14b", parameter_size="14B", capabilities=("tools",)),
    ]
    provider = ScriptedProvider([_response(text="fast done"), _response(text="strong done")], models)
    agent = _agent(provider, tmp_settings, registry=_registry())
    agent.respond("short status request")
    agent.respond("architecture concurrency framework recursion dependency security design")
    assert provider.models_used == ["qwen3:8b", "qwen3:14b"]

    pinned = _agent(provider, tmp_settings, registry=_registry(), model_name="qwen3:8b")
    pinned.respond("architecture concurrency framework recursion dependency security design")
    assert provider.models_used[-1] == "qwen3:8b"


def test_router_does_not_escalate_to_unavailable_model(tmp_settings):
    tmp_settings.models_strong = "nemotron:12b"
    catalog = [ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",))]
    router = ModelRouter(tmp_settings, catalog=catalog)
    choice = router.choose(last_verify_failed=True)
    assert choice.name == "qwen3:8b"
    assert choice.source == "default"

    catalog.append(ModelInfo("nemotron:12b", parameter_size="12B", capabilities=("tools",)))
    router = ModelRouter(tmp_settings, catalog=catalog)
    assert router.choose(last_verify_failed=True).name == "nemotron:12b"


def test_provider_failure_falls_back_to_another_local_model(tmp_settings):
    class FallbackProvider(ScriptedProvider):
        def chat(self, messages, tools=None, model=None):
            self.models_used.append(model)
            if model == "qwen3:14b":
                raise RuntimeError("strong model unavailable")
            return _response(text="fallback completed")

    provider = FallbackProvider([], [
        ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",)),
        ModelInfo("qwen3:14b", parameter_size="14B", capabilities=("tools",)),
    ])
    agent = _agent(provider, tmp_settings, registry=_registry())
    reply = agent.respond("architecture concurrency framework recursion dependency security design")
    assert "fallback completed" in reply
    assert provider.models_used == ["qwen3:14b", "qwen3:8b"]


def test_unknown_confirmation_fails_closed(tmp_settings):
    calls = []
    registry = _registry(_spec("project.sensitive", lambda **kwargs: calls.append(1) or ToolResult(True), level=PermissionLevel.SENSITIVE))
    outcome = Executor(registry, PermissionManager(tmp_settings), EventBus(), lambda tool, description: "maybe").run("project.sensitive", {})
    assert calls == []
    assert outcome.executed is False
    assert outcome.cancelled is True


def test_restrict_only_rule_beats_user_allow(tmp_settings):
    calls = []
    registry = _registry(_spec("project.unsafe", lambda **kwargs: calls.append(1) or ToolResult(True)))
    rule_store = RuleStore(tmp_settings.memory_dir / "rules.json")
    rule_store.create("block unsafe", action="block", if_content="forbidden", scope="global", priority=100)
    provider = ScriptedProvider([_response(_call("project.unsafe", value="forbidden"))])
    agent = _agent(provider, tmp_settings, registry=registry)
    agent.preferences.set_permission("project.unsafe", "allow")
    reply = agent.respond("attempt forbidden operation")
    assert calls == []
    assert "blocked" in reply.lower()
    assert agent._tasks.steps[0].status == StepStatus.BLOCKED


def test_debug_verification_failure_gets_bounded_recovery(tmp_settings):
    class LLM:
        def __init__(self):
            self.count = 0

        def __call__(self, messages, tools=None, model=None):
            self.count += 1
            if self.count in (1, 3):
                return _response(_call("code.patch_file", patch="patch"))
            return _response(text="model stopped")

    class ExecutorStub:
        def __init__(self):
            self.verifications = 0

        def run(self, name, args, *extra):
            if name == "code.patch_file":
                return ToolResult(True, output="patched")
            self.verifications += 1
            return ToolResult(self.verifications > 1, output="passed" if self.verifications > 1 else "FAILED")

    from core.debug_loop import DebugLoop
    llm = LLM()
    executor = ExecutorStub()
    report = DebugLoop(llm, executor, tmp_settings, auto_verify="pytest -q", verify_dir=str(tmp_settings.workspace_root)).diagnose(
        "fix the failing test", {"goal": "fix", "project": "", "files": ""}
    )
    assert report.verified is True
    assert report.verification_state == "passed"
    assert executor.verifications == 2
    assert llm.count >= 4


def test_debug_actual_tool_calls_are_bounded(tmp_settings):
    class LLM:
        def __call__(self, messages, tools=None, model=None):
            return _response(_call("project.read", value=1), _call("project.read", value=2), _call("project.read", value=3))

    class ExecutorStub:
        def __init__(self):
            self.calls = 0

        def run(self, name, args, *extra):
            self.calls += 1
            return ToolResult(True)

    from core.debug_loop import DebugLoop
    tmp_settings.max_tool_calls = 2
    executor = ExecutorStub()
    report = DebugLoop(LLM(), executor, tmp_settings).diagnose("fix", {"goal": "fix"})
    assert executor.calls == 2
    assert "limit" in report.exhausted


def test_patch_without_workspace_tracks_actual_application_root(tmp_settings):
    agent = _agent(ScriptedProvider([]), tmp_settings, registry=_registry())
    root = agent._project_root_from_args({}, "code.patch_file")
    assert Path(root) == tmp_settings.base_dir.resolve()


def test_fast_path_denial_and_tool_failure_are_not_completed(tmp_settings):
    denied = _agent(
        ScriptedProvider([]),
        tmp_settings,
        registry=build_registry(tmp_settings),
        confirmer=lambda tool, description: "deny",
    )
    denied.respond("open custom-app")
    denied_record = denied._task_store.inspect_task(denied._tasks.task_id)
    assert denied_record["status"] == "cancelled"

    registry = _registry(_spec("system.info", lambda **kwargs: ToolResult(False, error="query failed")))
    failed = _agent(ScriptedProvider([]), tmp_settings, registry=registry)
    failed.respond("show system info")
    failed_record = failed._task_store.inspect_task(failed._tasks.task_id)
    assert failed_record["status"] == "failed"
    assert failed._tasks.steps[0].status == StepStatus.FAILED


def test_debug_cancellation_persists_cancelled_status(tmp_settings):
    token = _Token()
    registry = _registry(_spec(
        "project.info",
        lambda **kwargs: token.__setattr__("cancelled", True) or ToolResult(True, output="info"),
    ))
    provider = ScriptedProvider([
        _response(_call("project.info", path=str(tmp_settings.workspace_root))),
        _response(text="should not finish"),
    ])
    agent = _agent(provider, tmp_settings, registry=registry)
    reply = agent.respond("debug this operation", token)
    record = agent._task_store.inspect_task(agent._tasks.task_id)
    assert "cancel" in reply.lower()
    assert record["status"] == "cancelled"
    assert record["metadata"]["lifecycle"]["verification"] == VerificationState.UNVERIFIED.value


def test_restart_reconciles_pending_and_cross_file_mismatch(tmp_settings):
    sessions = tmp_settings.data_dir / "phase4-sessions"
    store = TaskStore(sessions)
    store.create(TaskRecord(
        task_id="pending-task",
        project_name="project",
        project_root=str(tmp_settings.workspace_root),
        title="Pending",
        goal="Pending goal",
        created_at="20240101-120000",
        status="pending",
    ))
    tracker = ChangeTracker(sessions)
    session = tracker.start("finished goal", str(tmp_settings.workspace_root), session_id="terminal-task")
    store.create(TaskRecord(
        task_id=session.id,
        project_name="project",
        project_root=str(tmp_settings.workspace_root),
        title="Terminal",
        goal=session.goal,
        created_at=session.started_at,
        status="completed",
        finished_at="20240101-120100",
        outcome="done",
    ))
    _agent(ScriptedProvider([]), tmp_settings, registry=_registry())
    assert store.get("pending-task").status == "failed"
    reconciled = ChangeTracker.load_task(sessions, "terminal-task")
    assert reconciled.status == "completed"
    assert reconciled.final_result == "done"


def test_patch_tool_rejects_paths_outside_workspace(tmp_settings, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    patch = f"=== {outside.as_posix()} ===\nreplace value = 1\n+value = 2\n"
    result = patch_file(patch, dry_run=False, workspace=str(workspace), settings=tmp_settings)
    assert result.success is False
    assert "escapes workspace" in result.error
    assert outside.read_text(encoding="utf-8") == "value = 1\n"


def test_router_state_resets_between_requests(tmp_settings):
    models = [
        ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",)),
        ModelInfo("qwen3:14b", parameter_size="14B", capabilities=("tools",)),
    ]
    registry = _registry(_spec("project.fail", lambda **kwargs: ToolResult(False, error="ValueError: failed")))
    provider = ScriptedProvider([
        _response(_call("project.fail")),
        _response(text="second request complete"),
    ], models)
    tmp_settings.max_recovery_attempts = 0
    agent = _agent(provider, tmp_settings, registry=registry)
    agent.respond("execute the custom operation")
    agent.respond("give a short status update")
    assert provider.models_used == ["qwen3:8b", "qwen3:8b"]


def test_model_manager_rejects_non_tool_models_and_remote_hosts(tmp_settings, monkeypatch):
    class Provider:
        def __init__(self, host):
            self.host = host

        def is_available(self):
            return True

        def list_models(self):
            return [ModelInfo("embedding-only", capabilities=("embedding",))]

    monkeypatch.setattr("ai.model_manager.OllamaProvider", Provider)
    result = build_backend(tmp_settings)
    assert isinstance(result.provider, OfflineProvider)
    assert "tool-capable" in result.note

    tmp_settings.ollama_host = "https://remote.example.com"
    remote = build_backend(tmp_settings)
    assert isinstance(remote.provider, OfflineProvider)
    assert "Remote Ollama" in remote.note


def test_persistence_failure_does_not_emit_success(tmp_settings, monkeypatch):
    bus = EventBus()
    terminal = []
    bus.subscribe("task_done", terminal.append)
    agent = _agent(ScriptedProvider([_response(text="done")]), tmp_settings, registry=_registry(), bus=bus)
    monkeypatch.setattr(agent._task_store, "finish", lambda *args, **kwargs: None)
    agent.respond("complete without persistence")
    assert terminal[-1]["status"] == "failed"
    assert terminal[-1]["verification"] == VerificationState.FAILED.value


def test_ollama_metadata_and_malformed_tool_arguments_fail_safely(monkeypatch):
    payload = {
        "models": [{
            "name": "qwen3:8b",
            "size": 123,
            "digest": "abc",
            "capabilities": ["tools"],
            "details": {"parameter_size": "8.2B", "family": "qwen3", "context_length": 40960},
        }]
    }
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr("ai.ollama_provider.httpx.get", lambda *args, **kwargs: response)
    provider = OllamaProvider()
    model = provider.list_models()[0]
    assert model.context_length == 40960
    assert model.capabilities == ("tools",)
    provider._post = lambda request: {"message": {"tool_calls": [{"function": {"name": "x", "arguments": "not-json"}}]}}
    with pytest.raises(AIProviderError):
        provider.chat([{"role": "user", "content": "x"}], model="qwen3:8b")


def test_model_manager_preserves_missing_model_fallback(tmp_settings, monkeypatch):
    class Provider:
        name = "ollama"
        model_label = "ollama"
        reason = ""

        def __init__(self, host):
            self.host = host

        def is_available(self):
            return True

        def list_models(self):
            return [ModelInfo("qwen3:8b", parameter_size="8B", capabilities=("tools",))]

    monkeypatch.setattr("ai.model_manager.OllamaProvider", Provider)
    tmp_settings.ai_model = "nemotron:12b"
    tmp_settings.preferred_models = ["qwen3:8b"]
    backend = build_backend(tmp_settings)
    assert backend.model_name == "qwen3:8b"
    assert "nemotron:12b" in backend.note
    assert "not installed" in backend.note


def test_event_bus_ignores_malformed_events_and_listener_failures():
    bus = EventBus()
    received = []
    bus.subscribe("x", lambda payload: (_ for _ in ()).throw(RuntimeError("listener")))
    bus.subscribe("x", received.append)
    bus.emit("x", None)
    bus.emit("", {"bad": True})
    bus.emit("x", {"value": 1})
    assert received == [{"value": 1}]


def test_task_store_exposes_lifecycle_metadata_without_duplicate_history(tmp_settings):
    record = TaskRecord(
        task_id="metadata-task",
        project_name="project",
        project_root=str(tmp_settings.workspace_root),
        title="Metadata",
        goal="Persist lifecycle",
        created_at="20240101-120000",
        metadata={"lifecycle": {"state": "completed", "verification": "passed"}},
    )
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(record)
    loaded = TaskStore(tmp_settings.sessions_dir).inspect_task("metadata-task")
    assert loaded["metadata"]["lifecycle"]["verification"] == "passed"
    assert "metadata" not in store.query_tasks(limit=1)[0]
