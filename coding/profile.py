from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .commands import ProjectCommands, detect_commands
from .dependency_analyzer import DependencySummary, analyze_dependencies
from .entry_points import EntryPoint, detect_entry_points
from .frameworks import Framework, detect_framework
from .project_index import FileRecord, index_cached, summarize_files
from .code_analyzer import ProjectSnapshot, snapshot_project


@dataclass
class ProjectProfile:
    name: str
    path: str
    language: str = "unknown"
    markers: list[str] = field(default_factory=list)
    git_repo: bool = False
    git_branch: str = ""
    git_dirty: bool = False
    framework: str = "unknown"
    framework_confidence: float = 0.0
    entry_points: list[EntryPoint] = field(default_factory=list)
    dependencies: DependencySummary = field(default_factory=DependencySummary)
    commands: ProjectCommands = field(default_factory=ProjectCommands)
    file_count: int = 0
    files_summary: str = ""

    @classmethod
    def from_snapshot(cls, snap: ProjectSnapshot, **overrides) -> "ProjectProfile":
        fields = {k: getattr(snap, k) for k in ("name", "path", "language", "markers", "git_repo")}
        fields.update(overrides)
        return cls(**fields)

    def to_context(self, max_chars: int = 700) -> str:
        lines = [
            f"Project: {self.name} ({self.language}, framework={self.framework})",
            f"Path: {self.path}",
        ]
        if self.git_repo:
            git = f"git branch={self.git_branch or '?'}"
            if self.git_dirty:
                git += " (uncommitted changes)"
            lines.append(git)
        if self.entry_points:
            lines.append("Entry points: " + "; ".join(e.to_context() for e in self.entry_points[:4]))
        if self.dependencies.count:
            lines.append("Dependencies: " + self.dependencies.to_context(240))
        lines.append("Commands: " + self.commands.to_context())
        text = "\n".join(lines)
        return text[:max_chars]


def build_profile(root: Path, cache_dir: Path | None = None, max_chars_files: int = 700) -> ProjectProfile:
    """Build a full project profile. Never raises for analysis problems."""
    records = index_cached(root, cache_dir)
    snap = snapshot_project(root)
    cls = ProjectProfile.from_snapshot(snap) if snap else ProjectProfile(name=root.name, path=str(root.resolve()))
    deps = analyze_dependencies(root)
    framework = detect_framework(root, cls.language, deps.entries)
    entries = detect_entry_points(root, records)
    commands = detect_commands(root)
    cls.dependencies = deps
    cls.entry_points = entries
    cls.commands = commands
    cls.framework = framework.name
    cls.framework_confidence = framework.confidence
    cls.file_count = len(records)
    cls.files_summary = summarize_files(records, max_chars_files)
    branch, dirty = git_state(root)
    cls.git_branch = branch
    cls.git_dirty = dirty
    return cls


def git_state(root: Path) -> tuple[str, bool]:
    if not (root / ".git").is_dir():
        return "", False
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root),
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
        b = (branch.stdout or "").strip() if branch.returncode == 0 else ""
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root),
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
        return b, bool((dirty.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        return "", False


def path_of(profile: ProjectProfile) -> Path:
    return Path(profile.path)