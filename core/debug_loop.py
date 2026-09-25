from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.cancellation import CancellationToken, cancellation_requested
from core.context_builder import ContextBuilder
from core.logging_setup import get_logger
from core.planner import ExecutionPlan, PlanValidationError
from core.verification import Verifier

log = get_logger("debug_loop")

_CODING_PREFIXES = ("code.", "git.", "project.", "filesystem.", "memory.", "rules.")


@dataclass
class DebugAttempt:
    action: str
    success: bool
    signature: str
    detail: str = ""


@dataclass
class DebugReport:
    issue: str
    root_cause: str = "Not determined."
    steps: list[DebugAttempt] = field(default_factory=list)
    verified: bool = False
    final_message: str = ""
    exhausted: str = ""
    verification_state: str = "unverified"
    cancelled: bool = False

    def to_context(self) -> str:
        lines = [f"Issue: {self.issue}", f"Root cause: {self.root_cause}"]
        if self.steps:
            lines.append("Attempted:")
            lines.extend(f"  {'ok ' if s.success else 'BAD'} {s.action} ({s.signature})" for s in self.steps)
        if self.verified:
            lines.append("Verification: PASSED")
        if self.exhausted:
            lines.append(f"Note: {self.exhausted}")
        return "\n".join(lines)


class DebugLoop:
    def __init__(
        self,
        llm_call,
        executor,
        settings,
        tracker=None,
        registry=None,
        verify=None,
        auto_verify: str = "",
        verify_dir: str = "",
        model_name: str = "",
        on_outcome=None,
        cancellation_token: CancellationToken | object | None = None,
    ):
        self._llm_call = llm_call
        self._executor = executor
        self._settings = settings
        self._tracker = tracker
        self._registry = registry
        self._verify = verify or Verifier()
        self._auto_verify = auto_verify
        self._verify_dir = verify_dir
        self._model_name = model_name
        self._on_outcome = on_outcome
        self._cancellation_token = cancellation_token

    def _run_tool(self, name: str, args: dict):
        if self._cancellation_token is None:
            return self._executor.run(name, args)
        return self._executor.run(name, args, self._cancellation_token)

    def _observe(self, name: str, args: dict, outcome) -> None:
        if callable(self._on_outcome):
            try:
                self._on_outcome(name, args, outcome)
            except Exception as exc:
                log.warning("debug outcome observer failed: %s", exc)
            return
        if self._tracker is None:
            return
        if self._tracker.active is None:
            if not getattr(outcome, "executed", False):
                return
            project_hint = str(getattr(self._tracker, "_debug_project", "") or "")
            self._tracker.start("Debugging session", project_hint)
        if name.startswith(_CODING_PREFIXES) or name in ("terminal.execute", "python.run_script"):
            self._tracker.step(
                name,
                args,
                bool(getattr(outcome, "allowed", True)),
                bool(getattr(outcome, "executed", True)),
                bool(getattr(outcome, "success", False)),
                str(getattr(outcome, "reason", "") or ""),
                str(getattr(outcome, "status", "") or ""),
                int(getattr(outcome, "duration_ms", 0) or 0),
                getattr(outcome, "exit_code", None),
            )

    def _run_auto_verify(self, report: DebugReport) -> DebugReport:
        command, workdir = self._auto_verify, self._verify_dir
        if not command or not command.strip() or not workdir:
            return report
        args = {"command": command, "working_dir": workdir}
        outcome = self._run_tool("terminal.execute", args)
        if getattr(outcome, "cancelled", False):
            report.cancelled = True
            report.verified = False
            report.verification_state = "unverified"
            report.final_message = "Verification cancelled."
            self._observe("terminal.execute", args, outcome)
            return report
        has_failure, sig = self._verify.check_failure(outcome.output or outcome.error or "")
        ok = bool(outcome.success and not has_failure)
        report.steps.append(DebugAttempt(
            action=f"auto-verify: {command}",
            success=ok,
            signature=sig,
            detail=(outcome.output or outcome.error or "")[:300],
        ))
        self._observe("terminal.execute", args, outcome)
        if ok:
            report.verified = True
            report.verification_state = "passed"
            report.final_message = (report.final_message or "Fix applied.") + f"\nVerification: {command} passed."
        else:
            report.verified = False
            report.verification_state = "failed"
            report.final_message = (report.final_message or "") + f"\nVerification: {command} still failing ({sig})."
        return report

    @staticmethod
    def extract_error(text: str, max_chars: int = 4000) -> str:
        if not text:
            return ""
        markers = ("Traceback", "Exception", "error:", "Error", "FAILED", "SyntaxError")
        start = len(text)
        for marker in markers:
            idx = text.lower().find(marker.lower())
            if idx != -1:
                start = min(start, idx)
        block = text[max(start - 300, 0):] if start < len(text) else text
        if len(block) > max_chars:
            block = block[:max_chars] + "...(truncated)"
        return block

    @staticmethod
    def _is_debug_tool(name: str) -> bool:
        for prefix in (
            "filesystem.read_file",
            "filesystem.list_dir",
            "project.info",
            "project.files",
            "project.grep",
            "code.patch_file",
            "git.diff",
            "git.status",
            "terminal.execute",
            "python.run_script",
        ):
            if name == prefix or name.startswith(prefix):
                return True
        return False

    def _validate_calls(self, calls: list, goal: str) -> None:
        plan = ExecutionPlan.from_tool_calls(calls, goal=goal)
        if self._registry is not None:
            plan.validate(self._registry)
            return
        for step in plan.steps:
            if not isinstance(step.tool, str) or not step.tool.strip() or not isinstance(step.args, dict):
                raise PlanValidationError("debug tool call is malformed")

    def diagnose(self, user_input: str, context: dict) -> DebugReport:
        settings = self._settings
        issue = DebugLoop.extract_error(user_input) or user_input[:200]
        report = DebugReport(issue=issue, root_cause=user_input.strip()[:120])
        builder = ContextBuilder(max_tokens=settings.context_max_tokens)
        builder.add("MISSION", context.get("mission", "Diagnose and fix the reported problem."), weight="mission")
        builder.add("PROJECT", str(context.get("project", "")), weight="project")
        builder.add("FILES", str(context.get("files", "")), weight="files")
        builder.add("ERROR", issue, weight="error")
        system = builder.build(project_label=str(context.get("project_name", "")))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        tools = [spec for spec in (self._registry.all_specs() if self._registry else [])]
        tools = [spec for spec in tools if DebugLoop._is_debug_tool(spec.name)]
        max_rounds = max(1, int(getattr(settings, "debug_max_steps", 12)))
        max_calls = max(1, int(getattr(settings, "max_tool_calls", max_rounds * 4)))
        max_failures = max(1, int(getattr(settings, "debug_max_attempts", 3)))
        max_recovery = max(0, int(getattr(settings, "max_recovery_attempts", 1)))
        total_calls = 0
        total_failures = 0
        seen: dict[str, int] = {}
        recovery_used = 0
        goal = str(context.get("goal", "") or user_input[:120])

        while True:
            for _ in range(max_rounds):
                if cancellation_requested(self._cancellation_token):
                    report.exhausted = "Cancellation requested."
                    report.cancelled = True
                    return report
                try:
                    response = self._llm_call(
                        messages,
                        tools=tools,
                        model=self._model_name or getattr(settings, "ai_model", "") or None,
                    )
                except Exception as exc:
                    report.exhausted = f"AI backend error during debugging: {exc}"
                    log.error(report.exhausted)
                    return report
                calls = list(getattr(response, "tool_calls", []) or [])
                messages.append({
                    "role": "assistant",
                    "content": getattr(response, "text", "") or "",
                    "tool_calls": None,
                })
                if not calls:
                    report.final_message = getattr(response, "text", "") or "No further actions planned by the model."
                    break
                try:
                    self._validate_calls(calls, goal)
                except (PlanValidationError, ValueError) as exc:
                    report.exhausted = f"Invalid debug execution plan: {exc}"
                    return report
                for call in calls:
                    if total_calls >= max_calls:
                        report.exhausted = f"Debug tool call limit reached ({max_calls})."
                        return report
                    if cancellation_requested(self._cancellation_token):
                        report.exhausted = "Cancellation requested."
                        report.cancelled = True
                        return report
                    args = getattr(call, "arguments", None)
                    name = getattr(call, "name", "")
                    total_calls += 1
                    try:
                        outcome = self._run_tool(name, args)
                    except Exception as exc:
                        outcome = type("O", (), {
                            "success": False,
                            "output": "",
                            "error": f"{type(exc).__name__}: {exc}",
                            "allowed": True,
                            "executed": False,
                            "status": "error",
                            "reason": str(exc),
                            "duration_ms": 0,
                            "exit_code": None,
                        })()
                    if getattr(outcome, "cancelled", False):
                        report.cancelled = True
                        report.exhausted = "Cancellation requested."
                        self._observe(name, args or {}, outcome)
                        return report
                    has_failure, signature = self._verify.check_failure(outcome.error or outcome.output or "")
                    signature = signature or (outcome.error or outcome.output or "")[:60].strip()
                    success = bool(getattr(outcome, "success", False) and not has_failure)
                    report.steps.append(DebugAttempt(
                        action=f"{name}({_brief(args or {})})",
                        success=success,
                        signature=signature,
                        detail=(getattr(outcome, "error", "") or getattr(outcome, "output", "") or "")[:300],
                    ))
                    self._observe(name, args or {}, outcome)
                    if not success:
                        total_failures += 1
                        seen[signature] = seen.get(signature, 0) + 1
                        if seen[signature] >= max_failures or total_failures >= max_failures:
                            report.exhausted = (
                                f"Repeated failure signature '{signature[:50]}' reached {max_failures} "
                                "times during this session. Stopping to avoid a loop."
                            )
                            report.final_message = report.exhausted
                            return report
                    messages.append({
                        "role": "tool",
                        "content": f"[{name}] {((getattr(outcome, 'output', '') or getattr(outcome, 'error', '')) or '')[:800]}",
                    })
                if report.exhausted:
                    return report
            if not self._auto_verify:
                report.verified = not report.exhausted and bool(report.steps) and all(s.success for s in report.steps[-1:])
                report.verification_state = "unverified"
                return report
            report = self._run_auto_verify(report)
            if report.verified or recovery_used >= max_recovery:
                if not report.verified and recovery_used >= max_recovery:
                    report.exhausted = "Debug verification recovery limit reached."
                return report
            recovery_used += 1
            messages.append({
                "role": "user",
                "content": "The verification command failed. Diagnose the failure and try a different bounded repair.",
            })


def _brief(args: dict) -> str:
    text = " ".join(f"{k}={str(v)[:60]}" for k, v in (args or {}).items())
    return text[:140]
