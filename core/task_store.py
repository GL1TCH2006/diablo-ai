from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger
from core.planner import ExecutionPlanRecord, PlanStepError, migrate_plan_record
from memory.storage import load_json, save_json

log = get_logger("task_store")


def _valid_task_id(task_id) -> bool:
    return (
        isinstance(task_id, str)
        and bool(task_id.strip())
        and task_id not in (".", "..")
        and "/" not in task_id
        and "\\" not in task_id
        and ":" not in task_id
        and "\x00" not in task_id
    )


def _dict_field(data: dict, name: str) -> dict:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list_field(data: dict, name: str) -> list:
    value = data.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


@dataclass
class CommitInfo:
    hash: str
    message: str
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CommitInfo":
        if not isinstance(data, dict):
            raise ValueError("commit must be an object")
        return cls(
            hash=str(data.get("hash") or ""),
            message=str(data.get("message") or ""),
            timestamp=str(data.get("timestamp") or ""),
        )


@dataclass
class GitTaskState:
    project_root: str = ""
    starting_commit: str = ""
    starting_branch: str = ""
    ending_commit: Optional[str] = None
    ending_branch: Optional[str] = None
    commits_made: list[CommitInfo] = field(default_factory=list)
    branches_created: list[str] = field(default_factory=list)
    branches_switched: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    net_diff_stat: str = ""
    final_status: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["commits_made"] = [c.to_dict() for c in self.commits_made]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "GitTaskState":
        if not isinstance(data, dict):
            raise ValueError("git state must be an object")
        commits = []
        for value in _list_field(data, "commits_made"):
            if isinstance(value, dict):
                commits.append(CommitInfo.from_dict(value))
            elif isinstance(value, str):
                commits.append(CommitInfo(hash=value, message="", timestamp=""))
            else:
                raise ValueError("commit entries must be objects or strings")
        return cls(
            project_root=str(data.get("project_root") or ""),
            starting_commit=str(data.get("starting_commit") or ""),
            starting_branch=str(data.get("starting_branch") or ""),
            ending_commit=data.get("ending_commit"),
            ending_branch=data.get("ending_branch"),
            commits_made=commits,
            branches_created=[str(value) for value in _list_field(data, "branches_created")],
            branches_switched=[str(value) for value in _list_field(data, "branches_switched")],
            files_changed=[str(value) for value in _list_field(data, "files_changed")],
            net_diff_stat=str(data.get("net_diff_stat") or ""),
            final_status=str(data.get("final_status") or ""),
        )


@dataclass
class TaskChangeStep:
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskChangeStep":
        if not isinstance(data, dict):
            raise ValueError("change step must be an object")
        return cls(
            tool=str(data.get("tool") or ""),
            args=_dict_field(data, "args"),
            allowed=bool(data.get("allowed", False)),
            executed=bool(data.get("executed", False)),
            success=bool(data.get("success", False)),
            reason=str(data.get("reason") or ""),
            ts=str(data.get("ts") or ""),
            status=str(data.get("status") or ""),
            duration_ms=int(data.get("duration_ms", 0) or 0),
            exit_code=data.get("exit_code"),
            checkpoint_id=str(data.get("checkpoint_id") or ""),
        )


def _coerce_plan(value) -> Optional["ExecutionPlanRecord"]:
    """Normalise anything plan-shaped into an ExecutionPlanRecord.

    Returns None when there is no plan or the payload cannot be understood, so a
    legacy or corrupt record reads as "no plan" instead of raising. A plan we
    cannot read must never be treated as a plan with zero steps, because that
    would let a resume conclude that no work ever ran.
    """
    if value is None:
        return None
    if isinstance(value, ExecutionPlanRecord):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return ExecutionPlanRecord.from_dict(migrate_plan_record(value))
    except (PlanStepError, TypeError, ValueError) as exc:
        log.warning("discarding unreadable plan payload: %s", exc)
        return None


