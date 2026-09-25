from pathlib import Path

from coding.code_analyzer import detect_language, snapshot_project
from memory.project_memory import ProjectMemory


def test_detect_language_by_extension():
    assert detect_language("x.py") == "Python"
    assert detect_language("x.js") == "JavaScript"
    assert detect_language("x.unknown_ext") == "unknown"


def test_snapshot_project_detects_python_and_git(tmp_path):
    project = tmp_path / "smart-campus"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / "app.py").write_text("print(1)\n", encoding="utf-8")

    snap = snapshot_project(project)
    assert snap is not None
    assert snap.name == "smart-campus"
    assert snap.language == "Python"
    assert snap.git_repo is True
    assert "pyproject.toml" in snap.markers


def test_project_memory_discovery(tmp_path, tmp_settings):
    workspace = tmp_path / "projects"
    (workspace / "a").mkdir(parents=True)
    (workspace / "a" / "package.json").write_text("{}", encoding="utf-8")

    memory = ProjectMemory(tmp_settings.memory_dir, workspace)
    discovered = memory.discover()
    assert any(s.name == "a" for s in discovered)
    assert any(s.language == "Node.js" for s in discovered)

    memory.set_active("a")
    active = memory.get_active()
    assert active is not None and active.name == "a"