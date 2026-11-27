from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_LANG_BY_EXT = {
    ".py": "Python",
    ".pyw": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".java": "Java",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".md": "Markdown",
    ".toml": "TOML",
}

_MARKERS = {
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "setup.py": "Python",
    "package.json": "Node.js",
    "tsconfig.json": "TypeScript",
    "pom.xml": "Java (Maven)",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "CMakeLists.txt": "C/C++ (CMake)",
    "*.csproj": "C#",
}


@dataclass
class ProjectSnapshot:
    name: str
    path: str
    language: str = "unknown"
    markers: list[str] = field(default_factory=list)
    git_repo: bool = False


def detect_language(path: str) -> str:
    ext = Path(path).suffix.lower()
    return _LANG_BY_EXT.get(ext, "unknown")


def snapshot_project(root: Path) -> ProjectSnapshot | None:
    if not root.is_dir():
        return None
    markers = []
    language = "unknown"
    for marker, lang in _MARKERS.items():
        if marker.startswith("*."):
            if list(root.glob(marker)):
                markers.append(marker)
                if language == "unknown":
                    language = lang
        elif (root / marker).exists():
            markers.append(marker)
            if language == "unknown":
                language = lang
    git_repo = (root / ".git").is_dir()
    if language == "unknown":
        py_files = sorted(root.glob("*.py"))
        if py_files:
            language = "Python"
    return ProjectSnapshot(name=root.name, path=str(root.resolve()), language=language, markers=markers, git_repo=git_repo)