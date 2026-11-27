from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.logging_setup import get_logger
from memory.storage import save_json

log = get_logger("change_tracker")


@dataclass
class ChangeStep:
    tool: str
    args: dict = field(default_factory=dict)
    allowed: bool = False
    executed: bool = False
    success: bool = False
    reason: str = ""
    ts: str = ""
    status: str = ""
    duration_ms: int = 0
    exit_code: int | None = None
    checkpoint_id: str = ""


@dataclass
class CodingSession:
    id: str
    project: str
    goal: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = "active"
    final_result: str = ""
    steps: list[ChangeStep] = field(default_factory=list)

    def mission_block(self) -> str:
        if not self.id:
            return ""
        lines = [f"Mission: {self.goal or 'Unspecified coding task'}", f"Project: {self.project}"]
        done = sum(1 for s in self.steps if s.success)
        lines.append(f"Status: {self.status} ({done}/{len(self.steps)} steps succeeded)")
        for step in self.steps[-3:]:
            status = "✓" if step.success else ("✗" if step.executed else "–")
            lines.append(f"  {status} {step.tool} {_scrub_args(step.args)}")
        if self.final_result:
            lines.append(f"Result: {self.final_result}")
        return "\n".join(lines)


def _scrub_args(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)[:160]


def _safe_session_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in (".", "..")
        and not any(char in value for char in ("/", "\\", ":", "\x00"))
    )


class ChangeTracker:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._active: CodingSession | None = None

    @property
    def active(self) -> CodingSession | None:
        return self._active

    def start(self, goal: str, project_path: str, session_id: str = "") -> CodingSession:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^\w.-]+", "-", project_path.strip().replace("\\", "/").split("/")[-1] or "project")
        resolved_id = session_id.strip() if session_id else f"{stamp}-{slug}-{uuid.uuid4().hex[:6]}"
        if not _safe_session_id(resolved_id):
            raise ValueError("unsafe coding session id")
        session = CodingSession(
            id=resolved_id,
            project=project_path,
            goal=goal.strip() or "Open-ended coding task",
            started_at=stamp,
        )
        self._active = session
        if not self.save(session):
            self._active = None
            raise OSError("could not persist coding session")
        log.info("coding session started: %s (%s)", session.id, session.goal)
        return session

    def step(
        self,
        tool: str,
        args: dict,
        allowed: bool,
        executed: bool,
        success: bool,
        reason: str = "",
        status: str = "",
        duration_ms: int = 0,
        exit_code: int | None = None,
        checkpoint_id: str = "",
    ) -> None:
        if self._active is None:
            return
        self._active.steps.append(ChangeStep(
            tool=tool, args={k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in (args or {}).items()},
            allowed=allowed, executed=executed, success=success, reason=reason or "",
            ts=time.strftime("%H:%M:%S"), status=status, duration_ms=int(duration_ms or 0),
            exit_code=exit_code, checkpoint_id=checkpoint_id,
        ))
        if not self.save(self._active):
            log.warning("could not persist coding session step")

    def finish(self, result: str, status: str = "completed") -> CodingSession | None:
        if self._active is None:
            return None
        session = self._active
        session.final_result = result
        session.finished_at = time.strftime("%Y%m%d-%H%M%S")
        session.status = status
        if not self.save(session):
            return None
        return session

    def clear_active(self) -> None:
        self._active = None

    def restore_active(self, session: CodingSession) -> None:
        if session.status != "active" or not _safe_session_id(session.id):
            raise ValueError("only an active valid coding session can be restored")
        self._active = session

    def summary(self) -> str:
        if self._active is None:
            return "No active coding session."
        return self._active.mission_block()

    def save(self, session: CodingSession) -> bool:
        try:
            payload = asdict(session)
            payload["steps"] = [asdict(s) for s in session.steps]
            save_json(self.sessions_dir / f"{session.id}.json", payload)
            return True
        except OSError as exc:
            log.warning("could not save session: %s", exc)
            return False

    @classmethod
    def load_task(cls, sessions_dir: Path, task_id: str) -> CodingSession | None:
        if not _safe_session_id(task_id):
            return None
        path = Path(sessions_dir) / f"{task_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_steps = data.pop("steps", [])
            steps = [ChangeStep(**step) for step in raw_steps]
            session = CodingSession(**data, steps=steps)
        except (OSError, ValueError, TypeError):
            return None
        return session if session.id == task_id else None

    @classmethod
    def load_all(cls, sessions_dir: Path) -> list[CodingSession]:
        root = Path(sessions_dir)
        if not root.is_dir():
            return []
        sessions = []
        for path in sorted(root.glob("*.json")):
            session = cls.load_task(root, path.stem)
            if session is not None:
                sessions.append(session)
        return sessions

    @classmethod
    def load_latest(cls, sessions_dir: Path) -> CodingSession | None:
        files = sorted(Path(sessions_dir).glob("*.json")) if Path(sessions_dir).is_dir() else []
        if not files:
            return None
        try:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            data["steps"] = [ChangeStep(**s) for s in data.get("steps", [])]
            return CodingSession(**data)
        except (OSError, ValueError, TypeError):
            return None