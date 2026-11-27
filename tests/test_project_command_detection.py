"""Command detection must never raise, including for unrecognised projects.

``coding.commands.detect_commands`` documents "Never raises". A project with no
recognised marker file previously left the detected language unbound, which
raised UnboundLocalError and silently stripped the debug loop's auto-verify
command (and its project context) for that project.
"""
import pytest

from coding.commands import detect_commands
from coding.profile import build_profile

MARKERS = [
    "pyproject.toml", "requirements.txt", "setup.py", "pytest.ini", "manage.py",
    "package.json", "pom.xml", "build.gradle", "CMakeLists.txt", "Makefile",
]


def test_empty_directory_does_not_raise(tmp_path):
    commands = detect_commands(tmp_path)
    assert commands.to_context() == "no detected commands"
    assert not commands.test.exists


def test_directory_with_only_source_files_does_not_raise(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    commands = detect_commands(tmp_path)
    assert commands.to_context() == "no detected commands"


def test_unreadable_directory_does_not_raise(tmp_path):
    # A path that cannot be listed must degrade, not raise.
    missing = tmp_path / "does-not-exist"
    commands = detect_commands(missing)
    assert commands.to_context() == "no detected commands"


def test_build_profile_survives_unrecognised_project(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    profile = build_profile(tmp_path, cache_dir=tmp_path / ".cache")
    assert profile.name
    assert isinstance(profile.to_context(), str)


@pytest.mark.parametrize("marker", MARKERS)
def test_every_known_marker_still_detects_a_language(tmp_path, marker):
    (tmp_path / marker).write_text("{}\n", encoding="utf-8")
    commands = detect_commands(tmp_path)
    assert commands.to_context() != "no detected commands", marker


def test_python_marker_still_yields_a_test_command(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    commands = detect_commands(tmp_path)
    assert commands.test.exists
    assert "pytest" in commands.test.command


def test_node_marker_still_yields_commands(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest", "build": "tsc"}}', encoding="utf-8")
    commands = detect_commands(tmp_path)
    assert commands.test.exists
    assert commands.build.exists


def test_debug_context_is_not_degraded_for_unrecognised_project(tmp_path):
    """A marker-less project must still get full debug context.

    Before the fix ``detect_commands`` raised, so ``_debug_context`` bailed out
    and returned a bare dict with no project profile, no relevant files and no
    project path -- which also stripped the debug loop's auto-verify step.
    """
    from config.settings import Settings
    from core.agent import Agent
    from core.context import ProjectContext

    root = tmp_path / "bare"
    root.mkdir()
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")

    settings = Settings(base_dir=tmp_path, sessions_dir=tmp_path / "sessions",
                        memory_dir=tmp_path / "mem", logs_dir=tmp_path / "logs",
                        data_dir=tmp_path, scratch_dir=tmp_path / "ws")
    for path in (settings.sessions_dir, settings.memory_dir, settings.logs_dir, settings.scratch_dir):
        path.mkdir(parents=True, exist_ok=True)

    agent = Agent.__new__(Agent)  # context builder only; no provider needed
    agent._settings = settings
    agent._project = ProjectContext(name="bare", path=str(root), language="Python",
                                   git_repo=False)
    agent._pending_goal = ""
    ctx = agent._debug_context("fix the failing test")

    assert ctx["project_path"] == str(root)
    assert ctx["project_name"] == "bare"
    assert ctx.get("project"), "project profile must be present"
    # No marker means no detected test command; that is correct, not a degradation.
    # (Relevant-file selection is a relevance heuristic and may legitimately be empty.)


def test_debug_context_keeps_test_command_when_a_marker_exists(tmp_path):
    from config.settings import Settings
    from core.agent import Agent
    from core.context import ProjectContext

    root = tmp_path / "marked"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")

    settings = Settings(base_dir=tmp_path, sessions_dir=tmp_path / "sessions",
                        memory_dir=tmp_path / "mem", logs_dir=tmp_path / "logs",
                        data_dir=tmp_path, scratch_dir=tmp_path / "ws")
    for path in (settings.sessions_dir, settings.memory_dir, settings.logs_dir, settings.scratch_dir):
        path.mkdir(parents=True, exist_ok=True)

    agent = Agent.__new__(Agent)
    agent._settings = settings
    agent._project = ProjectContext(name="marked", path=str(root), language="Python",
                                   git_repo=False)
    agent._pending_goal = ""
    ctx = agent._debug_context("fix the failing test")
    assert ctx.get("test_command"), "auto-verify needs a detected test command"
    assert "pytest" in ctx["test_command"]
