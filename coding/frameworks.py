from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Framework:
    name: str
    confidence: float
    reason: str = ""

    @property
    def unknown(self) -> bool:
        return self.name == "unknown"


def detect_framework(root: Path, language: str, deps_entries: list[str]) -> Framework:
    """Identify the governing framework of a project from markers + dependencies."""
    files = {p.name for p in root.iterdir()} if root.is_dir() else set()

    if language == "Python":
        if "Django" in deps_entries or "django" in deps_entries or "manage.py" in files:
            return Framework("Django", 0.9, "manage.py / django dependency")
        if "flask" in deps_entries or "Flask" in deps_entries or "flask" in {d.lower() for d in deps_entries}:
            return Framework("Flask", 0.85, "flask dependency")
        if "fastapi" in {d.lower() for d in deps_entries}:
            return Framework("FastAPI", 0.85, "fastapi dependency")
        return Framework("Python", 0.4, "no framework marker")

    if language in ("JavaScript", "TypeScript"):
        if (root / "next.config.js").is_file() or (root / "next.config.ts").is_file():
            return Framework("Next.js", 0.85, "next.config present")
        if (root / "vite.config.ts").is_file() or (root / "vite.config.js").is_file():
            return Framework("Vite", 0.8, "vite config present")
        lowered = {d.lower() for d in deps_entries}
        if "react" in lowered or "react-dom" in lowered:
            return Framework("React", 0.8, "react dependency present")
        if "express" in lowered:
            return Framework("Express", 0.8, "express dependency present")
        return Framework("Node.js", 0.4, "no framework marker")

    if language == "Java":
        lowered = {d.lower() for d in deps_entries}
        if any("spring-boot" in d for d in lowered) or (root / "mvnw").exists():
            return Framework("Spring Boot", 0.85, "spring-boot dependency / maven wrapper")
        return Framework("Maven", 0.5, "pom.xml present")

    if language in ("C", "C++"):
        if (root / "CMakeLists.txt").is_file():
            return Framework("CMake", 0.85, "CMakeLists.txt present")
        if (root / "Makefile").is_file():
            return Framework("Make", 0.7, "Makefile present")
        return Framework("C/C++", 0.3, "no build-system marker")

    return Framework("unknown", 0.0, "no detectable framework")