@dataclass
class TaskRecord:
    task_id: str
    project_name: str
    project_root: str
    title: str
    goal: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "pending"
    outcome: Optional[str] = None
    git: GitTaskState = field(default_factory=GitTaskState)
    snapshots: list[str] = field(default_factory=list)
    change_steps: list[TaskChangeStep] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # M2.12 P2: durable execution plan. Owned by this record; carries no task
    # identity of its own (task_id/run_id stay authoritative here and on
    # TaskManager). None means "no plan", which includes every record written
    # before P2.
    plan: Optional[ExecutionPlanRecord] = None

    VALID_STATUSES = ("pending", "active", "completed", "failed", "cancelled")

    def __post_init__(self):
        if not _valid_task_id(self.task_id):
            raise ValueError("task_id must be a safe non-empty identifier")
        if self.status not in self.VALID_STATUSES:
            raise ValueError(f"invalid status: {self.status}, must be one of {self.VALID_STATUSES}")
        self.plan = _coerce_plan(self.plan)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["git"] = self.git.to_dict()
        data["change_steps"] = [s.to_dict() for s in self.change_steps]
        # Serialised explicitly rather than via asdict recursion so the plan
        # owns its own schema, including plan_version and StepStatus values.
        data["plan"] = self.plan.to_dict() if isinstance(self.plan, ExecutionPlanRecord) else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        if not isinstance(data, dict):
            raise ValueError("task record must be an object")
        return cls(
            task_id=str(data.get("task_id") or ""),
            project_name=str(data.get("project_name") or ""),
            project_root=str(data.get("project_root") or ""),
            title=str(data.get("title") or ""),
            goal=str(data.get("goal") or ""),
            created_at=str(data.get("created_at") or ""),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            status=str(data.get("status") or "pending"),
            outcome=data.get("outcome"),
            git=GitTaskState.from_dict(_dict_field(data, "git")),
            snapshots=[str(value) for value in _list_field(data, "snapshots")],
            change_steps=[TaskChangeStep.from_dict(value) for value in _list_field(data, "change_steps")],
            metadata=_dict_field(data, "metadata"),
            plan=_coerce_plan(data.get("plan")),
        )


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _commit_dict(value) -> dict:
    if isinstance(value, CommitInfo):
        return value.to_dict()
    if isinstance(value, dict):
        return CommitInfo.from_dict(value).to_dict()
    return CommitInfo(hash=str(value or ""), message="", timestamp="").to_dict()


def _change_step_dict(value) -> dict:
    if isinstance(value, TaskChangeStep):
        return value.to_dict()
    if isinstance(value, dict):
        return TaskChangeStep.from_dict(value).to_dict()
    return TaskChangeStep(tool=str(value or "")).to_dict()


def serialize_task(record: TaskRecord) -> dict:
    git = record.git if isinstance(record.git, GitTaskState) else GitTaskState()
    return {
        "task_id": record.task_id,
        "project": {
            "name": record.project_name or "",
            "root": record.project_root or "",
        },
        "title": record.title or "",
        "goal": record.goal or "",
        "created_at": record.created_at or "",
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "status": record.status or "",
        "outcome": record.outcome,
        "git": {
            "project_root": git.project_root or "",
            "starting_commit": git.starting_commit or "",
            "starting_branch": git.starting_branch or "",
            "ending_commit": git.ending_commit,
            "ending_branch": git.ending_branch,
            "commits_made": [_commit_dict(commit) for commit in (git.commits_made or [])],
            "branches_created": _string_list(git.branches_created),
            "branches_switched": _string_list(git.branches_switched),
            "files_changed": _string_list(git.files_changed),
            "net_diff_stat": git.net_diff_stat or "",
            "final_status": git.final_status or "",
        },
        "snapshots": _string_list(record.snapshots),
        "change_steps": [_change_step_dict(step) for step in (record.change_steps or [])],
        "metadata": dict(record.metadata) if isinstance(record.metadata, dict) else {},
        "plan": record.plan.to_dict() if isinstance(record.plan, ExecutionPlanRecord) else None,
    }


def serialize_task_summary(record: TaskRecord) -> dict:
    git = record.git if isinstance(record.git, GitTaskState) else GitTaskState()
    return {
        "task_id": record.task_id,
        "project_name": record.project_name or "",
        "project_root": record.project_root or "",
        "title": record.title or "",
        "goal": record.goal or "",
        "created_at": record.created_at or "",
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "status": record.status or "",
        "outcome": record.outcome,
        "git": {
            "starting_commit": git.starting_commit or "",
            "starting_branch": git.starting_branch or "",
            "ending_commit": git.ending_commit,
            "ending_branch": git.ending_branch,
            "commits_made_count": len(git.commits_made or []),
            "files_changed_count": len(git.files_changed or []),
            "snapshot_count": len(record.snapshots or []),
        },
    }


