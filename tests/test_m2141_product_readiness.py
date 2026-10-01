"""M2.14.1 - companion launch, truthful voice status, and Vision reachability.

Four independent defects, grouped because each one is the same failure shape:
**the product claimed a capability the runtime did not have.**

* the companion UI had a launcher module but no way to launch it, and the first
  draft of ``--companion`` tore down the capability runtime on its own failure
  path before falling back to the console;
* the header offered a voice toggle that could never speak, and TTS was invoked
  without checking whether anything could hear it;
* the Vision adapter advertised eight operations and served five, so three
  advertised operations raised on every call;
* the system prompt listed rules whose scope the permission ladder ignored.

These tests assert reachable behaviour, not that a symbol exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ui
from ui.bridge import CoreEventBusBridge
from ui.launch import CompanionUnavailable, build_companion_for_agent, _submitter_for
from voice.capabilities import (
    VoiceCapability,
    VoiceStatus,
    render_voice_status,
    voice_status,
)


# ══════════════════════════════════════════════════════ companion reachability


class TestCompanionIsReachable:
    def test_ui_exports_the_launch_entry_points(self):
        assert ui.run_companion is not None
        assert ui.CompanionUnavailable is CompanionUnavailable

    def test_main_declares_a_companion_flag(self):
        """--companion must be a real CLI flag, not a documented intention."""
        source = Path(__file__).resolve().parents[1] / "main.py"
        assert '"--companion"' in source.read_text(encoding="utf-8")

    def test_an_unavailable_companion_states_a_reason(self):
        exc = CompanionUnavailable("tkinter is not installed")
        assert "tkinter" in str(exc)
        assert exc.reason, "the failure must carry a stated reason"


class TestCompanionRuntimeIsBuiltFromRealObjects:
    def test_a_real_agent_can_supply_a_submitter(self):
        """The production entry point: a real Agent, wrapped for the UI thread."""
        from core.agent import Agent

        class _RealishAgent(Agent):
            def __init__(self):  # bypass the heavy constructor deliberately
                self.calls: list[str] = []

            def respond(self, text, *args, **kwargs):
                self.calls.append(text)
                return "a reply"

        agent = _RealishAgent()
        assert _submitter_for(agent)("hello") == "a reply"
        assert agent.calls == ["hello"]

    def test_a_ui_chosen_task_id_is_not_forwarded_to_the_agent(self):
        """Task identity is owned by the Agent's TaskManager. Accepting a
        UI-invented id would create a second source of task identity."""
        captured: list[tuple] = []

        class _Agent:
            def respond(self, *args, **kwargs):
                captured.append((args, kwargs))
                return ""

        _submitter_for(_Agent())("hello", "ui-made-up-task-id")
        assert captured == [(("hello",), {})], (
            f"the UI's task id reached the agent: {captured}")

    def test_the_submitter_returns_the_agent_reply(self):
        class _Agent:
            def respond(self, text):
                return f"echo: {text}"

        assert _submitter_for(_Agent())("hi") == "echo: hi"

    def test_the_runtime_owns_everything_it_creates(self, monkeypatch):
        """Shutdown must have exactly one owner for each resource."""
        import ui.companion as companion
        import ui.launch as launch

        closed: list[str] = []

        class _Controller:
            def __init__(self, bridge):
                self.bridge = bridge

            def start(self):
                return True

            def close(self, timeout=1.0):
                closed.append("controller")

        class _App:
            def mainloop(self):
                return 0

        monkeypatch.setattr(launch, "UIController", _Controller)
        monkeypatch.setattr(companion, "FloatingCompanionApp",
                            lambda controller=None, root=None: _App())

        class _Agent:
            def submit_message(self, text, task_id=""):
                return {"ok": True}

            def cancel_task(self, task_id=""):
                return True

        runtime = launch.build_companion_for_agent(_Agent(), _NullBus())
        assert runtime.controller is not None
        assert runtime.bridge is not None
        assert runtime.app is not None, "the runtime must own the Tk app too"

        runtime.close()
        assert closed == ["controller"], "the worker must be stopped exactly once"
        runtime.close()
        assert closed == ["controller"], "close must be idempotent"

    def test_a_window_failure_releases_what_was_already_built(self, monkeypatch):
        """A partial construction must not leak a running worker thread."""
        import ui.companion as companion
        import ui.launch as launch

        closed: list[str] = []

        class _Controller:
            def __init__(self, bridge):
                pass

            def start(self):
                return True

            def close(self, timeout=1.0):
                closed.append("controller")

        def _boom(**kwargs):
            raise RuntimeError("no display")

        monkeypatch.setattr(launch, "UIController", _Controller)
        monkeypatch.setattr(companion, "FloatingCompanionApp", _boom)

        class _Agent:
            def submit_message(self, text, task_id=""):
                return {}

        with pytest.raises(CompanionUnavailable):
            launch.build_companion_for_agent(_Agent(), _NullBus())
        assert closed == ["controller"], (
            "a failed window creation leaked the worker thread")


class _NullBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, *a, **k):
        return None


class TestBridgeLifecycle:
    def test_typed_events_reach_the_subscriber(self):
        events: list = []
        bridge = CoreEventBusBridge(_NullBus())
        bridge.subscribe_to_events(events.append)
        bridge._on_status({"message": "working", "task_id": "t1"})
        assert events, "a status update produced no event"

    def test_unsubscribing_stops_delivery(self):
        events: list = []
        bridge = CoreEventBusBridge(_NullBus())
        unsubscribe = bridge.subscribe_to_events(events.append)
        bridge._on_status({"message": "first", "task_id": "t1"})
        count = len(events)
        unsubscribe()
        bridge._on_status({"message": "second", "task_id": "t1"})
        assert len(events) == count, "delivery continued after unsubscribe"

    def test_close_is_idempotent(self):
        bridge = CoreEventBusBridge(_NullBus())
        bridge.close()
        bridge.close()

    def test_current_status_is_readable_before_any_work(self):
        bridge = CoreEventBusBridge(_NullBus())
        status = bridge.current_status()
        assert status is not None

    def test_cancelling_with_no_task_is_safe(self):
        assert CoreEventBusBridge(_NullBus()).cancel_task("") in (True, False)


class TestCompanionDoesNotTearDownTheFallback:
    def test_the_fallback_keeps_the_capability_runtime_alive(self):
        """The defect this guards against.

        ``--companion`` used to close the capability runtime in a ``finally``.
        When the companion was unavailable the exception unwound through that
        ``finally``, the runtime was closed, and the console fallback then ran
        against a torn-down runtime: a closed Blender connection and a released
        Windows layer, with no error explaining the later failures.

        ``main`` cannot be driven headlessly, so the decision is asserted on the
        structure of the block.
        """
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        assert "# Fall through" in source, "the fallback path is missing"
        block = source.split("if args.companion:")[1].split("# Fall through")[0]
        assert "finally:" not in block, (
            "the companion block must not close capabilities in a finally")
        assert block.count("close_capabilities()") == 1, (
            "the success path must close exactly once")
        assert "except CompanionUnavailable" in block


# ══════════════════════════════════════════════════════ voice honesty


class TestVoiceStatusIsMeasuredNotAsserted:
    def test_every_component_is_reported_explicitly(self):
        status = voice_status()
        for field in ("tts_capability", "tts_available", "stt_capability",
                      "stt_available", "wake_capability", "wake_available",
                      "barge_in_capability", "barge_in_available"):
            assert hasattr(status, field), f"{field} must be reported, not omitted"

    def test_unbuilt_components_say_deferred_not_available(self):
        """STT, wake word and barge-in are not built. Saying so is the fix."""
        status = voice_status()
        assert status.stt_capability is VoiceCapability.DEFERRED
        assert status.stt_available is False
        assert status.wake_capability is VoiceCapability.DEFERRED
        assert status.wake_available is False
        assert status.barge_in_capability is VoiceCapability.DEFERRED
        assert status.barge_in_available is False

    def test_an_available_backend_is_named(self):
        status = voice_status()
        if status.tts_available:
            assert status.tts_backend, "an available TTS backend must be named"

    def test_unavailable_tts_names_the_reason(self):
        status = voice_status()
        if not status.tts_available:
            assert status.notes, "an unavailable backend must explain itself"

    def test_the_rendered_status_does_not_promise_speech_it_lacks(self):
        rendered = render_voice_status()
        status = voice_status()
        if not status.tts_available:
            lowered = rendered.lower()
            assert "available" not in lowered or "unavailable" in lowered or \
                "not available" in lowered, (
                f"the status renders as though speech works: {rendered!r}")

    def test_a_failing_tts_factory_degrades_instead_of_raising(self, monkeypatch):
        """A COM or registry error during the probe must not propagate into the
        UI header, which is built on the console startup path."""
        import voice.tts as tts_module

        def _boom():
            raise OSError("COM error")

        monkeypatch.setattr(tts_module, "create_tts_backend", _boom)
        status = voice_status()
        assert status.tts_available is False
        assert any("TTS probe failed" in note for note in status.notes), (
            f"the probe failure was swallowed instead of reported: {status.notes}")

    def test_an_unavailable_backend_is_named_in_the_notes(self, monkeypatch):
        import voice.tts as tts_module

        class _Dead:
            def is_available(self):
                return False

        monkeypatch.setattr(tts_module, "create_tts_backend", _Dead)
        status = voice_status()
        assert status.tts_available is False
        assert status.notes, "an unusable backend must explain itself"

    def test_voice_status_accepts_settings_without_requiring_them(self):
        assert isinstance(voice_status(None), VoiceStatus)


class TestTerminalVoiceIsSafe:
    def _ui(self):
        import ui.terminal as terminal

        ui = terminal.TerminalUI.__new__(terminal.TerminalUI)
        ui._voice_status_cache = None
        return ui

    def test_no_backend_means_no_speech_attempt(self, monkeypatch):
        """The defect: ``tts.speak`` was called whenever voice was enabled,
        raising a COM error into the console loop on a machine with no engine."""
        import voice.text_to_speech as tts

        spoken: list[str] = []
        monkeypatch.setattr(tts, "speak", lambda text: spoken.append(text))

        ui = self._ui()
        ui._voice_status_cache = VoiceStatus(tts_available=False)
        ui._speak("hello")
        assert spoken == [], "speech was attempted with no available backend"

    def test_an_available_backend_is_used(self, monkeypatch):
        import voice.text_to_speech as tts

        spoken: list[str] = []
        monkeypatch.setattr(tts, "speak", lambda text: spoken.append(text))

        ui = self._ui()
        ui._voice_status_cache = VoiceStatus(tts_available=True, tts_backend="sapi")
        ui._speak("hello")
        assert spoken == ["hello"]

    def test_a_speech_failure_does_not_propagate(self, monkeypatch):
        """A voice failure must never be worth crashing the console loop over."""
        import voice.text_to_speech as tts

        def _boom(text):
            raise RuntimeError("COM error")

        monkeypatch.setattr(tts, "speak", _boom)

        ui = self._ui()
        ui._voice_status_cache = VoiceStatus(tts_available=True, tts_backend="sapi")
        ui._speak("hello")  # must not raise

    def test_the_status_line_does_not_advertise_unavailable_speech(self):
        """A status line that reads as active while nothing can speak is the lie
        this fixes."""
        summary = VoiceStatus(tts_available=False).summary()
        assert summary, "the voice line must render something"
        assert "tts=" not in summary, (
            f"the status names a TTS backend that is not available: {summary!r}")
        assert "nothing available" in summary.lower() or "unavailable" in summary.lower()

    def test_the_status_line_names_a_working_backend(self):
        status = VoiceStatus(tts_available=True, tts_backend="SapiTtsBackend")
        assert "SapiTtsBackend" in status.summary()

    def test_the_status_is_probed_once_and_cached(self, monkeypatch):
        """Probing imports win32com; doing it per reply would be slow."""
        import voice.capabilities as vc

        probes: list[int] = []
        real = vc.voice_status

        def _counting(*a, **k):
            probes.append(1)
            return real(*a, **k)

        monkeypatch.setattr(vc, "voice_status", _counting)
        ui = self._ui()
        ui._voice_status()
        ui._voice_status()
        ui._voice_status()
        assert len(probes) == 1, f"the backend was probed {len(probes)} times"


# ══════════════════════════════════════════════════════ Vision reachability


def _adapter(layer):
    from integration.contracts import VisionCapabilityAdapter

    return VisionCapabilityAdapter(layer)


def _served(layer) -> set[str]:
    return set(_adapter(layer).capabilities()["operations"])


class TestVisionAdvertisesOnlyWhatItServes:
    def test_every_advertised_operation_is_callable(self):
        """The core property: an advertised operation that raises is a defect."""
        from vision.capabilities import VisionCapabilityLayer

        layer = VisionCapabilityLayer()
        for op in _served(layer):
            assert callable(getattr(layer, op, None)), (
                f"{op} is advertised but the layer cannot serve it")

    def test_the_three_unserved_operations_are_not_advertised(self):
        from vision.capabilities import VisionCapabilityLayer

        served = _served(VisionCapabilityLayer())
        for op in ("detect_objects", "extract_text", "analyze_ui"):
            assert op not in served, (
                f"no backend implements {op}; advertising it raises on every call")

    def test_the_served_operations_are_all_reachable(self):
        from vision.capabilities import VisionCapabilityLayer

        assert {"validate_image", "inspect", "inspect_render",
                "describe", "compare"} <= _served(VisionCapabilityLayer())

    def test_can_execute_agrees_with_the_advertised_list(self):
        from vision.capabilities import VisionCapabilityLayer

        adapter = _adapter(VisionCapabilityLayer())
        for op in adapter.capabilities()["operations"]:
            assert adapter.can_execute(op) is True
        for op in ("detect_objects", "extract_text", "analyze_ui"):
            assert adapter.can_execute(op) is False

    def test_validate_image_is_reachable_even_with_no_backend(self):
        """It never reaches a backend, so backend discovery must not hide it."""

        class _NoBackend:
            def validate_image(self, p):
                return {}

            def inspect(self, p):
                return {}

            def detect_capabilities(self):
                return {"supported_operations": []}

        assert _served(_NoBackend()) == {"validate_image", "inspect"}

    def test_discovered_backend_operations_are_included(self):
        class _Layer:
            def validate_image(self, p):
                return {}

            def inspect(self, p):
                return {}

            def describe(self, p, detail_level="medium", focus=None, context=None):
                return {}

            def detect_capabilities(self):
                return {"supported_operations": ["describe"]}

        assert _served(_Layer()) == {"validate_image", "inspect", "describe"}

    def test_a_layer_with_nothing_implementable_falls_back(self):
        from integration.contracts import VISION_FALLBACK_OPERATIONS

        class _Empty:
            def detect_capabilities(self):
                return {}

        assert _served(_Empty()) == set(VISION_FALLBACK_OPERATIONS)

    def test_an_exploding_discovery_does_not_break_the_adapter(self):
        class _Layer:
            def validate_image(self, p):
                return {}

            def inspect(self, p):
                return {}

            def detect_capabilities(self):
                raise RuntimeError("backend probe failed")

        assert _served(_Layer()) == {"validate_image", "inspect"}

    def test_discovery_cannot_invent_an_operation_the_layer_lacks(self):
        """A backend claiming support the layer does not implement must not
        cause the adapter to advertise a call that will raise."""

        class _Liar:
            def inspect(self, p):
                return {}

            def detect_capabilities(self):
                return {"supported_operations": ["detect_objects", "inspect"]}

        assert _served(_Liar()) == {"inspect"}

    def test_the_operations_are_stable_across_repeated_calls(self):
        """Discovery must not make the advertised list vary per call."""
        from vision.capabilities import VisionCapabilityLayer

        adapter = _adapter(VisionCapabilityLayer())
        assert (adapter.capabilities()["operations"]
                == adapter.capabilities()["operations"])


class TestVisionDiffRatioIsCorrect:
    def test_identical_images_have_a_zero_difference(self, tmp_path):
        from PIL import Image

        from vision.inspect import compare_images

        a, b = tmp_path / "a.png", tmp_path / "b.png"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(a)
        Image.new("RGB", (32, 32), (10, 20, 30)).save(b)

        result = compare_images(a, b)
        assert result["similar"] is True
        assert result["diff_ratio"] == 0.0

    def test_a_fully_different_image_has_a_difference_of_one(self, tmp_path):
        from PIL import Image

        from vision.inspect import compare_images

        a, b = tmp_path / "a.png", tmp_path / "b.png"
        Image.new("RGB", (32, 32), (0, 0, 0)).save(a)
        Image.new("RGB", (32, 32), (255, 255, 255)).save(b)

        assert compare_images(a, b)["diff_ratio"] == 1.0

    def test_the_ratio_is_normalised_to_the_pixel_count(self, tmp_path):
        """The defect: differing *channel* values were counted against a pixel
        count, so one changed pixel in an RGB image reported 3/4096 and the
        threshold of 0.01 could never be met by any non-identical image."""
        from PIL import Image

        from vision.inspect import compare_images

        a, b = tmp_path / "a.png", tmp_path / "b.png"
        base = Image.new("RGB", (64, 64), (0, 0, 0))
        base.save(a)
        changed = base.copy()
        changed.putpixel((0, 0), (255, 255, 255))
        changed.save(b)

        ratio = compare_images(a, b)["diff_ratio"]
        assert 0.0 < ratio < 0.01, (
            f"one changed pixel in 4096 must be ~0.0002, got {ratio}")

    def test_the_ratio_is_always_within_the_unit_interval(self, tmp_path):
        from PIL import Image

        from vision.inspect import compare_images

        for size in (8, 64, 200):
            a, b = tmp_path / f"a{size}.png", tmp_path / f"b{size}.png"
            Image.new("RGB", (size, size), (1, 2, 3)).save(a)
            Image.new("RGB", (size, size), (250, 251, 252)).save(b)
            assert 0.0 <= compare_images(a, b)["diff_ratio"] <= 1.0

    def test_similar_is_derived_from_the_ratio_and_threshold(self, tmp_path):
        from PIL import Image

        from vision.inspect import compare_images

        a, b = tmp_path / "a.png", tmp_path / "b.png"
        Image.new("RGB", (16, 16), (0, 0, 0)).save(a)
        Image.new("RGB", (16, 16), (2, 2, 2)).save(b)

        result = compare_images(a, b, threshold=0.5)
        assert result["similar"] is True
        assert result["diff_ratio"] <= 0.5


# ══════════════════════════════════════════════════════ rule scope in context


class _FakeRules:
    """Renders from the arguments the agent actually passes."""

    def __init__(self):
        self.calls: list[dict] = []

    def render_active(self, project="", limit=0, task_id=""):
        self.calls.append({"project": project, "task_id": task_id, "limit": limit})
        if not task_id:
            return "alpha rule"
        return f"alpha rule\n{task_id} rule"

    def detect_conflicts_for_all(self):
        return ""


def _bare_agent(rules, task_id):
    from core.agent import Agent

    agent = Agent.__new__(Agent)
    agent._rules_store = rules
    agent._rules_engine = type("E", (), {"project": "alpha"})()
    agent._project = type("P", (), {"name": "alpha"})()
    agent._tasks = type("T", (), {"task_id": task_id})()
    agent._settings = type("S", (), {"rules_max_active_in_prompt": 50})()
    agent._memory_store = type("M", (), {"render_active": lambda **k: ""})()
    return agent


class TestAgentRendersRulesForTheCurrentTaskOnly:
    def test_the_task_id_is_passed_to_rule_rendering(self):
        from core.agent import Agent

        rules = _FakeRules()
        _bare_agent(rules, "task-2").rules_summary()
        assert rules.calls[-1]["task_id"] == "task-2", (
            "rules_summary did not tell the store which task is running")

    def test_a_task_rule_is_hidden_from_another_task(self):
        from core.agent import Agent

        rendered = _bare_agent(_FakeRules(), "task-2").rules_summary()
        assert "task-2 rule" in rendered
        assert "task-1 rule" not in rendered

    def test_with_no_task_no_task_rule_is_shown(self):
        from core.agent import Agent

        assert "task-1 rule" not in _bare_agent(_FakeRules(), "").rules_summary()

    def test_the_prompt_reference_section_filters_by_task_too(self):
        from core.agent import Agent

        rules = _FakeRules()
        _bare_agent(rules, "task-3")._reference_sections()
        assert rules.calls[-1]["task_id"] == "task-3", (
            "the system prompt and the permission ladder must agree about scope")

    def test_a_render_failure_does_not_break_the_prompt(self):
        """Defensive, but this is the path that must never take the agent down."""

        class _Broken:
            def render_active(self, **kwargs):
                raise RuntimeError("rules file is corrupt")

        agent = _bare_agent(_Broken(), "t1")
        assert agent._reference_sections() is not None


class TestTaskIdReachesThePermissionLadder:
    def test_the_executor_re_reads_the_task_id_on_every_decision(self):
        """A provider that was captured once would leave a stale task_id applied
        to every later permission decision after a task switch."""
        from core.executor import Executor

        state = {"task": "t1"}
        executor = Executor.__new__(Executor)
        executor._task_provider = lambda: state["task"]

        assert executor._task_id() == "t1"
        state["task"] = "t2"
        assert executor._task_id() == "t2", (
            "the task id must be re-read per decision, not cached")

    def test_a_missing_provider_is_not_an_error(self):
        from core.executor import Executor

        executor = Executor.__new__(Executor)
        executor._task_provider = None
        assert executor._task_id() == ""

    def test_a_raising_provider_degrades_to_no_task(self):
        from core.executor import Executor

        executor = Executor.__new__(Executor)
        executor._task_provider = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        assert executor._task_id() == "", (
            "a failing task lookup must not block a permission decision")

    def test_the_agent_wires_its_own_task_id(self):
        """The wiring that makes task-scoped rules enforceable at all."""
        import inspect

        from core.agent import Agent

        source = inspect.getsource(Agent.__init__)
        assert "set_task_provider" in source, (
            "the Agent no longer gives the executor a task id")