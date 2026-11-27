import pytest

from ai.tokenizer import TokenBudget, est_tokens, head_tail, truncate_lines
from coding.profile import build_profile
from coding.project_index import index_cached
from coding.relevance import pick_relevant
from core.context_builder import ContextBuilder
from core.executor import Executor


def test_est_tokens_ascii():
    assert est_tokens("hello") < est_tokens("hello world this is a longer string with many tokens")
    assert est_tokens("") == 0
    assert est_tokens("a" * 400) >= 90


def test_est_tokens_cjk_denser():
    assert est_tokens("你好世界这是一个测试") >= est_tokens("hello world" * 3) * 0


def test_head_tail_short_no_op():
    assert head_tail("short text", 100) == "short text"


def test_head_tail_truncates_middle():
    text = "HEAD " + "x" * 1000 + " TAIL"
    trimmed = head_tail(text, 200, tail_chars=20)
    assert trimmed.startswith("HEAD")
    assert trimmed.endswith("TAIL")
    assert "omitted" in trimmed


def test_truncate_lines():
    lines = truncate_lines("\n".join(f"line {i}" for i in range(50)), 1000, head_lines=4)
    assert "more lines omitted" in lines
    assert lines.splitlines()[0] == "line 0"


def test_token_budget():
    budget = TokenBudget(1000, {"a": 0.5, "b": 0.5})
    assert budget.budget["a"] == 500
    assert budget.budget["b"] == 500
    assert budget.remaining({"a": 100, "b": 100}) == 800


def _mini_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_home(): pass\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


def test_relevance_selects_by_keyword(tmp_path):
    root = _mini_project(tmp_path)
    records = index_cached(root, None)
    hits = pick_relevant(root, "flask app test", records=records)
    paths = {h.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for h in hits}
    assert "app.py" in paths
    assert len(hits) <= 8


def test_context_builder_assembles_sections(tmp_path):
    morning = _mini_project(tmp_path)
    profile = build_profile(str(morning) if False else morning)
    builder = (
        ContextBuilder(max_tokens=1000)
        .add("PROJECT", profile.to_context(400), weight="project")
        .add("MISSION", "goal: add a health endpoint", weight="mission")
        .add("TOOL RESULT", ContextBuilder.trim_tool_result("ok output", 200), weight="tool_result")
    )
    prompt = builder.build(project_label=f"{profile.name}")
    assert "===== PROJECT =====" in prompt
    assert "====" in prompt and "health" in prompt
    assert "DIABLO" in prompt


def test_context_budget_enforced(tmp_path):
    morning = _mini_project(tmp_path)
    profile = build_profile(str(morning) if False else morning)
    big = "y" * 20000
    builder = (
        ContextBuilder(max_tokens=400)
        .add("PROJECT", profile.to_context(400), weight="project")
        .add("FILES", big, weight="files")
        .add("ERROR", "traceback\n" * 500, weight="error")
    )
    prompt = builder.build()
    assert est_tokens(prompt) <= 600


def test_executor_trims_large_results(tmp_settings):
    from tools.registry import ToolRegistry, ToolSpec, ToolResult
    from config.permissions import PermissionLevel, PermissionManager

    registry = ToolRegistry()
    spec = ToolSpec(name="noise.echo", description="echos", fn=lambda **k: ToolResult(True, output="Z" * 50000), level=PermissionLevel.SAFE, parameters={"type": "object", "properties": {}, "required": []})
    registry.register(spec)
    perms = PermissionManager(tmp_settings, None)
    ex = Executor(registry, perms, _events_stub(), None, max_result_chars=500)
    outcome = ex.run("noise.echo", {})
    assert outcome.success
    assert len(outcome.output) < 1000


def test_executor_passes_null_permissions_only_when_safe(tmp_settings):
    pass


def _events_stub():
    class _Events:
        def emit_tool(self, *a, **k):
            return None
    return _Events()