from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DependencySummary:
    manager: str = "none"
    count: int = 0
    entries: list[str] = field(default_factory=list)
    manifest: str = ""

    def to_context(self, max_chars: int = 300) -> str:
        parts = [f"manager={self.manager}", f"count={self.count}"]
        if self.entries:
            parts.append("top: " + ", ".join(self.entries[:12]))
        text = "; ".join(parts)
        return text[:max_chars]


_MANAGER_BY_LOCK = {
    "Conan.lock": "conan",
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
}


def analyze_dependencies(root: Path) -> DependencySummary:
    """Best-effort dependency summary for a project root. Never raises."""
    try:
        if (root / "requirements.txt").is_file():
            entries = _pip_lines((root / "requirements.txt").read_text(encoding="utf-8"))
            return _mk("pip", "requirements.txt", entries)
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                data = {}
            deps = data.get("project", {}).get("dependencies", []) or []
            entries = sorted({re.split(r"[<>=!~\[\s]", d.strip())[0] for d in deps if isinstance(d, str) and d.strip()})
            if entries:
                return _mk("pip", "pyproject.toml", entries)
        package_json = root / "package.json"
        if package_json.is_file():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            deps = list(data.get("dependencies", {}).keys())
            dev = list(data.get("devDependencies", {}).keys())
            manager = "npm"
            for lock, m in _MANAGER_BY_LOCK.items():
                if (root / lock).is_file():
                    manager = m
                    break
            return _mk(manager, "package.json", deps + dev)
        pom = root / "pom.xml"
        if pom.is_file():
            try:
                text = pom.read_text(encoding="utf-8")
            except OSError:
                text = ""
            artifacts = re.findall(r"<artifactId>\s*([^<]+?)\s*</artifactId>", text)
            return _mk("maven", "pom.xml", artifacts)
        build_gradle = root / "build.gradle"
        if build_gradle.is_file():
            return _mk("gradle", "build.gradle", [])
    except Exception:  # pragma: no cover - defensive
        pass
    return DependencySummary()


def _pip_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[\s]", line)[0].strip()
        if name:
            out.append(name)
    return out


def _mk(manager: str, manifest: str, entries: list[str]) -> DependencySummary:
    unique = []
    for e in entries:
        if e and e not in unique:
            unique.append(e)
    return DependencySummary(manager=manager, count=len(unique), entries=unique[:60], manifest=manifest)