import json

import pytest

from coding.commands import detect_commands
from coding.dependency_analyzer import analyze_dependencies
from coding.entry_points import detect_entry_points
from coding.frameworks import detect_framework
from coding.profile import build_profile
from coding.project_index import IGNORED_DIRS, index_cached, index_project
from tools.coding import project_files, project_grep, project_info
from tools import build_registry


def _write(root, path, content):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_index_project_ignores_vendor_dirs(tmp_path):
    _write(tmp_path, "src/app.py", "x = 1\n")
    _write(tmp_path, "node_modules/dep/index.js", "y\n")
    _write(tmp_path, ".venv/lib/site.py", "z\n")
    records = index_project(tmp_path)
    paths = {r.relative for r in records}
    assert "src/app.py" in paths
    assert not any(p.startswith(("node_modules", ".venv")) for p in paths)


def test_index_cached_roundtrip(tmp_path):
    _write(tmp_path, "main.py", "print(1)\n")
    cache = tmp_path / "idx"
    first = index_cached(tmp_path, cache, max_files=100)
    second = index_cached(tmp_path, cache, max_files=100)
    assert [r.relative for r in first] == [r.relative for r in second]


def test_dependency_pip_requirements(tmp_path):
    _write(tmp_path, "requirements.txt", "flask==3.0\nnumpy>=2.0\n# comment\n")
    deps = analyze_dependencies(tmp_path)
    assert deps.manager == "pip"
    assert "flask" in deps.entries and "numpy" in deps.entries
    assert deps.count == 2


def test_dependency_pyproject(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi", "uvicorn"]\n')
    deps = analyze_dependencies(tmp_path)
    assert deps.manager == "pip" and "fastapi" in deps.entries


def test_dependency_node(tmp_path):
    _write(tmp_path, "package.json", '{"dependencies": {"express": "^4.18"}, "devDependencies": {"jest": "^29"}}')
    _write(tmp_path, "package-lock.json", "{}")
    deps = analyze_dependencies(tmp_path)
    assert deps.manager == "npm" and "express" in deps.entries and "jest" in deps.entries


def test_entry_points_python(tmp_path):
    _write(tmp_path, "manage.py", "")
    _write(tmp_path, "pytest.ini", "")
    eps = detect_entry_points(tmp_path)
    kinds = {e.kind for e in eps}
    assert any(e.relative_path == "manage.py" and e.kind == "run" for e in eps)
    assert "test" in kinds


def test_entry_points_node(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "node src/index.js", "test": "jest"}}')
    _write(tmp_path, "src/index.js", "")
    eps = detect_entry_points(tmp_path)
    assert any(e.relative_path == "src/index.js" for e in eps)
    assert any(e.kind == "test" for e in eps)


def test_framework_flask(tmp_path):
    _write(tmp_path, "app.py", "from flask import Flask\n")
    _write(tmp_path, "requirements.txt", "flask\n")
    fw = detect_framework(tmp_path, "Python", ["flask"])
    assert fw.name == "Flask"


def test_framework_django_via_manage(tmp_path):
    _write(tmp_path, "manage.py", "")
    fw = detect_framework(tmp_path, "Python", [])
    assert fw.name == "Django"


def test_framework_vite(tmp_path):
    _write(tmp_path, "vite.config.ts", "")
    fw = detect_framework(tmp_path, "TypeScript", [])
    assert fw.name == "Vite"


def test_commands_django_test(tmp_path):
    _write(tmp_path, "manage.py", "")
    commands = detect_commands(tmp_path)
    assert commands.test.exists and "manage.py" in commands.test.command


def test_commands_node_test(tmp_path):
    _write(tmp_path, "package.json", '{"scripts": {"test": "jest", "build": "tsc"}}')
    commands = detect_commands(tmp_path)
    assert "npm test" in commands.test.command
    assert "npm run build" in commands.build.command


def test_build_profile_rich(tmp_path):
    _write(tmp_path, "requirements.txt", "flask\npytest\n")
    _write(tmp_path, "app.py", "import flask\nprint('hi')\n")
    _write(tmp_path, "tests/test_app.py", "def test_x(): pass\n")
    profile = build_profile(tmp_path)
    assert profile.language == "Python"
    assert profile.framework == "Flask"
    assert profile.file_count >= 3
    assert profile.dependencies.count >= 2
    assert "run=python app.py" in profile.commands.to_context() or "run=" in profile.commands.to_context()
    assert "Project:" in profile.to_context()


@pytest.fixture
def flask_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    _write(root, "requirements.txt", "flask==3.0\n")
    _write(root, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write(root, "routes/home.py", "def home(): return 'ok'\n")
    return root


def test_project_info_tool(flask_fixture, tmp_settings):
    result = project_info(str(flask_fixture), tmp_settings)
    assert result.success
    assert "Flask" in result.output
    assert "Python" in result.output


def test_project_files_tool(flask_fixture, tmp_settings):
    result = project_files(str(flask_fixture), pattern="routes", settings=tmp_settings)
    assert result.success
    assert any("home.py" in line for line in result.output.splitlines())


def test_project_grep_tool(flask_fixture, tmp_settings):
    result = project_grep(str(flask_fixture), "Flask\\(|app =", settings=tmp_settings)
    assert result.success
    assert any("app.py" in line and "Flask" in line for line in result.output.splitlines())


def test_project_tools_registered(tmp_settings):
    registry = build_registry(tmp_settings)
    names = registry.names()
    assert {"project.info", "project.files", "project.grep"} <= set(names)
    assert registry.get("project.info").level.value == 0