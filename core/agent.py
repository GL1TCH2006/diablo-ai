from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ai.provider import AIProvider, ToolCall
from config.permissions import PermissionManager
from config.settings import Settings
from core.cancellation import CancellationRequested, CancellationToken, cancellation_requested
from core.context import build_system_prompt, ProjectContext
from core.events import EventBus
from core.executor import ExecutionOutcome, Executor
from core.logging_setup import get_logger
from core.planner import (
    ExecutionPlan,
    ExecutionPlanRecord,
    PlanStepError,
    PlanStepRecord,
    PlanValidationError,
    Planner,
    side_effect_key,
)
from core.request_context import (
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    BoundedRequestGate,
    RequestContext,
)
from core.task_manager import StepStatus, TaskState
from core.verification import VerificationResult, VerificationState, Verifier
from coding.change_tracker import ChangeTracker
from coding.snapshot_manager import SnapshotManager
from memory.rule_engine import RuleEngine
from memory.rules_store import RuleStore
from memory.memory_store import MemoryStore
from core.task_store import TaskStore, TaskRecord, GitTaskState, CommitInfo, TaskChangeStep

log = get_logger("agent")


class Agent:
    def __init__(
        self,
        provider: AIProvider,
        settings: Settings,
        registry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
        project: ProjectContext | None = None,
        memory_conversation=None,
        memory_preferences=None,
        memory_project=None,
        model_name: str = "",
        model_catalog: list | None = None,
    ):
        self._provider = provider
        self._settings = settings
        self._registry = registry
        self._permissions = permissions
        self._events = events
        self._confirmer = confirmer
        self._project = project
        self._conversation = memory_conversation
        self._preferences = memory_preferences
        self._project_memory = memory_project
        self._model_name = model_name
        self._model_catalog = model_catalog
        self._planner = Planner(settings)
        self._executor = Executor(
            registry,
            permissions,
            events,
            confirmer,
            max_result_chars=settings.max_result_chars,
            before_execute=self._before_execute,
            state_listener=self._executor_state_listener,
            event_context=self._event_payload,
        )
        self._verifier = Verifier()
        self._messages: list[dict] = []
        self._max_concurrent_requests = DEFAULT_MAX_CONCURRENT_REQUESTS
        self._request_gate = BoundedRequestGate(self._max_concurrent_requests)
        self._request_context = RequestContext()
        # M2.12 P2: the durable plan for the request in flight. Lives on the
        # Agent rather than on RequestContext because the P1 field set is
        # ratified and must not grow; it is reset per request in
        # _begin_request_context and mirrored to TaskRecord.plan immediately.
        self._active_plan: ExecutionPlanRecord | None = None
        # The plan step currently between write-ahead and terminal state. Set by
        # _step_write_ahead so pre-execution hooks (checkpoints) can attach
        # provenance to the step they belong to.
        self._running_plan_step: PlanStepRecord | None = None
        self._task_store = TaskStore(settings.sessions_dir)
        self._change_tracker = ChangeTracker(settings.sessions_dir)
        self._snapshot_manager = SnapshotManager(settings.sessions_dir)
        self._rules_store = RuleStore(settings.memory_dir / "rules.json")
        self._rules_engine = RuleEngine(self._rules_store, project=self._project.name if self._project else "")
        permissions.rule_engine = self._rules_engine
        self._memory_store = MemoryStore(settings.memory_dir / "memory.json")
        if self._project_memory is not None:
            from core.project_task_history import ProjectTaskHistory
            attach_task_history = getattr(self._project_memory, "attach_task_history", None)
            if callable(attach_task_history):
                attach_task_history(ProjectTaskHistory(self._task_store, settings.workspace_root))
        self._router = self._build_router()
        from tools import runtime_context
        runtime_context.router = self._router
        runtime_context.prefs = self._preferences
        self._reconcile_interrupted_tasks()

    # ------------------------------------------------------------------
    # M2.12 P1 — request-scoped state
    #
    # Request state lives on RequestContext. The names below are thin,
    # backwards-compatible accessors onto the *active* context, kept because
    # the UI and existing tests read and write them directly. They are
    # accessors only: no request state is stored on the Agent instance.
    # ------------------------------------------------------------------

    @property
    def request_context(self) -> RequestContext:
        """The active (most recent) request context.

        Retained after ``respond()`` returns so post-request inspection keeps
        working exactly as before. Not an execution input for a future
        scheduler, which will pass each context explicitly.
        """
        context = getattr(self, "_request_context", None)
        if context is None:
            context = RequestContext()
            self._request_context = context
        return context

    @property
    def max_concurrent_requests(self) -> int:
        """Configured request admission limit. P1 default is 1 (single-flight)."""
        return int(getattr(self, "_max_concurrent_requests", DEFAULT_MAX_CONCURRENT_REQUESTS))

    @property
    def request_gate(self) -> BoundedRequestGate:
        return self._request_gate

    # -- legacy aliases onto the active RequestContext -----------------

    @property
    def _tasks(self):
        return self.request_context.tasks

    @_tasks.setter
    def _tasks(self, value) -> None:
        self.request_context.tasks = value

    @property
    def _active_cancellation_token(self):
        return self.request_context.cancellation_token

    @_active_cancellation_token.setter
    def _active_cancellation_token(self, value) -> None:
        self.request_context.cancellation_token = value

    @property
    def _router_verify_ok(self) -> bool:
        return self.request_context.router_verify_ok

    @_router_verify_ok.setter
    def _router_verify_ok(self, value: bool) -> None:
        self.request_context.router_verify_ok = bool(value)

    @property
    def _escalations_used(self) -> int:
        return self.request_context.escalations_used

    @_escalations_used.setter
    def _escalations_used(self, value: int) -> None:
        self.request_context.escalations_used = int(value)

    @property
    def _offline_fallback_used(self) -> bool:
        return self.request_context.offline_fallback_used

    @_offline_fallback_used.setter
    def _offline_fallback_used(self, value: bool) -> None:
        self.request_context.offline_fallback_used = bool(value)

    @property
    def _pending_goal(self) -> str:
        return self.request_context.pending_goal

    @_pending_goal.setter
    def _pending_goal(self, value: str) -> None:
        self.request_context.pending_goal = "" if value is None else str(value)

    @property
    def _recovered_task_ids(self) -> list[str]:
        return self.request_context.recovered_task_ids

    @_recovered_task_ids.setter
    def _recovered_task_ids(self, value) -> None:
        self.request_context.recovered_task_ids = list(value or [])

    @property
    def _terminal_event_emitted(self) -> bool:
        return self.request_context.terminal_event_emitted

    @_terminal_event_emitted.setter
    def _terminal_event_emitted(self, value: bool) -> None:
        self.request_context.terminal_event_emitted = bool(value)

    @property
    def _last_model(self):
        return self.request_context.last_model

    @_last_model.setter
    def _last_model(self, value) -> None:
        self.request_context.last_model = value

    def _build_router(self):
        from ai.model_router import ModelRouter
        catalog = list(self._model_catalog) if self._model_catalog is not None else []
        if self._model_catalog is None and hasattr(self._provider, "list_models"):
            try:
                catalog = self._provider.list_models()
            except Exception as exc:
                log.warning("could not list models for router: %s", exc)
        router = ModelRouter(self._settings, catalog=catalog)
        router.apply_management_overrides(self._preferences)
        return router

    @property
    def router(self):
        return self._router

    def model_status(self) -> str:
        choices = self._router.state() if self._router else {}
        verified = "verified" if choices.get("fast_identity_verified") else "identity unverified"
        line = f"MODE: {choices.get('mode', '?')}  FAST: {choices.get('fast', '?')} ({verified})  STRONG: {choices.get('strong', '?')}"
        if self._last_model is not None:
            line += f"\nCurrent: {self._last_model.to_state_line()}"
        return line

    _CODING_PREFIXES = ("project.", "code.", "git.", "gh.", "memory.", "rules.", "filesystem.write_file", "filesystem.delete_file")

    @property
    def conversation(self):
        return self._conversation

    @property
    def preferences(self):
        return self._preferences

    @property
    def project_memory(self):
        return self._project_memory

    def task_history_context(self, limit: int = 5) -> str:
        if self._project is None or self._project_memory is None:
            return ""
        render = getattr(self._project_memory, "render_task_history_context", None)
        if not callable(render):
            return ""
        try:
            return render(self._project.name, max_chars=900, limit=limit)
        except Exception as exc:
            log.warning("project task history render failed: %s", exc)
            return ""

    @property
    def change_tracker(self):
        return self._change_tracker

    @property
    def current_project(self) -> ProjectContext | None:
        return self._project

    def set_confirmer(self, confirmer=None) -> None:
        self._confirmer = confirmer
        self._executor.set_confirmer(confirmer)

    def _event_payload(self) -> dict:
        return {
            "run_id": getattr(self._tasks, "run_id", ""),
            "task_id": getattr(self._tasks, "task_id", "") or getattr(self._tasks, "run_id", ""),
        }

    def _emit_lifecycle(self, event_kind: str, **values) -> None:
        bus = getattr(self, "_events", None)
        if bus is not None:
            bus.emit(event_kind, {**self._event_payload(), **values})

    def _transition(self, state: TaskState) -> None:
        if not hasattr(self._tasks, "transition"):
            return
        try:
            self._tasks.transition(state)
        except ValueError as exc:
            log.warning("task lifecycle transition ignored: %s", exc)

    def _executor_state_listener(self, state: str) -> None:
        if state == "awaiting_permission":
            self._transition(TaskState.AWAITING_PERMISSION)

    def _begin_verification(self) -> None:
        self._transition(TaskState.VERIFYING)
        self._emit_lifecycle("verification_started", state="verifying")

    def _record_verification(self, result: VerificationResult) -> None:
        self._tasks.set_verification(result.state, result.detail, result.evidence)
        if result.state == VerificationState.FAILED:
            self._emit_lifecycle("verification_failed", verification=result.state.value, detail=result.detail)
            self._transition(TaskState.FAILED)
        else:
            self._emit_lifecycle(
                "verification_completed",
                verification=result.state.value,
                detail=result.detail,
                kind=result.kind,
            )

    def _lifecycle_metadata(self) -> dict:
        tasks = self._tasks
        state = getattr(tasks, "state", TaskState.CREATED)
        verification = getattr(tasks, "verification", VerificationState.NOT_RUN)
        return {
            "state": state.value if hasattr(state, "value") else str(state),
            "verification": verification.value if hasattr(verification, "value") else str(verification),
            "verification_detail": getattr(tasks, "verification_detail", ""),
            "verification_evidence": dict(getattr(tasks, "verification_evidence", {}) or {}),
            "verification_history": list(getattr(tasks, "verification_history", []) or [])[-20:],
            "recovery_attempts": int(getattr(tasks, "recovery_attempts", 0) or 0),
            "partial": bool(getattr(tasks, "partial", False)),
            "failure_reason": getattr(tasks, "failure_reason", ""),
            "run_id": getattr(tasks, "run_id", ""),
        }

    def _sync_task_metadata(self, task_id: str = "") -> None:
        store = getattr(self, "_task_store", None)
        tracker = getattr(self, "_change_tracker", None)
        target = task_id or getattr(self._tasks, "task_id", "")
        if store is None or not target:
            return
        record = store.get(target)
        if record is None:
            return
        record.metadata = dict(record.metadata or {})
        record.metadata["lifecycle"] = self._lifecycle_metadata()
        if tracker is not None and tracker.active is not None and tracker.active.id == target:
            record.change_steps = [
                TaskChangeStep(
                    tool=step.tool,
                    args=step.args,
                    allowed=step.allowed,
                    executed=step.executed,
                    success=step.success,
                    reason=step.reason,
                    ts=step.ts,
                    status=getattr(step, "status", ""),
                    duration_ms=getattr(step, "duration_ms", 0),
                    exit_code=getattr(step, "exit_code", None),
                    checkpoint_id=getattr(step, "checkpoint_id", ""),
                )
                for step in tracker.active.steps
            ]
        try:
            store.update(target, metadata=record.metadata, change_steps=record.change_steps)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist task lifecycle for %s: %s", target, exc)

    def _create_run_record(self) -> None:
        project = self._project
        project_name = project.name if project and project.name != "(none)" else "unknown"
        project_root = project.path if project and project.path else ""
        record = TaskRecord(
            task_id=self._tasks.task_id,
            project_name=project_name,
            project_root=project_root,
            title=self._tasks.task_title,
            goal=self._pending_goal.strip() or self._tasks.task_title,
            created_at=time.strftime("%Y%m%d-%H%M%S"),
            status="pending",
            metadata={"lifecycle": self._lifecycle_metadata()},
        )
        try:
            self._task_store.create(record)
        except ValueError as exc:
            if "already exists" not in str(exc):
                log.warning("could not create task record: %s", exc)

    def _classify_interrupted_steps(self, record) -> list[str]:
        """Flag steps left RUNNING by a process death.

        A step persisted as RUNNING may or may not have applied its side effect.
        The status is deliberately left alone and the step is marked
        ``interrupted`` instead, so nothing replays it implicitly and a future
        Agent.resume can require an explicit decision. Read-only steps are
        reported separately because replaying them is free.
        """
        plan = getattr(record, "plan", None)
        if plan is None:
            return []
        flagged: list[str] = []
        for step in plan.steps:
            if step.status is StepStatus.RUNNING and not step.interrupted:
                step.mark_interrupted()
                flagged.append(step.step_id)
        return flagged

    def _reconcile_interrupted_tasks(self) -> None:
        store = getattr(self, "_task_store", None)
        tracker = getattr(self, "_change_tracker", None)
        if store is None or tracker is None:
            return
        records = {record.task_id: record for record in store.list_all()}
        for record in records.values():
            if record.status not in ("pending", "active"):
                continue
            session = tracker.load_task(store.sessions_dir, record.task_id)
            if session is not None and session.status == "active":
                session.status = "failed"
                session.final_result = "Recovered after interrupted process; not resumed automatically."
                session.finished_at = time.strftime("%Y%m%d-%H%M%S")
                tracker.save(session)
            metadata = dict(record.metadata or {})
            interrupted = self._classify_interrupted_steps(record)
            metadata["lifecycle"] = {
                **(metadata.get("lifecycle") or {}),
                "state": "failed",
                "verification": "failed",
                "verification_detail": "Recovered after interrupted process.",
                "recovered": True,
                "recovery_action": "marked_failed",
                "interrupted_steps": interrupted,
            }
            reconciled = False
            try:
                if interrupted:
                    store.update(record.task_id, plan=record.plan)
                store.update(record.task_id, metadata=metadata)
                reconciled = store.finish(record.task_id, "Recovered after interrupted process; not resumed automatically.", "failed") is not None
            except (OSError, TypeError, ValueError) as exc:
                log.warning("could not reconcile interrupted task %s: %s", record.task_id, exc)
            if reconciled:
                self._recovered_task_ids.append(record.task_id)
                for step_id in interrupted:
                    self._emit_lifecycle(
                        "plan_step_interrupted",
                        task_id=record.task_id,
                        step_id=step_id,
                        replayable=False,
                    )
        for session in tracker.load_all(store.sessions_dir):
            if session.status != "active":
                continue
            record = records.get(session.id)
            if record is None:
                session.status = "failed"
                session.final_result = "Recovered orphan coding session; not resumed automatically."
                session.finished_at = time.strftime("%Y%m%d-%H%M%S")
                tracker.save(session)
                self._recovered_task_ids.append(session.id)
            elif record.status in TaskRecord.VALID_STATUSES and record.status not in ("pending", "active"):
                session.status = record.status
                session.final_result = record.outcome or session.final_result
                session.finished_at = record.finished_at or time.strftime("%Y%m%d-%H%M%S")
                tracker.save(session)

    def session_summary(self) -> str:
        return self._change_tracker.summary() if self._change_tracker else "No active coding session."

    def rules_summary(self) -> str:
        project = self._project.name if self._project else self._rules_engine.project
        return self._rules_store.render_active(project=project, limit=self._settings.rules_max_active_in_prompt) or "(no active rules)"

    def rules_and_conflicts(self) -> str:
        text = self.rules_summary()
        conflicting = self._rules_store.detect_conflicts_for_all()
        if conflicting:
            text += f"\nConflict warnings:\n{conflicting}"
        return text

    def why_summary(self) -> str:
        decision = getattr(self._permissions, "last_decision", None)
        if decision is None:
            return "No permission decision recorded yet."
        tool, dec = decision
        reason = dec.reason or "no specific reason"
        engine_note = self._rules_engine.explain() if self._rules_engine else ""
        return f"Last decision for '{tool}': allowed={dec.allow} confirm={dec.requires_confirmation}\nReason: {reason}\n{engine_note}"

    def git_diff_summary(self) -> str:
        root = Path(self._project.path) if self._project and self._project.path else None
        if not root or not (root / ".git").is_dir():
            return "No git repository detected for the active project."
        from tools.git import git_diff, git_status
        status = git_status(str(root))
        diff = git_diff(str(root), include_staged=False, stat=False)
        if diff.error:
            return f"git diff failed: {diff.error}"
        parts = []
        if status.output:
            parts.append(status.output)
        parts.append(diff.output)
        return "\n\n".join(parts)

    def _is_coding_tool(self, tool: str) -> bool:
        if tool in ("terminal.execute", "python.run_script"):
            return True
        return tool.startswith(self._CODING_PREFIXES)

    def _canonical_root(self, value: str) -> str:
        if not value:
            return ""
        candidate = Path(str(value))
        try:
            from tools.git import _git
            code, out, _ = _git(str(candidate), ["rev-parse", "--show-toplevel"])
            if code == 0 and out.strip():
                return str(Path(out.strip()).resolve())
        except Exception:
            pass
        try:
            return str(candidate.resolve())
        except OSError:
            return str(candidate)

    def _project_root_from_args(self, args: dict, tool: str = "") -> str:
        workspace = args.get("workspace")
        if workspace:
            return self._canonical_root(str(workspace))
        if tool == "code.patch_file":
            return self._canonical_root(str(getattr(self._settings, "base_dir", Path.cwd())))
        for key in ("path", "file", "script"):
            value = args.get(key)
            if not value:
                continue
            candidate = Path(str(value))
            if candidate.is_dir():
                return self._canonical_root(str(candidate))
            if candidate.exists() or candidate.suffix:
                return self._canonical_root(str(candidate.parent))
            return self._canonical_root(str(candidate))
        for key in ("root", "working_dir", "cwd"):
            value = args.get(key)
            if value:
                return self._canonical_root(str(value))
        if self._project and self._project.path:
            return self._canonical_root(self._project.path)
        scratch = getattr(self._settings, "scratch_dir", "")
        return self._canonical_root(str(scratch)) if scratch else ""

    def _before_execute(self, tool: str, args: dict) -> None:
        if not self._is_coding_tool(tool) or self._change_tracker is None:
            return
        project_root = self._project_root_from_args(args, tool)
        if self._change_tracker.active is not None:
            active_root = self._canonical_root(self._change_tracker.active.project)
            if project_root and active_root and project_root.casefold() != active_root.casefold():
                raise ValueError("operation target is outside the active task project")
            self._attach_checkpoint(self._maybe_create_checkpoint(tool, args))
            return
        goal = (self._pending_goal.strip() or "Development task across multiple tool calls").strip()
        self._change_tracker.start(goal, project_root, session_id=getattr(self._tasks, "task_id", ""))
        self._pending_goal = ""
        if project_root:
            self._capture_task_git_start(
                task_id=self._change_tracker.active.id,
                project_root=project_root,
                title=self._tasks.task_title or goal,
                goal=goal,
            )
        self._attach_checkpoint(self._maybe_create_checkpoint(tool, args))

    def _attach_checkpoint(self, snapshot_id: str) -> None:
        """Bind a pre-execution snapshot to the step about to run."""
        if not snapshot_id:
            return
        step = getattr(self, "_running_plan_step", None)
        if step is None or step.checkpoint_id:
            return
        step.checkpoint_id = snapshot_id
        self._persist_plan()

    def _note_step(self, tool: str, args: dict, outcome) -> None:
        if not self._is_coding_tool(tool) or self._change_tracker is None:
            return
        if self._change_tracker.active is None:
            if not outcome.executed:
                return
            project_hint = self._project_root_from_args(args, tool)
            goal = (self._pending_goal.strip() or "Development task across multiple tool calls").strip()
            self._change_tracker.start(goal, project_hint, session_id=getattr(self._tasks, "task_id", ""))
            self._pending_goal = ""
            if self._change_tracker.active:
                self._capture_task_git_start(
                    task_id=self._change_tracker.active.id,
                    project_root=project_hint,
                    title=self._tasks.task_title or goal,
                    goal=goal,
                )
        running = getattr(self, "_running_plan_step", None)
        self._change_tracker.step(
            tool,
            args,
            bool(getattr(outcome, "allowed", False)),
            bool(getattr(outcome, "executed", False)),
            bool(getattr(outcome, "success", False)),
            str(getattr(outcome, "reason", "") or ""),
            str(getattr(outcome, "status", "") or ""),
            int(getattr(outcome, "duration_ms", 0) or 0),
            getattr(outcome, "exit_code", None),
            str(getattr(running, "checkpoint_id", "") or ""),
        )
        self._sync_task_metadata(self._change_tracker.active.id)
        if tool == "git.commit" and getattr(outcome, "success", False) and self._change_tracker.active:
            self._capture_commit(
                task_id=self._change_tracker.active.id,
                project_root=self._project_root_from_args(args, tool),
                commit_message=args.get("message", ""),
            )

    def _persist_tool_observation(self, tool: str, outcome: ExecutionOutcome) -> None:
        spec = self._registry.get(tool) if isinstance(tool, str) else None
        if spec is None or not spec.persist_data or not isinstance(getattr(outcome, "data", None), dict):
            return
        task_id = getattr(self._tasks, "task_id", "")
        store = getattr(self, "_task_store", None)
        if not task_id or store is None:
            return
        result = dict(outcome.data)
        capability_result = result.get("capability_result")
        if not isinstance(capability_result, dict):
            return
        observation = {
            "tool": tool,
            "status": str(getattr(outcome, "status", "") or ""),
            "capability": capability_result.get("capability", ""),
            "operation": capability_result.get("operation", ""),
            "result": capability_result,
        }
        try:
            encoded = json.dumps(observation, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        if len(encoded) > 24000:
            observation["result"] = {
                "truncated": True,
                "summary": encoded[:24000],
            }
        try:
            record = store.get(task_id)
            if record is None:
                return
            metadata = dict(record.metadata or {})
            observations = list(metadata.get("tool_observations") or [])
            observations.append(observation)
            metadata["tool_observations"] = observations[-20:]
            store.update(task_id, metadata=metadata)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist tool observation for %s: %s", task_id, exc)

    def _should_checkpoint(self, tool: str) -> bool:
        checkpoint_tools = {
            "git.commit", "git.push", "git.restore", "git.reset", "git.rebase",
            "git.add", "git.pull", "git.branch.create", "git.branch.switch",
            "filesystem.write_file", "filesystem.delete_file",
            "terminal.execute", "python.run_script",
            "code.patch_file",
        }
        return tool in checkpoint_tools

    def _maybe_create_checkpoint(self, tool: str, args: dict) -> str:
        """Create a pre-execution checkpoint. Returns its snapshot id, or "".

        M2.12 P2: the id is returned so the caller can attach it to the
        executing plan step, which is what makes a step's checkpoint an exact
        "workspace state immediately before this step ran" undo boundary.
        """
        if not self._should_checkpoint(tool):
            return ""
        if self._snapshot_manager is None:
            return ""
        if self._change_tracker is None or self._change_tracker.active is None:
            return ""

        task_id = self._change_tracker.active.id
        if not task_id:
            return ""

        project_root = self._project_root_from_args(args, tool)

        if not project_root:
            return ""

        from pathlib import Path
        if not Path(project_root).is_dir():
            return ""

        if not (Path(project_root) / ".git").exists():
            return ""

        label = f"pre-{tool.replace('.', '-')}"
        try:
            snapshot = self._snapshot_manager.create_snapshot(
                task_id=task_id,
                label=label,
                project_root=project_root,
                creation_reason=f"auto-checkpoint-before-{tool}",
            )
            self._task_store.link_snapshot(task_id, snapshot.snapshot_id)
            return str(getattr(snapshot, "snapshot_id", "") or "")
        except Exception as exc:
            record = self._task_store.get(task_id)
            if record is not None:
                metadata = dict(record.metadata or {})
                metadata["checkpoint_error"] = f"{tool}: {exc}"
                self._task_store.update(task_id, metadata=metadata)
            log.warning("auto-checkpoint failed for %s: %s", tool, exc)
            return ""

    def _capture_task_git_start(self, task_id: str, project_root: str, title: str, goal: str) -> None:
        """Capture starting Git state for a task and create TaskRecord."""
        if self._task_store is None:
            return
        if not task_id or not project_root:
            return

        git_state = GitTaskState(project_root=project_root)

        # Try to capture starting Git state if it's a Git repo
        try:
            from pathlib import Path
            from tools.git import _git
            root_path = Path(project_root)
            if (root_path / ".git").exists():
                code, out, err = _git(project_root, ["rev-parse", "HEAD"])
                if code == 0 and out:
                    git_state.starting_commit = out.strip()
                code, out, err = _git(project_root, ["branch", "--show-current"])
                if code == 0 and out:
                    git_state.starting_branch = out.strip()
                else:
                    git_state.starting_branch = "(detached)"
        except Exception as exc:
            log.warning("failed to capture starting git state for task %s: %s", task_id, exc)

        # Determine project name
        project_name = self._project.name if self._project and self._project.name != "(none)" else Path(project_root).name or "unknown"
        existing = self._task_store.get(task_id)
        if existing is not None:
            title = existing.title or title
            goal = existing.goal or goal

        record = TaskRecord(
            task_id=task_id,
            project_name=project_name,
            project_root=project_root,
            title=title,
            goal=goal,
            created_at=time.strftime("%Y%m%d-%H%M%S"),
            started_at=time.strftime("%Y%m%d-%H%M%S"),
            status="active",
            git=git_state,
            metadata={"lifecycle": self._lifecycle_metadata()},
        )
        try:
            self._task_store.create(record)
        except ValueError as exc:
            if "already exists" in str(exc):
                self._task_store.update(
                    task_id,
                    project_name=project_name,
                    project_root=project_root,
                    title=title,
                    goal=goal,
                    status="active",
                    started_at=time.strftime("%Y%m%d-%H%M%S"),
                    git=git_state,
                    metadata={"lifecycle": self._lifecycle_metadata()},
                )
            else:
                log.warning("failed to create task record for %s: %s", task_id, exc)

    def _capture_task_git_end(self, task_id: str, outcome: str, status: str) -> bool:
        """Capture ending Git state for a task and finish the TaskRecord."""
        if self._task_store is None:
            return False
        if not task_id:
            return False

        record = self._task_store.get(task_id)
        if record is None and self._change_tracker and self._change_tracker.active:
            session = self._change_tracker.active
            project_root = self._project.path if self._project and self._project.path else ""
            if not project_root:
                candidate = Path(session.project)
                project_root = str(candidate) if candidate.is_dir() else ""
            project_name = self._project.name if self._project and self._project.name != "(none)" else Path(project_root).name or "unknown"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                self._task_store.create(TaskRecord(
                    task_id=task_id,
                    project_name=project_name,
                    project_root=project_root,
                    title=self._tasks.task_title if self._tasks else task_id,
                    goal=session.goal,
                    created_at=session.started_at or stamp,
                    started_at=session.started_at or stamp,
                    status="active",
                    git=GitTaskState(project_root=project_root),
                    metadata={"lifecycle": self._lifecycle_metadata()},
                ))
            except (OSError, TypeError, ValueError) as exc:
                log.warning("failed to create late task record for %s: %s", task_id, exc)
            record = self._task_store.get(task_id)
        if record is None:
            return False

        if self._change_tracker and self._change_tracker.active:
            session = self._change_tracker.active
            record.change_steps = [
                TaskChangeStep(
                    tool=step.tool,
                    args=step.args,
                    allowed=step.allowed,
                    executed=step.executed,
                    success=step.success,
                    reason=step.reason,
                    ts=step.ts,
                    status=getattr(step, "status", ""),
                    duration_ms=getattr(step, "duration_ms", 0),
                    exit_code=getattr(step, "exit_code", None),
                    checkpoint_id=getattr(step, "checkpoint_id", ""),
                )
                for step in session.steps
            ]

        project_root = record.project_root
        if not project_root:
            try:
                self._task_store.update(
                    task_id,
                    change_steps=record.change_steps,
                    metadata={**(record.metadata or {}), "lifecycle": self._lifecycle_metadata()},
                )
                return self._task_store.finish(task_id, outcome, status) is not None
            except Exception as exc:
                log.warning("could not finish task record for %s: %s", task_id, exc)
                return False

        git_state = record.git

        # Capture ending Git state
        try:
            from tools.git import _git, git_diff, git_status
            root_path = Path(project_root)
            if (root_path / ".git").exists():
                # Ending commit
                code, out, err = _git(project_root, ["rev-parse", "HEAD"])
                if code == 0 and out:
                    git_state.ending_commit = out.strip()

                # Ending branch
                code, out, err = _git(project_root, ["branch", "--show-current"])
                if code == 0 and out:
                    git_state.ending_branch = out.strip()
                else:
                    git_state.ending_branch = "(detached)"

                # Final status
                status_result = git_status(project_root)
                if status_result.success:
                    git_state.final_status = status_result.output or ""

                # Net diff stat
                diff_result = git_diff(project_root, include_staged=False, stat=True)
                if diff_result.success:
                    git_state.net_diff_stat = diff_result.output or ""

                # Changed files from porcelain status
                if git_state.final_status:
                    import re
                    for line in git_state.final_status.splitlines():
                        line = line.strip()
                        if line and not line.startswith("##"):
                            # Porcelain format: XY path
                            parts = line.split(maxsplit=1)
                            if len(parts) == 2 and parts[1] not in git_state.files_changed:
                                git_state.files_changed.append(parts[1])
        except Exception as exc:
            log.warning("failed to capture ending git state for task %s: %s", task_id, exc)

        # Update record with final state
        try:
            self._task_store.update(
                task_id,
                git=git_state,
                change_steps=record.change_steps,
                metadata={**(record.metadata or {}), "lifecycle": self._lifecycle_metadata()},
            )
            return self._task_store.finish(task_id, outcome, status) is not None
        except Exception as exc:
            log.warning("failed to finish task record for %s: %s", task_id, exc)
            return False

    def _capture_commit(self, task_id: str, project_root: Optional[str], commit_message: str) -> None:
        """Capture a commit made during the task and add it to the TaskRecord."""
        if self._task_store is None:
            return
        if not task_id or not project_root:
            return

        try:
            from pathlib import Path
            from tools.git import _git

            root_path = Path(project_root)
            if not (root_path / ".git").exists():
                return

            # Get the latest commit hash
            code, out, err = _git(project_root, ["rev-parse", "HEAD"])
            if code != 0 or not out:
                return
            commit_hash = out.strip()

            # Avoid duplicate recording if commit already recorded
            record = self._task_store.get(task_id)
            if record is None:
                return

            # Check if this commit is already recorded (by hash)
            existing_hashes = {c.hash for c in record.git.commits_made}
            if commit_hash in existing_hashes:
                return

            # Create commit info
            commit_info = CommitInfo(
                hash=commit_hash,
                message=commit_message.strip(),
                timestamp=time.strftime("%Y%m%d-%H%M%S"),
            )

            # Append to commits_made
            record.git.commits_made.append(commit_info)

            # Also update ending_commit to track the latest
            record.git.ending_commit = commit_hash

            self._task_store.update(task_id, git=record.git)
            log.info("captured commit %s for task %s", commit_hash[:8], task_id)

        except Exception as exc:
            log.warning("failed to capture commit for task %s: %s", task_id, exc)

    def _select_model(self, user_input: str):
        from ai.model_router import estimate_complexity
        if self._model_name and (
            getattr(self._provider, "name", "") == "offline"
            or not self._router.catalog_ok
            or self._router.is_available(self._model_name)
        ):
            self._last_model = type("MC", (), {
                "name": self._model_name,
                "mode": "fixed",
                "to_state_line": lambda self_: f"MODEL: {self._model_name} MODE: fixed SOURCE: explicit",
                "escalated": False,
            })()
            return self._last_model
        if self._model_name:
            log.warning("explicit model %s is unavailable or not tool-capable; using adaptive fallback", self._model_name)
        if not hasattr(self, "_router"):
            model_name = getattr(getattr(self, "_settings", None), "ai_model", "") or "offline/rule-based"
            self._last_model = type("MC", (), {
                "name": model_name,
                "mode": "fixed",
                "to_state_line": lambda self_: f"MODEL: {model_name} MODE: fixed SOURCE: fallback",
                "escalated": False,
            })()
            return self._last_model
        project_pref = ""
        if self._project and self._project_memory:
            try:
                project_pref = self._project_memory.get_preferred_model(self._project.name)
            except Exception:  # pragma: no cover - defensive
                project_pref = ""
        choice = self._router.choose(
            project_pref=project_pref,
            complexity=estimate_complexity(user_input),
            last_verify_failed=not self._router_verify_ok,
            escalations_used=self._escalations_used,
        )
        self._last_model = choice
        if choice.escalated:
            self._escalations_used += 1
        log.info("router: %s", choice.to_state_line())
        return choice

    def _capture_plan_goal(self, user_input: str) -> None:
        match = re.search(r"%plan\s*([^\n]*)", user_input)
        if match:
            self._pending_goal = match.group(1).strip()

    def _reference_sections(self) -> str:
        extra = []
        try:
            rules_text = self._rules_store.render_active(
                project=self._project.name if self._project else "", limit=self._settings.rules_max_active_in_prompt)
            if rules_text:
                extra.append(rules_text)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("rules render failed: %s", exc)
        try:
            mem_text = self._memory_store.render_active(limit_chars=2500)
            if mem_text:
                extra.append(mem_text)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("memory render failed: %s", exc)
        try:
            if self._project and self._project_memory:
                sections = self._project_memory.render_context(self._project.name, max_chars=1200)
                if sections:
                    extra.append("#PROJECT PROFILE (state Diablo has inferred or been told)\n" + sections)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("project memory render failed: %s", exc)
        try:
            task_history = self.task_history_context()
            if task_history:
                extra.append("#PROJECT TASK HISTORY (read-only TaskStore summaries)\n" + task_history)
        except Exception as exc:
            log.warning("task history context failed: %s", exc)
        return "\n\n".join(extra)

    # ------------------------------------------------------------------
    # M2.12 P2 - durable execution plan
    #
    # ExecutionPlan (frozen) is validated intent. ExecutionPlanRecord is the
    # durable, mutable record persisted on TaskRecord.plan. The plan holds no
    # task identity: TaskManager.run_id / task_id stay authoritative.
    # ------------------------------------------------------------------

    @property
    def active_plan(self) -> ExecutionPlanRecord | None:
        return getattr(self, "_active_plan", None)

    def _persist_plan(self) -> None:
        """Mirror the in-flight plan onto its TaskRecord.

        Called on every step transition, so a process death always leaves a
        record on disk describing the last known step state.
        """
        store = getattr(self, "_task_store", None)
        plan = getattr(self, "_active_plan", None)
        task_id = getattr(self._tasks, "task_id", "")
        if store is None or plan is None or not task_id:
            return
        try:
            store.update(task_id, plan=plan)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist execution plan for %s: %s", task_id, exc)

    def _begin_plan(self, plan: ExecutionPlan) -> ExecutionPlanRecord | None:
        """Create and persist the durable record for a validated plan.

        Any plan already in flight is retired into ``superseded`` first, so
        re-planning on a later reasoning turn does not erase the record of
        steps that already ran.
        """
        previous = getattr(self, "_active_plan", None)
        record = ExecutionPlanRecord.from_plan(plan)
        record.absorb(previous)
        cycles = record.check_cycles()
        if cycles:
            log.warning("plan dependency cycle detected among %s", ", ".join(cycles))
            self._emit_lifecycle(
                "plan_invalid",
                reason="dependency_cycle",
                step_ids=cycles,
            )
            return None
        self._active_plan = record
        self._persist_plan()
        self._emit_lifecycle(
            "plan_created",
            goal=record.goal,
            plan_version=record.plan_version,
            steps=len(record.steps),
        )
        return record

    def _finalize_plan(self) -> None:
        """Settle dependency skips, flag abandoned steps, and persist."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return
        skipped = plan.propagate_skipped()
        if skipped:
            self._emit_lifecycle("plan_steps_skipped", step_ids=skipped)
        # A step still RUNNING here never reached a terminal state: the request
        # ended underneath it. Flag it rather than leaving it looking active, so
        # the task cannot be recorded as cleanly finished over an unresolved
        # side effect.
        abandoned = [item.step_id for item in plan.steps if item.status is StepStatus.RUNNING]
        for step in plan.steps:
            if step.status is StepStatus.RUNNING:
                step.mark_interrupted()
        if abandoned:
            log.warning("plan steps ended while still running: %s", ", ".join(abandoned))
            self._emit_lifecycle("plan_steps_abandoned", step_ids=abandoned)
        self._sync_task_metadata()
        self._persist_plan()

    def _plan_has_unresolved_steps(self) -> list[str]:
        """Ids of steps that neither succeeded nor reached a terminal state."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return []
        return [
            item.step_id
            for item in plan.steps
            if item.interrupted or item.status is StepStatus.RUNNING
        ]

    def _next_plan_step(self) -> PlanStepRecord | None:
        """Dependency-gated next executable step, or None."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return None
        return plan.next_step()

    def _plan_step_for(self, step_id: str) -> PlanStepRecord | None:
        plan = getattr(self, "_active_plan", None)
        return plan.step(step_id) if plan is not None else None

    def _annotate_step(self, step: PlanStepRecord, outcome: ExecutionOutcome | None = None) -> None:
        """Fill in the fields the existing architecture can actually supply."""
        if outcome is not None:
            data = getattr(outcome, "data", None)
            if isinstance(data, dict):
                capability_result = data.get("capability_result")
                if isinstance(capability_result, dict):
                    step.capability = str(capability_result.get("capability") or "")
        verification = getattr(outcome, "verification", None)
        if verification is not None and hasattr(verification, "state"):
            state = getattr(verification.state, "value", verification.state)
            step.verification_state = str(state or "")

    def _step_write_ahead(self, step: PlanStepRecord) -> None:
        """Persist RUNNING *before* the side effect is dispatched.

        Without this a hard kill leaves a persisted PENDING step whose side
        effect may already have applied, and any later resume would treat it as
        untouched work.
        """
        step.begin_attempt()
        self._running_plan_step = step
        self._persist_plan()
        self._emit_lifecycle(
            "plan_step_started",
            step_id=step.step_id,
            tool=step.tool,
            attempt=step.attempts,
            side_effect_key=step.side_effect_key,
        )

    def _step_finalize(
        self,
        step: PlanStepRecord,
        outcome: ExecutionOutcome,
        status: StepStatus,
    ) -> None:
        """Persist the terminal state of a step and record what happened."""
        detail = str(getattr(outcome, "reason", "") or getattr(outcome, "error", "") or "")
        summary = _plan_result_summary(outcome)
        step.finish(status, detail=detail, result_summary=summary)
        if getattr(self, "_running_plan_step", None) is step:
            self._running_plan_step = None
        # FAILED and BLOCKED both leave a step that a later resume could act on.
        # CANCELLED deliberately gets no hint: cancellation is a user decision,
        # not a condition to recover from automatically.
        if status in (StepStatus.FAILED, StepStatus.BLOCKED) and not step.recovery_hint:
            # Derived from the observed failure, never from user wording.
            step.recovery_hint = _recovery_hint_for(outcome)
        self._annotate_step(step, outcome)
        expected = self._evaluate_expected_result(step, outcome)
        if expected is not None:
            step.detail = f"{step.detail} {expected}".strip() if step.detail else expected
        self._persist_plan()
        self._emit_lifecycle(
            "plan_step_finished",
            step_id=step.step_id,
            tool=step.tool,
            status=step.status.value,
            attempts=step.attempts,
            detail=step.detail,
        )

    def _evaluate_expected_result(self, step: PlanStepRecord, outcome: ExecutionOutcome) -> str | None:
        """Honour PlanStep.expected_result.

        Supports the closed vocabulary the current verifier can actually
        evaluate. Returns a short note, or None when the step declares no
        expectation.
        """
        expected = step.expected_result
        if not isinstance(expected, dict) or not expected:
            return None
        kind = str(expected.get("type") or "")
        text = f"{getattr(outcome, 'output', '') or ''}\n{getattr(outcome, 'error', '') or ''}"
        if kind == "output_contains":
            needle = str(expected.get("value") or "")
            return f"expected output {needle!r} {'found' if needle and needle.lower() in text.lower() else 'NOT found'}"
        if kind == "output_matches":
            pattern = str(expected.get("value") or "")
            try:
                hit = bool(pattern) and re.search(pattern, text, re.I) is not None
            except re.error:
                return f"expected_result pattern {pattern!r} is not a valid regex"
            return f"expected pattern {'matched' if hit else 'did not match'}"
        if kind == "exit_code":
            want = expected.get("value")
            got = getattr(outcome, "exit_code", None)
            return f"expected exit code {want!r}, got {got!r}"
        if kind == "file_exists":
            raw = str(expected.get("path") or "")
            try:
                exists = bool(raw) and Path(raw).exists()
            except OSError:
                exists = False
            return f"expected file {raw!r} {'exists' if exists else 'is missing'}"
        return f"unsupported expected_result type {kind!r}"

    def _step_status(self, outcome: ExecutionOutcome) -> StepStatus:
        if getattr(outcome, "cancelled", False):
            return StepStatus.CANCELLED
        if getattr(outcome, "executed", False) and getattr(outcome, "success", False):
            return StepStatus.DONE
        if not getattr(outcome, "executed", False) and not getattr(outcome, "allowed", False):
            if getattr(outcome, "status", "") == "cancelled" or "Denied by user" in str(getattr(outcome, "reason", "")):
                return StepStatus.CANCELLED
            return StepStatus.BLOCKED
        return StepStatus.FAILED

    def _step_event(self, step: TaskStep, outcome: ExecutionOutcome | None = None) -> None:
        self._events.emit("task", {
            **self._event_payload(),
            "lines": self._tasks.lines(),
            "step": step.name,
            "step_status": step.status.value,
            "state": self._tasks.state.value,
            "outcome": getattr(outcome, "status", "") if outcome is not None else "",
        })

    def _record_execution(
        self,
        tool: str,
        args: dict,
        outcome: ExecutionOutcome,
        step: TaskStep | None = None,
    ) -> TaskStep:
        # The executing plan step is read from _running_plan_step rather than
        # passed in, so the durable-plan wiring does not change this method's
        # signature and existing observers/doubles keep working.
        plan_step = getattr(self, "_running_plan_step", None)
        if step is None:
            step = self._tasks.add_step(tool, step_id=plan_step.step_id if plan_step else "")
        self._tasks.set_status(step, StepStatus.RUNNING)
        self._step_event(step, outcome)
        self._persist_tool_observation(tool, outcome)
        self._note_step(tool, args, outcome)
        status = self._step_status(outcome)
        self._tasks.set_status(step, status, str(getattr(outcome, "reason", "") or ""))
        if getattr(outcome, "executed", False) and status in (StepStatus.FAILED, StepStatus.CANCELLED):
            self._tasks.invalidate_verification("A later executed operation invalidated prior verification.")
        if status in (StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.CANCELLED):
            if getattr(outcome, "executed", False) or any(s.status == StepStatus.DONE for s in self._tasks.steps):
                self._tasks.mark_partial()
            threshold = max(1, int(getattr(self._settings, "models_max_attempts_before_escalate", 1)))
            if self._tasks.recovery_attempts + 1 >= threshold:
                self._router_verify_ok = False
        if self._tasks.state not in (TaskState.FAILED, TaskState.CANCELLED, TaskState.COMPLETED):
            self._transition(TaskState.EXECUTING)
        verification = getattr(outcome, "verification", None)
        if (
            outcome.success
            and not outcome.cancelled
            and isinstance(verification, VerificationResult)
        ):
            self._begin_verification()
            self._record_verification(verification)
        self._step_event(step, outcome)
        self._sync_task_metadata()
        return step

    def _plan_step_runnable(self, step: PlanStepRecord) -> bool:
        """Dependency gate: a step runs only when its dependencies are DONE."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return False
        index = plan.by_id()
        if step.dependencies_unsatisfiable(index):
            return False
        return step.dependencies_satisfied(index)

    def _annotate_intent_expectations(self, record: ExecutionPlanRecord, intent) -> None:
        """Populate expected_result where the fast-intent contract knows it.

        These are real, pre-declared assertions the executor can evaluate
        against the observed outcome, not post-hoc narration.
        """
        name = getattr(intent, "name", "")
        if name == "create_hello_python":
            for step in record.steps:
                if step.tool == "python.run_script":
                    step.expected_result = {"type": "output_contains", "value": "Hello World"}
        elif name == "open_app":
            for step in record.steps:
                if step.tool == "applications.open":
                    step.expected_result = {"type": "output_contains", "value": "opened"}

    def _execute_step(
        self,
        tool: str,
        args: dict,
        plan_step: PlanStepRecord | None = None,
    ) -> ExecutionOutcome:
        self._transition(TaskState.EXECUTING)
        if plan_step is not None:
            # Write-ahead: the RUNNING state reaches disk before dispatch.
            self._step_write_ahead(plan_step)
        outcome = self._executor.run(tool, args, self._active_cancellation_token)
        self._record_execution(tool, args, outcome)
        if plan_step is not None:
            self._step_finalize(plan_step, outcome, self._step_status(outcome))
        return outcome

    def _begin_recovery(self, reason: str) -> None:
        self._tasks.record_recovery()
        self._transition(TaskState.RECOVERING)
        self._emit_lifecycle("recovery_started", reason=reason, attempt=self._tasks.recovery_attempts)

    def _mark_failure(self, detail: str) -> None:
        if not any(step.status in (StepStatus.FAILED, StepStatus.BLOCKED) for step in self._tasks.steps):
            step = self._tasks.add_step("execution")
            self._tasks.set_status(step, StepStatus.FAILED, detail)
            self._step_event(step)
        self._tasks.failure_reason = detail
        self._transition(TaskState.FAILED)
        self._sync_task_metadata()

    def _debug_outcome(self, tool: str, args: dict, outcome: ExecutionOutcome) -> None:
        self._record_execution(tool, args, outcome)

    def _finish_tracking(self, reply: str, status: str | None = None) -> str:
        tasks = self._tasks
        status = status or tasks.final_status()
        # A step abandoned mid-flight means the request ended over a possibly
        # applied side effect. That is not a clean completion, whatever the
        # in-memory step statuses imply.
        unresolved = self._plan_has_unresolved_steps()
        if unresolved and status == "completed":
            status = "failed"
            tasks.failure_reason = (
                f"Task ended with unresolved plan step(s): {', '.join(unresolved)}."
            )
        if status == "cancelled":
            tasks.invalidate_verification("Task was cancelled before verification completed.")
        elif status == "failed":
            if tasks.verification == VerificationState.PASSED:
                tasks.invalidate_verification("Task execution failed after earlier verification.")
            if tasks.verification != VerificationState.FAILED:
                result = VerificationResult(VerificationState.FAILED, "Execution or verification failed.", "execution")
                if tasks.state == TaskState.FAILED:
                    tasks.set_verification(result.state, result.detail)
                    self._emit_lifecycle("verification_failed", verification=result.state.value, detail=result.detail)
                else:
                    self._begin_verification()
                    self._record_verification(result)
        elif tasks.verification == VerificationState.NOT_RUN:
            result = VerificationResult(
                VerificationState.UNVERIFIED if tasks.any_executed() else VerificationState.NOT_REQUIRED,
                "No independent verifier was available.",
                "execution",
            )
            self._begin_verification()
            self._record_verification(result)
        if status == "cancelled":
            self._transition(TaskState.CANCELLED)
        elif status == "failed":
            self._transition(TaskState.FAILED)
        elif tasks.state not in (TaskState.FAILED, TaskState.CANCELLED):
            self._transition(TaskState.COMPLETED)
        self._sync_task_metadata()
        task_id = tasks.task_id
        tracker = getattr(self, "_change_tracker", None)
        persistence_ok = True
        if tracker is not None and tracker.active is not None:
            active_id = tracker.active.id
            persistence_ok = self._capture_task_git_end(active_id, reply, status)
            if not persistence_ok:
                status = "failed"
                tasks.failure_reason = "Task persistence failed."
                tasks.state = TaskState.FAILED
                tasks.set_verification(VerificationState.FAILED, tasks.failure_reason)
            if tracker.finish(reply, status) is None:
                persistence_ok = False
                status = "failed"
                tasks.failure_reason = "Coding session persistence failed."
                tasks.state = TaskState.FAILED
                tasks.set_verification(VerificationState.FAILED, tasks.failure_reason)
                try:
                    record = self._task_store.get(active_id)
                    if record is not None:
                        self._task_store.update(
                            active_id,
                            metadata={**(record.metadata or {}), "lifecycle": self._lifecycle_metadata()},
                        )
                        self._task_store.finish(active_id, tasks.failure_reason, "failed")
                except Exception as exc:
                    log.warning("could not reconcile task after tracker failure: %s", exc)
            tracker.clear_active()
            task_id = active_id
        elif self._task_store is not None and task_id:
            record = self._task_store.get(task_id)
            if record is None:
                persistence_ok = False
            else:
                try:
                    self._task_store.update(task_id, metadata={**(record.metadata or {}), "lifecycle": self._lifecycle_metadata()})
                    persistence_ok = self._task_store.finish(task_id, reply, status) is not None
                except Exception as exc:
                    log.warning("failed to persist final task %s: %s", task_id, exc)
                    persistence_ok = False
        if not persistence_ok:
            status = "failed"
            tasks.failure_reason = tasks.failure_reason or "Task persistence failed."
            self._transition(TaskState.FAILED)
            tasks.set_verification(VerificationState.FAILED, tasks.failure_reason)
        if not getattr(self, "_terminal_event_emitted", False):
            self._terminal_event_emitted = True
            event = {
                "completed": "task_completed",
                "failed": "task_failed",
                "cancelled": "task_cancelled",
            }.get(status, "task_failed")
            self._emit_lifecycle(
                event,
                status=status,
                outcome=reply,
                verification=tasks.verification.value,
                task_succeeded=tasks.task_succeeded(),
                partial=tasks.partial,
            )
        return status

    def greet_response(self) -> str:
        from core.constants import GREETING
        return GREETING

    def respond(self, user_input: str, cancellation_token: CancellationToken | object | None = None) -> str:
        """Handle one user request inside its own RequestContext.

        Admission is governed by :class:`BoundedRequestGate`. P1 ships with
        ``max_concurrent_requests = 1``, so requests are still handled one at a
        time; the gate exists so a later task can raise the limit without
        redesigning the request state.

        The gate guards genuinely *shared* Agent state — the change tracker's
        single active session, the conversation transcript,
        ``PermissionManager.last_decision`` and the ``tools.runtime_context``
        singleton. Request state itself is per-context.
        """
        gate = getattr(self, "_request_gate", None)
        if gate is None:
            gate = BoundedRequestGate(self.max_concurrent_requests)
            self._request_gate = gate
        with gate:
            return self._respond(user_input, cancellation_token)

    def _begin_request_context(self) -> RequestContext:
        """Install a fresh RequestContext for the request about to run.

        Any recovery ids discovered while reconciling interrupted tasks are
        carried over from the previous context and drained there, so they are
        still reported exactly once by the next request.
        """
        previous = self.request_context
        context = RequestContext()
        context.inherit_pending_recoveries(previous)
        self._request_context = context
        self._active_plan = None
        return context

    def _respond(self, user_input: str, cancellation_token: CancellationToken | object | None = None) -> str:
        user_input = (user_input or "").strip()
        self._begin_request_context()
        self._active_cancellation_token = cancellation_token
        self._terminal_event_emitted = False
        self._router_verify_ok = True
        self._escalations_used = 0
        self._offline_fallback_used = False
        self._tasks.begin_task(user_input)
        self._capture_plan_goal(user_input)
        self._events.emit_status("state", "thinking")
        self._emit_lifecycle("task_started", status="created", goal=user_input)
        for recovered_task_id in list(getattr(self, "_recovered_task_ids", [])):
            self._emit_lifecycle("task_recovered", recovered_task_id=recovered_task_id, status="failed")
        self._recovered_task_ids = []
        reply = ""
        try:
            self._create_run_record()
            self._transition(TaskState.PLANNING)
            self._emit_lifecycle("planning", status="planning", goal=user_input)
            if cancellation_requested(cancellation_token):
                self._transition(TaskState.CANCELLED)
                reply = "Task cancelled."
            elif self._planner.matches_greeting(user_input):
                reply = self.greet_response()
            else:
                intent = self._planner.fast_intent(user_input)
                if intent is not None:
                    reply = self._run_intent(intent)
                elif self._planner.coding_intent(user_input) == "debug":
                    reply = self._run_debug(user_input)
                else:
                    reply = self._llm_loop(user_input)
            if self._tasks.state not in (TaskState.FAILED, TaskState.CANCELLED, TaskState.COMPLETED):
                if any(step.status == StepStatus.CANCELLED for step in self._tasks.steps):
                    self._transition(TaskState.CANCELLED)
                elif self._tasks.any_failed():
                    self._transition(TaskState.FAILED)
                else:
                    if self._tasks.verification == VerificationState.NOT_RUN:
                        state = VerificationState.UNVERIFIED if self._tasks.any_executed() else VerificationState.NOT_REQUIRED
                        self._begin_verification()
                        self._record_verification(VerificationResult(state, "No independent verifier was available."))
                    self._transition(TaskState.COMPLETED)
        except CancellationRequested:
            self._transition(TaskState.CANCELLED)
            reply = "Task cancelled."
        except Exception as exc:
            log.error("Agent request failed: %s", exc, exc_info=True)
            self._mark_failure(f"{type(exc).__name__}: {exc}")
            reply = f"Task failed: {exc}"
        finally:
            self._finalize_plan()
            self._append_to_conversation(user_input, reply)
            status = self._finish_tracking(reply)
            self._events.emit_status("state", "idle")
            self._events.emit("task_done", {
                **self._event_payload(),
                "steps": self._tasks.lines(),
                "status": status,
                "outcome": reply,
                "verification": self._tasks.verification.value,
                "task_succeeded": self._tasks.task_succeeded(),
                "partial": self._tasks.partial,
                "plan": self._plan_public_summary(),
            })
            self._active_cancellation_token = None
        return reply

    def _plan_public_summary(self) -> dict | None:
        """Small plan digest for the task_done event. No args, ever."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return None
        return {
            "plan_version": plan.plan_version,
            "counts": plan.counts(),
            "is_complete": plan.is_complete,
            "interrupted_step_ids": [item.step_id for item in plan.interrupted_steps()],
        }

    def _run_intent(self, intent) -> str:
        try:
            plan = ExecutionPlan.from_intent(intent, goal=intent.summary or intent.name)
            plan.validate(self._registry)
        except (PlanValidationError, ValueError) as exc:
            self._mark_failure(f"Invalid execution plan: {exc}")
            return f"Task failed: invalid execution plan ({exc})."

        record = self._begin_plan(plan)
        if record is None:
            self._mark_failure("Invalid execution plan: dependency cycle.")
            return "Task failed: invalid execution plan (dependency cycle)."
        self._annotate_intent_expectations(record, intent)

        outcomes = []
        for step in record.steps:
            if step.status is StepStatus.SKIPPED:
                continue
            if not self._plan_step_runnable(step):
                continue
            if cancellation_requested(self._active_cancellation_token):
                self._transition(TaskState.CANCELLED)
                step.finish(StepStatus.CANCELLED, detail="Cancelled before dispatch.")
                break
            outcome = self._execute_step(step.tool, step.args, plan_step=step)
            outcomes.append(outcome)
            if self._tasks.verification == VerificationState.FAILED or self._tasks.state == TaskState.FAILED:
                break
            if not outcome.success or outcome.cancelled:
                break
        self._finalize_plan()

        intent_name = getattr(intent, "name", "")
        if intent_name == "open_app":
            if not outcomes:
                return "Opening cancelled."
            outcome = outcomes[0]
            app = intent.steps[0][1].get("app", "application")
            if outcome.success:
                return f"Opening {app}."
            if outcome.cancelled:
                return f"Opening {app} cancelled."
            return f"Could not open {app}: {outcome.error or outcome.reason}"

        if intent_name == "create_hello_python":
            path = Path(intent.steps[0][1]["path"])
            if len(outcomes) < 2:
                if outcomes and outcomes[0].cancelled:
                    return "File creation cancelled."
                return "Task stopped before verification."
            write_out, run_out = outcomes
            if not write_out.success:
                return f"Failed to create the file: {write_out.error or write_out.reason}"
            if not run_out.success:
                return f"File was created but running it failed: {run_out.error or run_out.reason}"
            self._begin_verification()
            exists, detail = self._verifier.check_file(path)
            output_ok = self._verifier.check_output(run_out.output, "Hello World")
            if exists and output_ok:
                self._record_verification(VerificationResult(VerificationState.PASSED, detail, "file_and_output"))
                return f"Created {path}, executed and verified. Output: Hello World"
            detail = detail if not exists else "Expected output was not found."
            self._record_verification(VerificationResult(VerificationState.FAILED, detail, "file_and_output"))
            return "File was created and executed but the output could not be verified."

        if intent_name == "system_info":
            outcome = outcomes[0] if outcomes else None
            if outcome is None:
                return "Could not read system information: task cancelled."
            if outcome.success:
                self._begin_verification()
                self._record_verification(VerificationResult(VerificationState.PASSED, "System information was returned.", "tool_observation"))
                return f"System check: {outcome.output.strip()}"
            return f"Could not read system information: {outcome.error or outcome.reason}"

        return "; ".join(o.display for o in outcomes) if outcomes else "Done."

    def _chat_with_fallback(self, messages: list[dict], specs: list, choice) -> object:
        try:
            return self._provider.chat(messages, tools=specs, model=choice.name)
        except Exception:
            fallback = self._router.fallback_name(exclude=choice.name)
            if not fallback or fallback == choice.name:
                from ai.offline_provider import OfflineProvider
                offline = OfflineProvider(scratch_dir=self._settings.scratch_dir)
                response = offline.chat(messages, tools=specs, model=offline.model_label)
                self._offline_fallback_used = True
                self._last_model = type("MC", (), {
                    "to_state_line": lambda self_: f"MODEL: {offline.model_label} MODE: fallback SOURCE: offline",
                    "escalated": False,
                })()
                return response
            self._begin_recovery(f"provider unavailable for {choice.name}")
            try:
                response = self._provider.chat(messages, tools=specs, model=fallback)
                self._last_model = type("MC", (), {"to_state_line": lambda self_: f"MODEL: {fallback} MODE: {choice.mode} SOURCE: provider-fallback", "escalated": False})()
                return response
            except Exception:
                from ai.offline_provider import OfflineProvider
                offline = OfflineProvider(scratch_dir=self._settings.scratch_dir)
                response = offline.chat(messages, tools=specs, model=offline.model_label)
                self._offline_fallback_used = True
                self._last_model = type("MC", (), {
                    "to_state_line": lambda self_: f"MODEL: {offline.model_label} MODE: fallback SOURCE: offline",
                    "escalated": False,
                })()
                return response

    def _llm_loop(self, user_input: str) -> str:
        specs = self._registry.all_specs()
        messages = [
            {"role": "system", "content": build_system_prompt(self._provider.model_label, self._project)},
        ]
        extra = self._reference_sections()
        if extra:
            messages[0]["content"] += "\n\n" + extra
        for prior in self._messages[-self._settings.max_conversation_turns * 2:]:
            if prior.get("role") in ("user", "assistant"):
                messages.append(prior)
        messages.append({"role": "user", "content": user_input})
        max_turns = max(1, int(getattr(self._settings, "max_turns", 4)))
        max_tool_calls = max(1, int(getattr(self._settings, "max_tool_calls", 32)))
        max_recovery_attempts = max(0, int(getattr(self._settings, "max_recovery_attempts", 3)))
        tool_call_count = 0

        for _ in range(max_turns):
            if cancellation_requested(self._active_cancellation_token):
                self._transition(TaskState.CANCELLED)
                return "Task cancelled."
            choice = self._select_model(user_input)
            try:
                response = self._chat_with_fallback(messages, specs, choice)
            except Exception as exc:
                self._mark_failure(f"AI backend error: {exc}")
                return f"AI backend error: {exc}"
            calls = list(getattr(response, "tool_calls", []) or [])
            messages.append({"role": "assistant", "content": getattr(response, "text", "") or "", "tool_calls": _to_api_calls(calls)})
            if not calls:
                if self._offline_fallback_used and self._tasks.any_executed():
                    detail = "AI backend became unavailable after tool execution; offline fallback could not verify completion."
                    self._mark_failure(detail)
                    return f"Task failed: {detail}"
                if self._tasks.state == TaskState.EXECUTING and self._tasks.verification in {
                    VerificationState.NOT_RUN,
                    VerificationState.UNVERIFIED,
                }:
                    self._begin_verification()
                    if self._tasks.verification == VerificationState.NOT_RUN:
                        state = VerificationState.UNVERIFIED if self._tasks.any_executed() else VerificationState.NOT_REQUIRED
                        self._record_verification(VerificationResult(state, "No independent verifier was available."))
                if self._tasks.state not in (TaskState.FAILED, TaskState.CANCELLED):
                    self._transition(TaskState.COMPLETED)
                return getattr(response, "text", "") or "Understood."

            try:
                plan = ExecutionPlan.from_tool_calls(calls, goal=user_input)
                plan.validate(self._registry)
            except (PlanValidationError, ValueError) as exc:
                self._mark_failure(f"Invalid execution plan: {exc}")
                return f"Task failed: invalid execution plan ({exc})."

            record = self._begin_plan(plan)
            if record is None:
                self._mark_failure("Invalid execution plan: dependency cycle.")
                return "Task failed: invalid execution plan (dependency cycle)."

            for step in record.steps:
                if step.status is StepStatus.SKIPPED:
                    continue
                if not self._plan_step_runnable(step):
                    continue
                if cancellation_requested(self._active_cancellation_token):
                    self._transition(TaskState.CANCELLED)
                    step.finish(StepStatus.CANCELLED, detail="Cancelled before dispatch.")
                    self._persist_plan()
                    return "Task cancelled."
                if tool_call_count >= max_tool_calls:
                    self._mark_failure(f"Tool call limit reached ({max_tool_calls}).")
                    return "Task stopped at the tool call limit."
                tool_call_count += 1
                outcome = self._execute_step(step.tool, step.args, plan_step=step)
                messages.append({"role": "tool", "content": f"[{step.tool}] {outcome.display}"})
                if self._tasks.verification == VerificationState.FAILED or self._tasks.state == TaskState.FAILED:
                    self._router_verify_ok = False
                    self._finalize_plan()
                    return "Task stopped because verification failed."
                if outcome.cancelled:
                    self._transition(TaskState.CANCELLED)
                    self._finalize_plan()
                    return "Task cancelled."
                if not outcome.success:
                    if not outcome.executed:
                        self._mark_failure(outcome.error or outcome.reason or "Tool was blocked.")
                        self._finalize_plan()
                        return "Task stopped because a tool was blocked."
                    if self._tasks.recovery_attempts >= max_recovery_attempts:
                        self._mark_failure(outcome.error or outcome.reason or "Tool failed repeatedly.")
                        self._finalize_plan()
                        return "Task stopped after bounded recovery attempts."
                    self._begin_recovery(outcome.error or outcome.reason or f"{step.tool} failed")
                    break
            self._finalize_plan()
            log.debug("agent loop: %d tool call(s) executed this turn", tool_call_count)

        step = self._tasks.add_step("max_turns_reached")
        self._tasks.set_status(step, StepStatus.FAILED, "Reasoning turn limit reached.")
        self._mark_failure("Reasoning turn limit reached.")
        return "Task reached the maximum number of reasoning turns. Stopping."

    def _run_debug(self, user_input: str) -> str:
        ctx = self._debug_context(user_input)
        from core.debug_loop import DebugLoop
        choice = self._select_model(user_input)
        before_steps = len(self._tasks.steps)
        loop = DebugLoop(
            lambda messages, tools=None, model=None: self._chat_with_fallback(messages, tools or [], choice),
            self._executor,
            self._settings,
            tracker=self._change_tracker,
            registry=self._registry,
            auto_verify=ctx.get("test_command", ""),
            verify_dir=str(Path(ctx["project_path"])) if ctx.get("project_path") else "",
            model_name=choice.name,
            on_outcome=self._debug_outcome,
            cancellation_token=getattr(self, "_active_cancellation_token", None),
        )
        report = loop.diagnose(user_input, ctx)
        if len(self._tasks.steps) == before_steps:
            for item in report.steps:
                step = self._tasks.add_step(item.action)
                self._tasks.set_status(step, StepStatus.DONE if item.success else StepStatus.FAILED, item.detail)
        failed_attempts = [item for item in report.steps if not item.success]
        if getattr(report, "cancelled", False):
            self._transition(TaskState.CANCELLED)
        elif getattr(self, "_offline_fallback_used", False) and self._tasks.any_executed() and not report.verified:
            self._mark_failure("AI backend fell back to offline mode after execution without independent verification.")
        elif report.exhausted:
            self._mark_failure(report.exhausted)
        elif failed_attempts and not report.verified:
            self._mark_failure("Debug execution did not produce a successful final attempt.")
        verification_state = getattr(report, "verification_state", VerificationState.UNVERIFIED.value)
        try:
            verification_state = VerificationState(verification_state)
        except ValueError:
            verification_state = VerificationState.UNVERIFIED
        if report.verified and verification_state == VerificationState.PASSED:
            self._begin_verification()
            self._record_verification(VerificationResult(VerificationState.PASSED, "Debug verification passed.", "debug"))
        elif not report.exhausted:
            self._begin_verification()
            state = VerificationState.FAILED if self._tasks.state == TaskState.FAILED else verification_state
            self._record_verification(VerificationResult(
                state,
                getattr(report, "final_message", "") or "Debug completed without independent verification.",
                "debug",
            ))
        self._router_verify_ok = bool(report.verified and not report.exhausted)
        if not report.verified and report.steps:
            log.info("debug verifier incomplete; next LLM call may escalate model")
        return report.to_context()

    def _debug_context(self, user_input: str) -> dict:
        ctx = {"mission": "Systematically diagnose the reported problem and fix it with targeted patches.",
               "goal": self._pending_goal or user_input[:80]}
        project = self._project
        if not project or not project.path:
            return ctx
        if not Path(project.path).is_dir():
            return ctx
        try:
            cache = self._settings.sessions_dir / "project-index"
            from coding.profile import build_profile
            from coding.project_index import index_cached
            from coding.relevance import pick_relevant
            profile = build_profile(Path(project.path), cache_dir=cache)
            ctx["project"] = profile.to_context(1400)
            ctx["project_name"] = profile.name
            records = index_cached(Path(project.path), cache_dir=cache)
            files = pick_relevant(Path(project.path), user_input, records=records, cache_dir=cache, max_files=6, max_lines=120)
            ctx["files"] = "\n\n".join(f.to_context() for f in files)[:4000]
            ctx["project_path"] = str(Path(project.path))
            if profile.commands.test and profile.commands.test.exists:
                ctx["test_command"] = profile.commands.test.command
                ctx["mission"] += f"\nAfter fixing, verify with: {profile.commands.test.command}"
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("debug context build failed: %s", exc)
        return ctx

    def _append_to_conversation(self, user_input: str, reply: str) -> None:
        self._messages.append({"role": "user", "content": user_input})
        self._messages.append({"role": "assistant", "content": reply})
        if self._conversation is not None:
            try:
                self._conversation.append_user(user_input)
                self._conversation.append_assistant(reply)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Could not persist conversation: %s", exc)


def _plan_result_summary(outcome: ExecutionOutcome) -> str:
    """Compact, persistence-safe record of what a step produced."""
    status = str(getattr(outcome, "status", "") or "")
    exit_code = getattr(outcome, "exit_code", None)
    output = str(getattr(outcome, "output", "") or "").strip().splitlines()
    tail = output[-1][:160] if output else ""
    parts = [p for p in (status, f"exit={exit_code}" if exit_code is not None else "", tail) if p]
    return " | ".join(parts)[:280]


def _recovery_hint_for(outcome: ExecutionOutcome) -> str:
    """Derive a resume hint from the observed failure, not from user wording."""
    error = str(getattr(outcome, "error", "") or "").strip()
    reason = str(getattr(outcome, "reason", "") or "").strip()
    if not getattr(outcome, "executed", False):
        return f"Step did not execute ({reason or error or 'blocked'}). Re-check preconditions before retrying."
    if getattr(outcome, "cancelled", False):
        return "Step was cancelled. Confirm intent before retrying."
    if exit_code := getattr(outcome, "exit_code", None):
        return f"Step failed with exit code {exit_code}. Inspect the failing command before retrying."
    if error:
        return f"Previous attempt failed: {error[:180]}"
    return f"Previous attempt failed: {reason[:180]}" if reason else "Previous attempt failed."


def _to_api_calls(calls: list[ToolCall]) -> list[dict] | None:
    if not calls:
        return None
    return [
        {"function": {"name": c.name, "arguments": _as_dict(c.arguments)}}
        for c in calls
    ]


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}