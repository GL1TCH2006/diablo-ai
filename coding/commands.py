from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandSpec:
    command: str = ""
    confidence: float = 0.0
    reason: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.command)


@dataclass
class ProjectCommands:
    test: CommandSpec = field(default_factory=CommandSpec)
    build: CommandSpec = field(default_factory=CommandSpec)
    run: CommandSpec = field(default_factory=CommandSpec)
    install: CommandSpec = field(default_factory=CommandSpec)

    def to_context(self) -> str:
        bits = []
        for name in ("run", "build", "test", "install"):
            spec = getattr(self, name)
            if spec.exists:
                bits.append(f"{name}={spec.command}")
        return "; ".join(bits) if bits else "no detected commands"

    def best_test(self) -> CommandSpec:
        return self.test


def _package_manager(root: Path) -> str:
    for name, mgr in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("package-lock.json", "npm")):
        if (root / name).is_file():
            return mgr
    return "npm"


def detect_commands(root: Path) -> ProjectCommands:
    """Best-effort detection of test/build/run/install commands. Never raises."""
    commands = ProjectCommands()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return commands

    files = {p.name for p in entries if p.is_file()}

    # An unrecognised project has no marker; keep language empty rather than
    # leaving it unbound so this function honours its "never raises" contract.
    language = ""
    for marker, lang in (("pyproject.toml", "python"), ("requirements.txt", "python"), ("setup.py", "python"),
                         ("pytest.ini", "python"), ("manage.py", "python"),
                         ("package.json", "node"), ("pom.xml", "java"), ("build.gradle", "java"),
                         ("CMakeLists.txt", "cc"), ("Makefile", "cc")):
        if marker in files:
            language = lang
            break

    if "node" in language:
        mgr = _package_manager(root)
        pkg = root / "package.json"
        scripts: dict = {}
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
            except (json.JSONDecodeError, OSError):
                scripts = {}
        if scripts.get("test"):
            commands.test = CommandSpec(f"{mgr} test", 0.9, "package.json scripts.test")
        elif "test" in scripts:
            commands.test = CommandSpec(f"{mgr} test", 0.8, "package.json scripts.test")
        if scripts.get("build"):
            commands.build = CommandSpec(f"{mgr} run build", 0.9, "package.json scripts.build")
        if scripts.get("start"):
            commands.run = CommandSpec(f"{mgr} start", 0.9, "package.json scripts.start")
        if scripts.get("dev"):
            commands.run = CommandSpec(f"{mgr} run dev", 0.85, "package.json scripts.dev")
        commands.install = CommandSpec(f"{mgr} install", 0.9, "lockfile / package.json")

    elif "python" in language:
        import sys
        py = sys.executable
        if "manage.py" in files:
            commands.test = CommandSpec(f"{py} manage.py test", 0.85, "Django manage.py")
            commands.run = CommandSpec(f"{py} manage.py runserver", 0.8, "Django manage.py")
        elif (root / "pyproject.toml").is_file():
            commands.test = CommandSpec(f"{py} -m pytest -q", 0.7, "pyproject present")
        elif (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
            commands.test = CommandSpec(f"{py} -m pytest -q", 0.8, "pytest config present")
        else:
            commands.test = CommandSpec(f"{py} -m pytest -q", 0.5, "python project heuristic")
        for entry in ("app.py", "main.py", "run.py"):
            if entry in files:
                commands.run = CommandSpec(f"{py} {entry}", 0.8, f"{entry} present")
                break
        commands.install = CommandSpec(f"{py} -m pip install -r requirements.txt", 0.6, "python project")

    elif "java" in language:
        wrapper = root / "mvnw"
        mvn = "mvnw" if wrapper.is_file() else "mvn"
        if (root / "pom.xml").is_file():
            commands.test = CommandSpec(f"{mvn} test", 0.9, "pom.xml present")
            commands.build = CommandSpec(f"{mvn} package", 0.8, "pom.xml present")
        elif (root / "build.gradle").is_file():
            gradle = "gradlew" if (root / "gradlew").is_file() else "gradle"
            commands.test = CommandSpec(f"{gradle} test", 0.85, "gradle present")
            commands.build = CommandSpec(f"{gradle} build", 0.8, "gradle present")

    elif "cc" in language:
        if "Makefile" in files:
            commands.build = CommandSpec("make", 0.9, "Makefile present")
            have_test = False
            try:
                have_test = any(line.strip().startswith("test:") for line in (root / "Makefile").read_text(encoding="utf-8").splitlines())
            except OSError:
                have_test = False
            if have_test:
                commands.test = CommandSpec("make test", 0.85, "Makefile test target")
        elif "CMakeLists.txt" in files:
            commands.build = CommandSpec("cmake -B build && cmake --build build", 0.85, "CMakeLists.txt present")
            commands.test = CommandSpec("ctest --test-dir build", 0.7, "CMake present")

    return commands