from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .project_index import FileRecord, index_project


@dataclass
class EntryPoint:
    relative_path: str
    kind: str  # run | test | build
    confidence: float
    reason: str = ""

    def to_context(self) -> str:
        return f"{self.relative_path} ({self.kind}, confidence={round(self.confidence, 2)})"


_PY_RUN = {
    "manage.py": ("run", 0.95, "Django management entry point"),
    "app.py": ("run", 0.8, "common Flask/app entry point"),
    "main.py": ("run", 0.8, "PEP 8 conventional main module"),
    "run.py": ("run", 0.85, "launcher module"),
    "wsgi.py": ("run", 0.7, "WSGI application entry point"),
    "asgi.py": ("run", 0.7, "ASGI application entry point"),
    "__init__.py": ("run", 0.1, "package marker (low confidence)"),
}

_NODE_RUN = {
    "src/index.js": ("run", 0.8, "Vite/Node source entry"),
    "src/index.ts": ("run", 0.8, "TypeScript source entry"),
    "index.js": ("run", 0.75, "root JS entry"),
    "server.js": ("run", 0.8, "Node server entry"),
    "src/main.ts": ("run", 0.75, "TS entry"),
    "src/main.js": ("run", 0.75, "JS entry"),
    "main.js": ("run", 0.7, "Node main module"),
}

_C_RUN = {"main.c": ("run", 0.9, "C entry point"), "main.cpp": ("run", 0.9, "C++ entry point"), "main.cc": ("run", 0.9, "C++ entry point")}

_JAVA_RUN = {"_Application.java": ("run", 0.9, "Spring Boot application"), "_Main.java": ("run", 0.8, "Java main class")}

_PY_TEST = {
    "pytest.ini": ("test", 0.8, "pytest config present"),
    "tox.ini": ("test", 0.7, "tox config present"),
    "conftest.py": ("test", 0.6, "pytest conftest present"),
}


def detect_entry_points(root: Path, records: list[FileRecord] | None = None) -> list[EntryPoint]:
    """Heuristic entry-point detection. Order = descending confidence."""
    entries: list[EntryPoint] = []
    files = {r.relative for r in (records or index_project(root))}
    lower = {f.lower(): f for f in files}

    for name, (kind, conf, reason) in list(_PY_RUN.items()) + list(_NODE_RUN.items()) + list(_C_RUN.items()):
        if name in files:
            entries.append(EntryPoint(name, kind, conf, reason))
        else:
            alt = lower.get(name.lower())
            if alt:
                entries.append(EntryPoint(alt, kind, conf, reason))

    for marker in files:
        base = marker.rsplit("/", 1)[-1]
        if base.lower().endswith("_application.java") or base.lower().endswith("main.java"):
            entries.append(EntryPoint(marker, "run", 0.9, "Java application entry"))
        elif base.lower().endswith("_main.java"):
            entries.append(EntryPoint(marker, "run", 0.8, "Java main class"))
        elif base == "manage.py":
            entries.append(EntryPoint(marker, "test", 0.7, "Django manage.py (python manage.py test)"))

    for marker, (kind, conf, reason) in _PY_TEST.items():
        if marker in files:
            entries.append(EntryPoint(marker, kind, conf, reason))

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        scripts = data.get("scripts", {}) or {}
        for script, kind, conf in (("start", "run", 0.9), ("dev", "run", 0.8), ("test", "test", 0.8), ("build", "build", 0.8)):
            if scripts.get(script):
                entries.append(EntryPoint(f"package.json script:{script}", kind, conf, f"npm {script}"))

    dedup: dict[str, EntryPoint] = {}
    for e in entries:
        key = (e.relative_path, e.kind)
        if key not in dedup or e.confidence > dedup[key].confidence:
            dedup[key] = e
    return sorted(dedup.values(), key=lambda e: (-e.confidence, e.relative_path))[:12]