def _task_sort_key(record: TaskRecord) -> tuple[str, str]:
    activity = record.finished_at or record.started_at or record.created_at or ""
    return activity, record.task_id


class TaskStore:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        self.tasks_dir = self.sessions_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        if not _valid_task_id(task_id):
            raise ValueError("unsafe task_id")
        return self.tasks_dir / f"{task_id}.json"

    def create(self, record: TaskRecord) -> TaskRecord:
        if not _valid_task_id(record.task_id):
            raise ValueError("task_id must be a safe non-empty identifier")
        path = self._task_path(record.task_id)
        if path.exists():
            raise ValueError(f"task with id {record.task_id} already exists")
        save_json(path, record.to_dict())
        log.info("task created: %s", record.task_id)
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        if not _valid_task_id(task_id):
            return None
        path = self._task_path(task_id)
        if not path.exists():
            return None
        try:
            data = load_json(path, {})
            if not data:
                return None
            return TaskRecord.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("failed to load task %s: %s", task_id, exc)
            return None

    def inspect_task(self, task_id: str) -> Optional[dict]:
        if not _valid_task_id(task_id):
            return None
        record = self.get(task_id)
        return serialize_task(record) if record is not None else None

    def update(self, task_id: str, /, **fields) -> Optional[TaskRecord]:
        if not _valid_task_id(task_id):
            return None
        record = self.get(task_id)
        if record is None:
            return None
        if "task_id" in fields and fields["task_id"] != task_id:
            raise ValueError("task_id cannot be changed")
        for key, value in fields.items():
            if key == "git" and isinstance(value, dict) and isinstance(record.git, GitTaskState):
                for git_key, git_value in value.items():
                    if hasattr(record.git, git_key):
                        setattr(record.git, git_key, git_value)
            elif key == "plan":
                record.plan = _coerce_plan(value)
            elif hasattr(record, key):
                setattr(record, key, value)
            else:
                record.metadata[key] = value
        TaskRecord.from_dict(record.to_dict())
        save_json(self._task_path(task_id), record.to_dict())
        log.info("task updated: %s", task_id)
        return record

    def link_snapshot(self, task_id: str, snapshot_id: str) -> Optional[TaskRecord]:
        record = self.get(task_id)
        if record is None or not _valid_task_id(snapshot_id):
            return None
        if snapshot_id not in record.snapshots:
            record.snapshots.append(snapshot_id)
            return self.update(task_id, snapshots=record.snapshots)
        return record

    def delete(self, task_id: str) -> bool:
        if not _valid_task_id(task_id):
            return False
        path = self._task_path(task_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            log.info("task deleted: %s", task_id)
            return True
        except OSError as exc:
            log.warning("failed to delete task %s: %s", task_id, exc)
            return False

    def list_all(self) -> list[TaskRecord]:
        records = []
        if not self.tasks_dir.is_dir():
            return records
        for path in sorted(self.tasks_dir.glob("*.json")):
            try:
                data = load_json(path, {})
                if data:
                    records.append(TaskRecord.from_dict(data))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("failed to load task from %s: %s", path, exc)
        return records

    def list_by_project(self, project_name: str) -> list[TaskRecord]:
        return [r for r in self.list_all() if r.project_name == project_name]

    def query_tasks(
        self,
        project: str = "",
        status: str = "",
        limit: Optional[int] = 20,
    ) -> list[dict]:
        if not isinstance(project, str) or not isinstance(status, str):
            raise ValueError("project and status filters must be strings")
        project = project.strip()
        status = status.strip().lower()
        if status and status not in TaskRecord.VALID_STATUSES:
            raise ValueError(f"invalid status: {status}, must be one of {TaskRecord.VALID_STATUSES}")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")

        records = self.list_all()
        if project:
            records = [record for record in records if record.project_name == project]
        if status:
            records = [record for record in records if record.status == status]
        records.sort(key=_task_sort_key, reverse=True)
        if limit is not None:
            records = records[:limit]
        return [serialize_task_summary(record) for record in records]

    def finish(self, task_id: str, outcome: str, status: str = "completed") -> Optional[TaskRecord]:
        record = self.get(task_id)
        if record is None:
            return None
        if status not in TaskRecord.VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        record.finished_at = time.strftime("%Y%m%d-%H%M%S")
        record.outcome = outcome
        record.status = status
        save_json(self._task_path(task_id), record.to_dict())
        log.info("task finished: %s (status=%s)", task_id, status)
        return record