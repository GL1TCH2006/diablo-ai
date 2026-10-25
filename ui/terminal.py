from __future__ import annotations

import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from config.permissions import PermissionLevel
from core.constants import APP_NAME, APP_VERSION
from core.events import EventBus
from core.logging_setup import get_logger
from personas import DEFAULT_PERSONA, format_status, load_persona, select_persona

log = get_logger("ui")


class TerminalUI:
    def __init__(
        self,
        agent,
        bus: EventBus,
        model_label: str,
        provider_state: str,
        blender_desc: str,
        voice_enabled: bool,
        project_label: str = "(none)",
    ):
        self._agent = agent
        self._bus = bus
        self._model_label = model_label
        self._provider_state = provider_state
        self._blender_desc = blender_desc
        self._voice_enabled = voice_enabled
        self._project_label = project_label
        self._console = Console(file=sys.stdout)
        bus.subscribe("tool", self._on_tool)
        bus.subscribe("status", self._on_status)
        bus.subscribe("task_done", self._on_task_done)
        # Persona mode: "auto", "sage", "raphael", "off"
        self._persona_mode: str = "auto"

    def _on_tool(self, data: dict) -> None:
        status = data.get("status")
        if status in ("running", "pending_confirmation"):
            self._console.print(Text(f"  -> {data.get('tool')} {status.replace('_', ' ')}", style="yellow"))
        elif status in ("blocked", "denied"):
            self._console.print(Text(f"  x {data.get('tool')}: {data.get('detail', '')}", style="red"))
        elif status == "error":
            self._console.print(Text(f"  x {data.get('tool')}: {data.get('detail', '')}", style="red"))

    def _on_status(self, data: dict) -> None:
        pass

    def _on_task_done(self, data: dict) -> None:
        self._console.print(Text("  Task complete.", style="green"))

    def run(self) -> None:
        self._header()
        self._help_short()
        while True:
            try:
                raw = Prompt.ask("[bold cyan]You[/]")
            except (EOFError, KeyboardInterrupt):
                self._console.print("\n[dim]Diablo signing off.[/]")
                return
            command = raw.strip()
            if not command:
                continue
            if command.startswith(":"):
                if self._handle_command(command):
                    return
                continue
            self._answer(command)

    def _handle_persona_command(self, arg: str) -> None:
        arg = arg.strip().lower()
        valid_modes = ("sage", "raphael", "auto", "off")
        if not arg:
            self._console.print(f"Current persona mode: {self._persona_mode}")
            self._console.print(f"Valid modes: {', '.join(valid_modes)}")
            return
        if arg not in valid_modes:
            self._console.print(f"[red]Unknown persona mode: {arg}[/]")
            self._console.print(f"Valid modes: {', '.join(valid_modes)}")
            return
        self._persona_mode = arg
        self._console.print(f"[green]Persona mode set to: {arg}[/]")

    def _determine_outcome(self) -> str:
        """Determine the authoritative outcome from the agent's task state."""
        task = getattr(self._agent, "_tasks", None)
        if task is None:
            return "unverified"
        state = getattr(task, "state", None)
        verification = getattr(task, "verification", None)
        from core.task_manager import TaskState
        from core.verification import VerificationState
        if state is TaskState.COMPLETED:
            if verification is VerificationState.PASSED:
                return "completed"
            return "unverified"
        if state is TaskState.FAILED:
            return "failed"
        if state is TaskState.CANCELLED:
            return "cancelled"
        if state is TaskState.AWAITING_PERMISSION:
            return "approval_required"
        return "unverified"

    def _apply_persona_formatting(self, reply: str, outcome: str) -> str:
        if self._persona_mode == "off":
            return reply
        if self._persona_mode == "auto":
            # Use selector on the original user input to detect explicit persona request
            # This is a best-effort; if no alias matched, format_status will use DEFAULT_PERSONA
            persona = select_persona("")
        else:
            try:
                persona = load_persona(self._persona_mode)
            except Exception:
                return reply
        try:
            return format_status(persona, outcome, reply)
        except Exception:
            return reply

    def _answer(self, text: str) -> None:
        self._console.print(f"[bold]You:[/] {text}")
        try:
            reply = self._agent.respond(text)
        except Exception as exc:
            log.error("Agent response failed: %s", exc, exc_info=True)
            reply = f"Diablo hit an internal error: {exc}"
        outcome = self._determine_outcome()
        formatted_reply = self._apply_persona_formatting(reply, outcome)
        self._console.print(Text(f"{APP_NAME}: {formatted_reply}", style="bold bright_green"))
        if self._voice_enabled:
            self._speak(formatted_reply)

    def _speak(self, reply: str) -> None:
        """Speak a reply, if TTS is genuinely available.

        M2.14.1: this used to call ``tts.speak`` unconditionally whenever voice
        was enabled, with no availability check and no error handling. A machine
        without SAPI voices, or a PowerShell failure, silently did nothing. It now
        probes once and reports the state honestly, and never lets a voice
        failure affect the turn the user actually asked for.
        """
        try:
            from voice.capabilities import voice_status

            status = self._voice_status() or voice_status()
            if not status.tts_available:
                log.info("TTS requested but unavailable: %s", status.notes or "no backend")
                return
            import voice.text_to_speech as tts
            tts.speak(reply)
        except Exception as exc:  # noqa: BLE001 - speech must never break a turn
            log.warning("TTS failed: %s", exc)

    def _voice_status(self):
        cached = getattr(self, "_voice_status_cache", None)
        if cached is None:
            from voice.capabilities import voice_status as _probe
            cached = _probe()
            self._voice_status_cache = cached
        return cached

    def _header(self) -> None:
        title = Text()
        title.append(f"{APP_NAME}", style="bold bright_red")
        title.append(f"  v{APP_VERSION}", style="dim")
        body = Text()
        body.append(f"Model       : {self._model_label}\n")
        body.append(f"Provider    : {self._provider_state}\n")
        body.append(f"Blender     : {self._blender_desc}\n")
        status = self._voice_status()
        body.append(f"Voice       : {status.summary()}\n")
        body.append(f"Project     : {self._project_label}", style="cyan")
        self._console.print(Panel(body, title=title, border_style="bright_red", width=82, title_align="left"))

    def _help_short(self) -> None:
        self._console.print(
            "[dim]Type a message for Diablo, or a command: :model  :memory  :prefs  :projects  :wipe  :help  :persona  :quit[/]",
        )

    def confirmer(self, tool: str, description: str) -> str:
        panel = Panel(
            Text(f"Description : {description}\nTool        : {tool}\nLevel       : {PermissionLevel.SENSITIVE}", style="yellow"),
            title="Diablo needs your confirmation",
            border_style="yellow",
            width=82,
        )
        self._console.print(panel)
        while True:
            choice = Prompt.ask("[yellow]Run it?[/] [bold](y)[/]es  [bold](n)[/]o  [bold](a)[/]lways  [bold](b)[/]lock", default="n")
            c = choice.strip().lower()[0] if choice.strip() else "n"
            if c == "y":
                return "allow"
            if c == "n":
                return "deny"
            if c == "a":
                return "always"
            if c == "b":
                return "block"

    def _handle_command(self, command: str) -> bool:
        cmd, _, rest = command.partition(" ")
        cmd = cmd.lower()
        if cmd in (":quit", ":exit", ":q"):
            self._console.print("[dim]Diablo signing off.[/]")
            return True
        if cmd == ":help":
            self._help_text()
            return False
        if cmd == ":persona":
            self._handle_persona_command(rest.strip())
            return False
        if cmd in (":memory", ":mem"):
            store = getattr(self._agent, "_conversation", None)
            if store:
                self._console.print("[dim]last conversation entries[/]")
                for entry in store.recent(5)[-10:]:
                    self._console.print(f"  [{entry.get('role')}] {str(entry.get('content'))[:120]}")
            else:
                self._console.print("No conversation store available.")
            return False
        if cmd == ":prefs":
            prefs = getattr(self._agent, "_preferences", None)
            if prefs:
                from rich.json import JSON
                self._console.print(JSON.from_data(prefs.all()))
            return False
        if cmd == ":wipe":
            if Prompt.ask("Delete all stored memory and rules? [y/n]", default="n").lower().startswith("y"):
                # Agent.wipe_user_data() is the authoritative implementation.
                # This used to clear only _conversation and _preferences, so
                # memory entries and rules survived a command the UI described
                # as deleting all stored memory - which mattered more once the
                # memory layer started refusing credentials, because entries
                # written before that policy existed still held them.
                wipe = getattr(self._agent, "wipe_user_data", None)
                if not callable(wipe):
                    self._console.print("[yellow]This build cannot wipe stored data.[/]")
                    return False
                removed = wipe()
                parts = [f"{label} {count}" for label, count in removed.items() if count]
                detail = f" ({', '.join(parts)})" if parts else " - nothing was stored"
                self._console.print(f"[green]Memory, rules and conversation cleared{detail}.[/]")
            return False
        if cmd in (":projects", ":project"):
            memory = getattr(self._agent, "_project_memory", None)
            active = getattr(self._agent, "_project", None)
            if memory and active:
                self._console.print(f"Active: {active.name} at {active.path}")
                for snap in memory.discover():
                    mark = "* " if snap.name == active.name else "  "
                    self._console.print(f"{mark}{snap.name} ({snap.language}, git={snap.git_repo}) {snap.path}")
            return False
        if cmd in (":mission", ":ms"):
            if hasattr(self._agent, "session_summary"):
                self._console.print(self._agent.session_summary())
            else:
                self._console.print("No active coding session.")
            return False
        if cmd in (":rules", ":r"):
            if hasattr(self._agent, "rules_and_conflicts"):
                self._console.print(self._agent.rules_and_conflicts())
            else:
                self._console.print("No rules system available.")
            return False
        if cmd == ":why":
            if hasattr(self._agent, "why_summary"):
                self._console.print(self._agent.why_summary())
            else:
                self._console.print("No decision recorder available.")
            return False
        if cmd in (":gitdiff", ":gd"):
            if hasattr(self._agent, "git_diff_summary"):
                self._console.print(self._agent.git_diff_summary())
            else:
                self._console.print("No git diff available.")
            return False
        if cmd == ":model":
            if hasattr(self._agent, "model_status"):
                self._console.print(self._agent.model_status())
            else:
                self._console.print(f"  Model: {self._model_label}")
                self._console.print(f"  Provider state: {self._provider_state}")
            return False
        return False

    def _help_text(self) -> None:
        self._console.print(
            "\n".join(
                [
                    ":model     show AI model / provider status",
                    ":memory    show recent conversation",
                    ":prefs     show stored preferences",
                    ":projects  discover workspace projects",
                    ":wipe      delete stored memory, rules and conversation",
                    ":mission   show status of the active coding session",
                    ":gitdiff   show uncommitted changes in the active project",
                    ":rules     show active restrict-only rules + conflicts",
                    ":why       explain the last permission decision",
                    ":persona   set response style: sage, raphael, auto, off",
                    ":quit      exit Diablo",
                ]
            )
        )