"""M2.16.UI - long-run stability harness for the edge companion (Part 30).

Runs the real companion on the real desktop, in a real Tk mainloop, for a
configured duration while repeatedly waking it, submitting tasks, cancelling some
of them and letting them complete. Samples CPU time, working set, thread count,
visible window count, timer count and companion history size throughout, then
reports whether any of them drifted.

Why a real mainloop
-------------------
Timer and handler accumulation is invisible to a test that calls ``pump_once()``,
because that path deliberately schedules nothing. The leak this is looking for
only appears when ``after`` callbacks actually fire and reschedule themselves, so
the harness uses ``app.run()`` and drives the companion with ``root.after``.

Run it directly::

    python tests/soak_edge_companion.py --minutes 30

Not collected by pytest: the filename does not begin with ``test_``. The bounded
version of this - a handful of cycles with leak assertions - *is* a test, in
``tests/test_m216_ui_edge_real.py``.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from typing import Any


# ── process metrics ──────────────────────────────────────────────────────


class _ProcessCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def working_set_bytes() -> int:
    """Current process working set, or 0 if the platform will not say.

    Tries ``K32GetProcessMemoryInfo`` first and falls back to the older
    ``psapi`` export. Without the fallback this silently returned 0 on this
    machine, which would have made the memory-growth check vacuous.
    """
    from ctypes import wintypes

    counters = _ProcessCounters()
    counters.cb = ctypes.sizeof(_ProcessCounters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    for dll_name, func_name in (("kernel32", "K32GetProcessMemoryInfo"),
                                ("psapi", "GetProcessMemoryInfo")):
        try:
            dll = getattr(ctypes.windll, dll_name)
            func = getattr(dll, func_name)
        except (AttributeError, OSError):
            continue
        func.restype = wintypes.BOOL
        func.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if func(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    return 0


def cpu_seconds() -> float:
    """Process CPU time across all threads, in seconds.

    Uses :func:`time.process_time`, which CPython implements natively. A direct
    ``GetProcessTimes`` call was tried first and reported success while returning
    zero kernel and user times on this machine, which would have made the CPU
    column of the report silently meaningless.
    """
    return float(time.process_time())


def visible_window_count() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    count = wintypes.DWORD(0)
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            count.value += 1
        return True

    user32.EnumWindows(proto(visit), 0)
    return int(count.value)


# ── scripted backend ─────────────────────────────────────────────────────


class SoakBridge:
    """A backend that answers on the Tk thread, mimicking real progress timing."""

    def __init__(self) -> None:
        self.submissions = 0
        self.cancellations = 0
        self.unsubscribes = 0
        self.active = False
        self._handler: Any = None
        self._pending: list[Any] = []

    def subscribe_to_events(self, handler: Any) -> Any:
        self._handler = handler

        def _unsubscribe() -> None:
            self.unsubscribes += 1
            self._handler = None

        return _unsubscribe

    def submit_message(self, text: str, task_id: str = "") -> dict:
        self.submissions += 1
        self.active = True
        self._pending = [
            ("step", "Opening Calculator", "desktop.open"),
            ("verification", "Verifying result", "desktop.verify"),
        ]
        return {"success": True}

    def cancel_task(self, task_id: str = "") -> bool:
        self.cancellations += 1
        self._pending = []
        self.active = False
        return True

    def deliver(self, root: Any, task_id: str) -> None:
        """Flush one scripted event on the next tick, then finish the task."""
        def step() -> None:
            if self._handler is None or not self._pending:
                return
            kind, message, tool = self._pending.pop(0)
            self._handler({"type": kind, "message": message, "task_id": task_id,
                           "tool": tool, "tool_status": "running"})
            root.after(120, step)

        root.after(120, step)

    def finish(self, root: Any, task_id: str, *, cancelled: bool) -> None:
        def done() -> None:
            if self._handler is None:
                return
            self._handler({
                "type": "task_cancelled" if cancelled else "task_completed",
                "message": "Stopped." if cancelled else "Calculator opened and verified.",
                "task_id": task_id,
            })
            self.active = False

        root.after(150, done)


# ── harness ──────────────────────────────────────────────────────────────


class SoakRunner:
    def __init__(self, minutes: float = 30.0) -> None:
        self.minutes = float(minutes)
        self.samples: list[dict[str, float]] = []
        self.cycles = 0
        self.errors: list[str] = []
        # Mirrors the controller's own bounds; the report checks against these
        # rather than against the first sample, because filling to the cap is the
        # documented behaviour rather than a leak.
        self.history_cap_progress = 40
        self.history_cap_events = 160

    def run(self) -> int:
        import tkinter as tk

        from core.windows.displays import DisplayLayer
        from ui.companion_state import CompanionState
        from ui.edge_companion import EdgeCompanionApp, active_edge_companion_count

        display_layer = DisplayLayer()
        root = tk.Tk()
        root.withdraw()
        window = tk.Toplevel(root)

        bridge = SoakBridge()
        from ui.companion_state import CompanionController

        controller = CompanionController(bridge, peek_seconds=0.28,
                                        dwell_seconds=3.0, max_progress=40)
        controller.attach()
        app = EdgeCompanionApp(controller, display_layer=display_layer, root=window)

        deadline = time.monotonic() + self.minutes * 60.0
        started_wall = time.time()

        def sample() -> None:
            try:
                self.samples.append({
                    "t": time.monotonic(),
                    "rss": working_set_bytes(),
                    "cpu": cpu_seconds(),
                    "threads": float(threading.active_count()),
                    "windows": float(visible_window_count()),
                    "timers": float(len(app.root.after_info())),
                    "events": float(len(controller.snapshot().events)),
                    "progress": float(len(controller.snapshot().progress)),
                    "companions": float(active_edge_companion_count()),
                })
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"sample: {exc}")
            if time.monotonic() < deadline:
                root.after(5000, sample)
            else:
                finish()

        def drive() -> None:
            """One full wake -> task -> (maybe cancel) -> result -> sleep cycle."""
            self.cycles += 1
            try:
                app._on_activate_click()
                task_id = controller.submit_task("Open Calculator")
                bridge.deliver(app.root, task_id)
                cancel_this = self.cycles % 3 == 0
                if cancel_this:
                    app._on_stop()
                    bridge.finish(app.root, task_id, cancelled=True)
                else:
                    bridge.finish(app.root, task_id, cancelled=False)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"cycle {self.cycles}: {exc}")
            if time.monotonic() < deadline:
                app.root.after(3000, drive)
            else:
                finish()

        finished = {"done": False}

        def finish() -> None:
            if finished["done"]:
                return
            finished["done"] = True
            final_state = controller.state.value
            try:
                app.close()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"close: {exc}")
            self.final_state = final_state
            self.final_bridges_closed = bridge.unsubscribes
            self.final_companions = active_edge_companion_count()
            try:
                display_layer.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

        self.final_state = "unknown"
        self.final_bridges_closed = -1
        self.final_companions = -1

        app.root.after(1000, sample)
        app.root.after(1500, drive)
        print(f"[soak] running {self.minutes:.1f} min; started "
              f"{time.strftime('%H:%M:%S', time.localtime(started_wall))}")
        try:
            app.run()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"run: {exc}")
        return self.report()

    def report(self) -> int:
        print()
        print("=" * 68)
        print("M2.16.UI EDGE COMPANION SOAK REPORT")
        print("=" * 68)
        if not self.samples:
            # A harness that collected nothing must say why rather than reporting
            # an empty table that reads like a clean run.
            print(f"NO SAMPLES COLLECTED ({len(self.errors)} errors)")
            for line in self.errors[:10]:
                print(f"  - {line}")
            return 1
        first, last = self.samples[0], self.samples[-1]
        elapsed = last["t"] - first["t"]
        cpu_used = last["cpu"] - first["cpu"]
        wall = max(elapsed, 1e-9)

        def mb(value: float) -> float:
            return value / (1024.0 * 1024.0)

        print(f"duration              {elapsed / 60.0:.1f} min")
        print(f"wake/task cycles      {self.cycles}")
        print(f"samples               {len(self.samples)}")
        print(f"cpu time used         {cpu_used:.2f} s ({100.0 * cpu_used / wall:.1f}% of wall)")
        print(f"working set           {mb(first['rss']):.1f} -> {mb(last['rss']):.1f} MB "
              f"(delta {mb(last['rss'] - first['rss']):+.1f})")
        print(f"threads               {first['threads']:.0f} -> {last['threads']:.0f}")
        print(f"visible windows       {first['windows']:.0f} -> {last['windows']:.0f}")
        print(f"pending tk timers     {first['timers']:.0f} -> {last['timers']:.0f}")
        print(f"companion event hist  {first['events']:.0f} -> {last['events']:.0f}")
        print(f"companion progress    {first['progress']:.0f} -> {last['progress']:.0f}")
        print(f"companion windows     {first['companions']:.0f} -> {last['companions']:.0f}")
        print(f"final state           {self.final_state}")
        print(f"bridge unsubscribes   {self.final_bridges_closed}")
        print(f"companions after exit {self.final_companions}")
        if self.errors:
            print(f"errors ({len(self.errors)}):")
            for line in self.errors[:10]:
                print(f"  - {line}")

        failures: list[str] = []
        if self.errors:
            failures.append(f"{len(self.errors)} runtime errors")
        if last["threads"] > first["threads"] + 2:
            failures.append(f"thread growth {first['threads']:.0f}->{last['threads']:.0f}")
        if last["timers"] > first["timers"] + 4:
            failures.append(f"timer growth {first['timers']:.0f}->{last['timers']:.0f}")
        # The histories are *bounded by design* (max_progress and max_progress * 4),
        # so filling to their cap over a long run is correct, not a leak. The check
        # is that they stayed under the cap the controller promises.
        if last["events"] > self.history_cap_events + 1:
            failures.append(f"event history exceeded its bound: {last['events']:.0f}")
        if last["progress"] > self.history_cap_progress + 1:
            failures.append(f"progress exceeded its bound: {last['progress']:.0f}")
        if last["companions"] != 1:
            failures.append(f"companion windows != 1 ({last['companions']:.0f})")
        if self.final_bridges_closed != 1:
            failures.append(f"bridge unsubscribes != 1 ({self.final_bridges_closed})")
        if self.final_companions != 0:
            failures.append("companion window survived shutdown")
        if last["rss"] == 0:
            print("NOTE: working set unavailable on this platform; "
                  "memory growth was not checked")
        elif last["rss"] - first["rss"] > 25 * 1024 * 1024:
            failures.append(f"memory growth {mb(last['rss'] - first['rss']):+.1f} MB")
        # Timer granularity means a run can finish a moment short of its target.
        if elapsed < self.minutes * 60.0 - 5.0:
            failures.append(f"ran only {elapsed / 60.0:.1f} of {self.minutes:.1f} min")

        print("-" * 68)
        if failures:
            for line in failures:
                print(f"FAIL: {line}")
            return 1
        print("SOAK PASS: no leak, no growth, clean shutdown")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30.0,
                        help="how long to soak (default 30)")
    args = parser.parse_args(argv)
    return SoakRunner(args.minutes).run()


if __name__ == "__main__":
    sys.exit(main())