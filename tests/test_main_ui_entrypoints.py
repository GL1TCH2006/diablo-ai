"""M2.16.UI - the two ``main.py`` companion entry-point defects, pinned.

Both bugs lived in the *wiring* between ``main.py`` and the companion launchers,
and both sat in a gap that no existing test covered: ``test_m216_ui_edge_view.py``
builds an ``EdgeCompanionApp`` directly with a fake display layer, so it proves the
placement logic and never proves that ``main.py`` supplies one. These tests close
that gap from the other side, by driving ``main.main()`` itself.

No Tk window is created anywhere in this module. ``main.py`` reaches Tk only
through ``run_edge_companion``, which is replaced with a recording stub, and the
placement assertions run against ``EdgeCompanionApp``'s own methods with a fake
root. Nothing here needs a desktop session, so the whole module is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import main as diablo_main


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• doubles (no Tk, no OS)


class _FakeBackend:
    """Stands in for ``ai.model_manager.build_backend``.

    The real one probes ``http://localhost:11434``. This suite must never touch a
    model or a socket, so the backend is fabricated here instead.
    """

    def __init__(self):
        from ai.model_manager import BackendResult
        from ai.offline_provider import OfflineProvider

        self._result = BackendResult(
            provider=OfflineProvider(scratch_dir=Path(".")),
            model_name="offline/rule-based",
            note="stubbed for test",
            models=[],
        )

    def __getattr__(self, name):
        return getattr(self._result, name)


class _FakeBlenderController:
    """``main.py`` calls ``BlenderController().describe()`` before branching.

    The real controller shells out to ``blender --version``, which this suite is
    forbidden to do, so detection is stubbed out entirely.
    """

    def describe(self) -> str:
        return "Blender: not probed (stubbed)"


class _RecordingTerminalUI:
    """Replaces ``ui.terminal.TerminalUI`` and records that it ran."""

    instances: list["_RecordingTerminalUI"] = []

    def __init__(self, *args, **kwargs):
        self.ran = False
        self.kwargs = kwargs
        _RecordingTerminalUI.instances.append(self)

    def confirmer(self, tool, description):
        return "allow"

    def run(self) -> None:
        self.ran = True


class _FakeTclError(Exception):
    pass


class _FakeTk:
    """Only ``TclError`` is needed; nothing here builds a real widget."""

    TclError = _FakeTclError


class _FakeRoot:
    """A window stand-in that records geometry requests.

    ``EdgeCompanionApp._apply_placement`` only ever calls ``geometry``,
    ``attributes`` and ``update_idletasks`` on the root, plus
    ``winfo_pointerxy``. None of that needs Tk.
    """

    def __init__(self, pointer=(0, 0)):
        self.geometries: list[str] = []
        self.attributes_calls: list[tuple] = []
        self.idletasks = 0
        self._pointer = pointer

    def geometry(self, value):
        self.geometries.append(value)

    def attributes(self, *args):
        self.attributes_calls.append(args)

    def update_idletasks(self):
        self.idletasks += 1

    def winfo_pointerxy(self):
        return self._pointer


def _fake_display(display_id, bounds, scale_percent=100, primary=False):
    """A real ``DisplayInfo``, built without enumerating anything.

    Only the *layer* is faked. The value objects are the production dataclasses, so
    the placement math runs against exactly the types it sees in production -
    re-implementing them here would let this test pass while production broke.
    """
    from core.windows.coordinates import Rect
    from core.windows.displays import DisplayIdentity, DisplayInfo

    rect = Rect(*bounds)
    return DisplayInfo(
        identity=DisplayIdentity(display_id),
        bounds=rect,
        work_area=rect,
        primary=primary,
        dpi_x=96,
        dpi_y=96,
        scale_percent=scale_percent,
        orientation=None,
    )


class _FakeDisplayLayer:
    def __init__(self, displays):
        self._displays = list(displays)
        self.calls = 0

    def list_displays(self):
        self.calls += 1
        return list(self._displays)


class _FakeCompanionApp:
    """The object ``launch_edge_companion`` returns.

    ``run_edge_companion`` calls ``run()`` and the runtime's ``__exit__`` calls
    ``close()``; neither touches Tk here.
    """

    def __init__(self):
        self.ran = False
        self.closed = False

    def run(self):
        self.ran = True
        return 0

    def close(self):
        self.closed = True


class _FakeSnapshot:
    peek = 1.0


class _FakeController:
    def snapshot(self):
        return _FakeSnapshot()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• the main() harness


@pytest.fixture()
def main_env(monkeypatch, tmp_path, capsys):
    """Stub every side effect ``main.main()`` reaches before the UI branch.

    Each patch removes one external dependency - a socket, a workspace scan, a
    Blender probe, the capability runtime, a log file - so the only thing left
    running is the branch under test.
    """
    from config.settings import Settings
    import config.settings as settings_module

    root = tmp_path.resolve()
    settings = Settings(
        base_dir=root,
        ai_provider="ollama",
        ollama_host="http://localhost:11434",
        ai_model="",
        workspace_root=root / "projects",
        data_dir=root / "data",
        memory_dir=root / "data" / "memory",
        logs_dir=root / "data" / "logs",
        backups_dir=root / "data" / "backups",
        scratch_dir=root / "workspace",
        sessions_dir=root / "data" / "sessions",
        voice_enabled=False,
        log_level="INFO",
        max_turns=4,
        max_conversation_turns=20,
        allowed_roots=[root],
        raw={},
    )
    for directory in (settings.data_dir, settings.memory_dir, settings.logs_dir,
                      settings.backups_dir, settings.scratch_dir, settings.workspace_root):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings_module, "load_settings", lambda: settings)

    import core.logging_setup as logging_setup
    import logging as _logging
    monkeypatch.setattr(logging_setup, "setup_logging",
                        lambda *a, **kw: _logging.getLogger("diablo.test"))

    import ai.model_manager as model_manager
    monkeypatch.setattr(model_manager, "build_backend",
                        lambda settings, force_offline=False: _FakeBackend())

    # (capability_registry, integration, blender, workflow, windows). A real but
    # empty CapabilityRegistry, because ``build_registry`` calls
    # ``registry.capabilities()`` and ``registry.get()`` while building
    # ``capability.execute`` and the desktop tools. The windows slot is a truthy
    # marker because main.py only builds a DisplayLayer when it is present.
    from integration import CapabilityRegistry

    monkeypatch.setattr(diablo_main, "build_capability_runtime",
                        lambda *a, **kw: (CapabilityRegistry(), None, None, None, object()))

    import blender.controller as blender_controller
    monkeypatch.setattr(blender_controller, "BlenderController",
                        lambda *a, **kw: _FakeBlenderController())

    import ui.terminal as terminal_module
    monkeypatch.setattr(terminal_module, "TerminalUI", _RecordingTerminalUI)

    _RecordingTerminalUI.instances = []

    return settings


def _run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return diablo_main.main()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• defect (a): NameError


def test_edge_companion_unavailable_falls_back_to_console_ui(main_env, monkeypatch, capsys):
    """Regression: the documented fallback crashed with ``NameError``.

    ``main.py``'s ``except (EdgeCompanionUnavailable, CompanionUnavailable)``
    referenced ``CompanionUnavailable``, which was imported only inside the later
    ``if args.companion:`` block. When the edge companion refused, Python evaluated
    that except tuple, the name was unbound, and the process died with a
    ``NameError`` instead of falling back to the console UI.

    The test drives the real ``main()`` with the edge companion refusing, and
    requires that it reaches the console UI. A ``NameError`` anywhere in the
    handler fails this test.
    """
    import ui.edge_companion as edge_module
    import ui.launch as launch

    def refuse(*args, **kwargs):
        raise edge_module.EdgeCompanionUnavailable(
            "no display available", reason="window_failed")

    monkeypatch.setattr(launch, "run_edge_companion", refuse)

    status = _run_main(monkeypatch, "--edge-companion")

    assert status == 0, "main() must continue to the console UI and exit cleanly"
    assert len(_RecordingTerminalUI.instances) == 1, (
        "the console UI was never constructed, so the fallback did not happen")
    assert _RecordingTerminalUI.instances[0].ran, (
        "the console UI was constructed but never run")
    assert "[edge companion unavailable: window_failed]" in capsys.readouterr().out


def test_console_ui_path_is_unaffected_by_the_companion_fix(main_env, monkeypatch):
    """Control: the no-flag path still lands on the console UI.

    Without this, the fallback test above would also pass if ``main()`` ran the
    console UI unconditionally and simply ignored ``--edge-companion``.
    """
    status = _run_main(monkeypatch)

    assert status == 0
    assert len(_RecordingTerminalUI.instances) == 1
    assert _RecordingTerminalUI.instances[0].ran


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• defect (b): --edge-companion never docked


def test_main_passes_a_display_layer_and_honours_edge(main_env, monkeypatch):
    """``main.py`` used to start the edge companion with no display layer.

    ``ui/launch.build_edge_companion`` defaults ``display_layer=None``, and with
    it ``EdgeCompanionApp._list_displays`` returns ``[]`` so ``_refresh_display``
    returns ``None`` and ``_apply_placement`` returns before applying anything.
    The window therefore stayed at 0,0 at full size and ``--edge`` did nothing.

    ``launch_edge_companion`` is replaced by a recorder, so this asserts the
    wiring without building a window.
    """
    import ui.edge_companion as edge_module

    captured: dict = {}

    def recorder(controller, **kwargs):
        captured.update(kwargs)
        return _FakeCompanionApp(), True

    monkeypatch.setattr(edge_module, "launch_edge_companion", recorder)

    status = _run_main(monkeypatch, "--edge-companion", "--edge", "left")

    assert status == 0
    assert captured, "the edge companion launcher was never called"
    layer = captured.get("display_layer")
    assert layer is not None, (
        "main.py started the edge companion without a display layer, so "
        "placement can never be applied and --edge is a no-op")
    assert hasattr(layer, "list_displays"), (
        f"the display layer passed to the companion is not a display layer: {layer!r}")
    assert captured.get("edge") == "left", (
        f"--edge was not honoured: {captured.get('edge')!r}")


def test_main_passes_the_display_layer_for_every_edge(monkeypatch, main_env, capsys):
    """The edge choice must reach the launcher for all four supported edges."""
    import ui.edge_companion as edge_module

    for edge in ("left", "right", "top", "bottom"):
        captured: dict = {}

        def recorder(controller, _captured=captured, **kwargs):
            _captured.update(kwargs)
            return _FakeCompanionApp(), True

        monkeypatch.setattr(edge_module, "launch_edge_companion", recorder)

        status = _run_main(monkeypatch, "--edge-companion", "--edge", edge)
        capsys.readouterr()

        assert status == 0, f"edge={edge} did not run"
        assert captured, f"edge={edge} never reached the launcher"
        assert captured.get("display_layer") is not None, f"edge={edge} got no display layer"
        assert captured.get("edge") == edge, f"edge={edge} was forwarded as {captured.get('edge')!r}"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• the placement path itself, with a fake display layer


def _bare_app(display_layer, edge, root):
    """An ``EdgeCompanionApp`` with only the attributes placement touches.

    Constructed with ``object.__new__`` on purpose: the real ``__init__`` builds
    Tk widgets, and this suite must not create a window. Every attribute used by
    ``_refresh_display``/``_apply_placement`` is set explicitly.
    """
    from ui.edge_companion import EdgeCompanionApp

    app = object.__new__(EdgeCompanionApp)
    app._tk = _FakeTk
    app._display_layer = display_layer
    app._edge = edge
    app._display = None
    app._docked_display_id = ""
    app._placement = None
    app._monitor_check = 60.0
    app._last_monitor_check = 0.0
    app.root = root
    app.controller = _FakeController()
    from ui.edge import CompanionGeometry
    app._geometry = CompanionGeometry()
    return app


def test_a_fake_display_layer_produces_an_applied_placement():
    """With a display layer, placement is computed *and* applied to the window.

    This is the half that ``main.py`` was preventing. A fake display layer and a
    fake root are enough: no Win32 enumeration and no window are involved.
    """
    display = _fake_display("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True)
    root = _FakeRoot(pointer=(960, 540))
    app = _bare_app(_FakeDisplayLayer([display]), "right", root)

    placement = app.redock()

    assert placement is not None, (
        "a display layer reported one display, so a placement must exist")
    assert placement.edge == "right"
    assert placement.display_id == "\\\\.\\DISPLAY1"
    # Applied, not merely computed: the window was told where to go.
    assert root.geometries, "placement was computed but never applied to the window"
    assert root.geometries[-1] == placement.geometry_string()
    assert ("-topmost", True) in root.attributes_calls
    assert root.idletasks == 1


def test_each_edge_is_honoured_by_the_placement_path():
    """``--edge`` must actually move the window, not merely be stored."""
    display = _fake_display("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True)
    seen = {}
    for edge in ("left", "right", "top", "bottom"):
        root = _FakeRoot(pointer=(960, 540))
        app = _bare_app(_FakeDisplayLayer([display]), edge, root)
        placement = app.redock()
        assert placement is not None, f"edge={edge} produced no placement"
        assert placement.edge == edge
        seen[edge] = placement.geometry_string()

    assert len(set(seen.values())) == 4, (
        f"four edges produced {len(set(seen.values()))} distinct placements, so "
        f"the edge choice is not affecting placement: {seen}")


def test_no_display_layer_means_no_placement_and_no_geometry():
    """The defect's exact symptom, pinned.

    Without a display layer ``_list_displays`` returns ``[]``, so
    ``_refresh_display`` returns ``None`` and ``_apply_placement`` returns before
    touching the window. This is why ``main.py`` has to supply one.
    """
    root = _FakeRoot(pointer=(960, 540))
    app = _bare_app(None, "right", root)

    assert app.redock() is None, (
        "a placement was produced with no display layer at all")
    assert root.geometries == [], (
        "the window was repositioned with no display data, which is exactly the "
        f"jump to an assumed position the code is written to avoid: {root.geometries}")


def test_the_display_layer_main_builds_can_enumerate_displays():
    """The layer ``main.py`` constructs is the real one, and it works here.

    ``main.py`` builds ``core.windows.DisplayLayer`` when the Windows capability
    is present. This reads live topology, which needs no window and no desktop
    session, and proves the wiring passes a usable object rather than a stub.
    """
    from core.windows import DisplayLayer, displays_available

    assert displays_available(), (
        "this host cannot report display topology, so the wiring under test "
        "cannot be exercised here")
    layer = DisplayLayer()
    try:
        displays = list(layer.list_displays())
    finally:
        layer.close()
    assert displays, "DisplayLayer enumerated no displays"
    first = displays[0]
    for attribute in ("display_id", "bounds", "scale_percent"):
        assert hasattr(first, attribute), (
            f"a real DisplayInfo is missing {attribute}, so ui.edge's placement "
            f"math would not work against it")