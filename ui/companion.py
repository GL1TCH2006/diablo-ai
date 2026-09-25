from __future__ import annotations

"""Tkinter floating companion view for the headless UI controller."""

import time
from typing import Any

from .bridge import AgentBridge
from .controller import UIController


class FloatingCompanionApp:
    """Lightweight Windows-friendly Tk view for :class:`UIController`."""

    def __init__(
        self,
        bridge: AgentBridge | None = None,
        *,
        controller: UIController | None = None,
        root: Any | None = None,
        always_on_top: bool = True,
        compact: bool = False,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self._tk = tk
        self._ttk = ttk
        self.root = root if root is not None else tk.Tk()
        self._owns_root = root is None
        self.controller = controller if controller is not None else UIController(bridge)
        self._closed = False
        self._always_on_top = False
        self._rendered_count = 0
        self._poll_job: str | None = None

        self._build_widgets(ScrolledText)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.set_always_on_top(always_on_top)
        if compact:
            self.set_compact(True)
        self._poll_job = self.root.after(50, self._poll)

    def _build_widgets(self, ScrolledText: Any) -> None:
        tk = self._tk
        ttk = self._ttk
        background = "#101216"
        panel = "#171a20"
        foreground = "#e7ebf0"
        muted = "#8b95a5"
        accent = "#63d391"
        danger = "#ef6b73"

        self.root.title("DIABLO")
        self.root.configure(bg=background)
        self.root.geometry("520x620")
        self.root.minsize(360, 180)
        self.root.resizable(True, True)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self._tk.TclError:
            pass
        style.configure("Diablo.TButton", background=panel, foreground=foreground, borderwidth=0)
        style.map("Diablo.TButton", background=[("active", "#252a33")])

        header = tk.Frame(self.root, bg=background, height=56)
        header.pack(fill="x", padx=14, pady=(12, 0))
        header.pack_propagate(False)

        title = tk.Label(header, text="DIABLO", bg=background, fg=foreground, font=("Segoe UI", 16, "bold"))
        title.pack(side="left", pady=8)

        self._status_dot = tk.Label(header, text="●", bg=background, fg=accent, font=("Segoe UI", 12))
        self._status_dot.pack(side="left", padx=(12, 4), pady=8)
        self._status_label = tk.Label(header, text="Ready", bg=background, fg=muted, font=("Segoe UI", 9))
        self._status_label.pack(side="left", pady=8)

        self._pin_button = tk.Button(
            header,
            text="Unpin",
            command=self.toggle_always_on_top,
            bg=panel,
            fg=muted,
            activebackground="#252a33",
            activeforeground=foreground,
            relief="flat",
            bd=0,
            padx=8,
        )
        self._pin_button.pack(side="right", padx=(6, 0), pady=8)
        self._expand_button = tk.Button(
            header,
            text="Compact",
            command=self.toggle_compact,
            bg=panel,
            fg=muted,
            activebackground="#252a33",
            activeforeground=foreground,
            relief="flat",
            bd=0,
            padx=8,
        )
        self._expand_button.pack(side="right", padx=6, pady=8)
        minimize_button = tk.Button(
            header,
            text="—",
            command=self.minimize,
            bg=panel,
            fg=muted,
            activebackground="#252a33",
            activeforeground=foreground,
            relief="flat",
            bd=0,
            width=3,
        )
        minimize_button.pack(side="right", padx=6, pady=8)
        close_button = tk.Button(
            header,
            text="×",
            command=self.close,
            bg=panel,
            fg=muted,
            activebackground="#252a33",
            activeforeground=foreground,
            relief="flat",
            bd=0,
            width=3,
        )
        close_button.pack(side="right", pady=8)

        self._body = tk.Frame(self.root, bg=background)
        self._body.pack(fill="both", expand=True, padx=14, pady=(10, 0))

        self._activity = ScrolledText(
            self._body,
            height=16,
            wrap="word",
            state="disabled",
            bg=panel,
            fg=foreground,
            insertbackground=foreground,
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            font=("Segoe UI", 10),
        )
        self._activity.pack(fill="both", expand=True)
        self._activity.tag_configure("user", foreground="#9cc7ff")
        self._activity.tag_configure("diablo", foreground=accent)
        self._activity.tag_configure("status", foreground=muted)
        self._activity.tag_configure("error", foreground=danger)
        self._activity.tag_configure("system", foreground="#c4cad4")

        input_frame = tk.Frame(self._body, bg=background)
        input_frame.pack(fill="x", pady=(10, 0))
        self._input = tk.Text(
            input_frame,
            height=3,
            wrap="word",
            bg=panel,
            fg=foreground,
            insertbackground=foreground,
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 10),
        )
        self._input.pack(side="left", fill="x", expand=True)
        self._input.bind("<Return>", self._on_return)
        self._input.bind("<Shift-Return>", self._on_shift_return)
        send_button = tk.Button(
            input_frame,
            text="Send",
            command=self.send_current_message,
            bg="#234d3b",
            fg=foreground,
            activebackground="#2d674e",
            activeforeground=foreground,
            relief="flat",
            bd=0,
            padx=14,
        )
        send_button.pack(side="right", padx=(8, 0))

        self._status_bar = tk.Label(
            self.root,
            text="Ready",
            anchor="w",
            bg=background,
            fg=muted,
            padx=16,
            pady=8,
            font=("Segoe UI", 9),
        )
        self._status_bar.pack(fill="x", side="bottom")
        self._error_color = danger

    def _on_return(self, event: Any) -> str | None:
        if int(getattr(event, "state", 0)) & 0x0001:
            return None
        self.send_current_message()
        return "break"

    def _on_shift_return(self, event: Any) -> None:
        return None

    def _poll(self) -> None:
        if self._closed:
            return
        self.controller.pump_events()
        self._render()
        self._poll_job = self.root.after(50, self._poll)

    def _render(self) -> None:
        state = self.controller.state
        color = {
            "ready": "#63d391",
            "idle": "#8b95a5",
            "thinking": "#f2c94c",
            "executing": "#63a8ff",
            "verifying": "#b28cff",
            "success": "#63d391",
            "error": self._error_color,
            "closed": "#8b95a5",
        }.get(state.value, "#8b95a5")
        self._status_dot.configure(fg=color)
        self._status_label.configure(text=state.label, fg=color)
        self._status_bar.configure(text=f"{state.label}  |  Diablo companion", fg=color)

        activities = self.controller.activities
        if self._rendered_count >= len(activities):
            return
        self._activity.configure(state="normal")
        for activity in activities[self._rendered_count :]:
            timestamp = time.strftime("%H:%M:%S", time.localtime(activity.timestamp))
            role = activity.role if activity.role in {"user", "diablo", "status", "error", "system"} else "system"
            self._activity.insert("end", f"{timestamp}  {role.upper()}: {activity.text}\n\n", role)
        self._rendered_count = len(activities)
        self._activity.see("end")
        self._activity.configure(state="disabled")

    def send_current_message(self) -> None:
        text = self._input.get("1.0", "end-1c")
        result = self.controller.send_message(text)
        if result.success:
            self._input.delete("1.0", "end")
        self._render()

    def set_always_on_top(self, enabled: bool) -> bool:
        self._always_on_top = bool(enabled)
        try:
            self.root.attributes("-topmost", self._always_on_top)
        except self._tk.TclError:
            self._always_on_top = False
        if hasattr(self, "_pin_button"):
            self._pin_button.configure(text="Unpin" if self._always_on_top else "Pin")
        return self._always_on_top

    def toggle_always_on_top(self) -> bool:
        return self.set_always_on_top(not self._always_on_top)

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    def set_compact(self, compact: bool) -> bool:
        compact = bool(compact)
        self.controller.set_compact(compact)
        if hasattr(self, "_expand_button"):
            self._expand_button.configure(text="Expand" if compact else "Compact")
        if compact:
            self._body.pack_forget()
            self.root.geometry("360x92")
        else:
            self._body.pack(fill="both", expand=True, padx=14, pady=(10, 0))
            self.root.geometry("520x620")
        self._render()
        return compact

    def toggle_compact(self) -> bool:
        return self.set_compact(not self.controller.compact)

    def minimize(self) -> None:
        try:
            self.root.iconify()
        except self._tk.TclError:
            pass

    def focus(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except self._tk.TclError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except self._tk.TclError:
                pass
            self._poll_job = None
        self.controller.close()
        if self._owns_root:
            try:
                self.root.destroy()
            except self._tk.TclError:
                pass

    def run(self) -> None:
        self.root.mainloop()


def create_companion_app(
    bridge: AgentBridge | None = None,
    *,
    controller: UIController | None = None,
    root: Any | None = None,
    always_on_top: bool = True,
    compact: bool = False,
) -> FloatingCompanionApp:
    """Create the floating companion without starting its event loop."""
    return FloatingCompanionApp(
        bridge,
        controller=controller,
        root=root,
        always_on_top=always_on_top,
        compact=compact,
    )


def run_companion(bridge: AgentBridge | None = None, **kwargs: Any) -> None:
    """Create and run the companion until its window is closed."""
    app = create_companion_app(bridge, **kwargs)
    app.run()


__all__ = [
    "FloatingCompanionApp",
    "create_companion_app",
    "run_companion",
]
