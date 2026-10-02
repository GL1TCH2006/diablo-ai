"""M2.16.UI - deterministic tests for the narrow backend event stream (Part 10).

The mapper is the security boundary between the backend and the screen, so these
tests are as much about what must *not* appear as about what must.

Everything is checked with no window and no backend: the mapper is a pure function
of its input, which is what makes the redaction rules cheap to keep honest.
"""

from __future__ import annotations

from typing import Any

import pytest

from ui.companion_events import (
    MAX_MESSAGE_CHARS,
    MAX_SUMMARY_CHARS,
    map_backend_event,
    sanitize_text,
)
from ui.companion_state import CompanionEventKind
from ui.models import AgentEvent


# ═════════════════════════════════════════════════════════ redaction


class TestRedaction:
    def test_credentials_are_removed(self) -> None:
        for text in (
            "api_key=sk-abcdef1234567890",
            "token: hunter2hunter2",
            "password = correcthorse",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc",
            "my api key is abcdef123456",
        ):
            cleaned = sanitize_text(text)
            assert "abcdef123456" not in cleaned, f"leaked a key from {text!r}"
            assert "hunter2hunter2" not in cleaned
            assert "correcthorse" not in cleaned
            assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
            assert "[redacted]" in cleaned

    def test_bare_provider_keys_are_removed(self) -> None:
        for text in ("sk-abcdefgh12345", "ghp_abcdefgh12345ijkl",
                     "xoxb-1234567890-abcdefghij"):
            assert sanitize_text(text) == "[redacted]", f"leaked from {text!r}"

    def test_native_handles_are_removed(self) -> None:
        for text in ("hwnd=0x000A1B2C", "handle: 0x00FF00FF00FF",
                     "runtime_id=0x1234567890",
                     "process_id=1234567",
                     "see 0xDEADBEEF00 for details"):
            cleaned = sanitize_text(text)
            assert "0x000A1B2C" not in cleaned, f"leaked a handle from {text!r}"
            assert "0x00FF00FF00FF" not in cleaned
            assert "0x1234567890" not in cleaned
            assert "0xDEADBEEF00" not in cleaned

    def test_handle_labels_survive_so_the_line_still_reads(self) -> None:
        assert "hwnd" in sanitize_text("hwnd=0x000A1B2C")
        assert "[handle]" in sanitize_text("hwnd=0x000A1B2C")

    def test_multiline_logs_become_one_line(self) -> None:
        raw = "Traceback (most recent call last):\n  File x\nRuntimeError: boom"
        cleaned = sanitize_text(raw)
        assert "\n" not in cleaned
        assert cleaned.startswith("Traceback (most recent call last):")

    def test_control_characters_are_stripped(self) -> None:
        assert "\x00" not in sanitize_text("bad\x00value")
        assert "\x1b" not in sanitize_text("bad\x1b[31mred")

    def test_length_is_capped(self) -> None:
        assert len(sanitize_text("x" * 5000)) <= MAX_MESSAGE_CHARS
        assert len(sanitize_text("x" * 5000, limit=40)) <= 40

    def test_summaries_are_capped_harder(self) -> None:
        assert len(sanitize_text("y" * 500, limit=MAX_SUMMARY_CHARS)) <= MAX_SUMMARY_CHARS

    @pytest.mark.parametrize("value", [None, "", 0, [], {}, object()])
    def test_sanitize_never_raises(self, value: Any) -> None:
        assert isinstance(sanitize_text(value), str)

    def test_normal_text_is_left_alone(self) -> None:
        text = "Opening Calculator"
        assert sanitize_text(text) == text


# ═════════════════════════════════════════════════════════ recognition


class TestRecognition:
    @pytest.mark.parametrize("name,expected", [
        ("REQUEST_ACCEPTED", CompanionEventKind.TASK_ACCEPTED),
        ("TASK_CREATED", CompanionEventKind.TASK_ACCEPTED),
        ("PLANNING", CompanionEventKind.THINKING),
        ("TOOL_SELECTED", CompanionEventKind.THINKING),
        ("STEP_STARTED", CompanionEventKind.STEP),
        ("STEP_COMPLETED", CompanionEventKind.STEP),
        ("VERIFICATION_STARTED", CompanionEventKind.VERIFICATION),
        ("VERIFICATION_COMPLETED", CompanionEventKind.VERIFICATION),
        ("RECOVERY_STARTED", CompanionEventKind.RECOVERY),
        ("CANCELLATION_REQUESTED", CompanionEventKind.CANCELLATION_REQUESTED),
        ("TASK_COMPLETED", CompanionEventKind.TASK_COMPLETED),
        ("TASK_FAILED", CompanionEventKind.TASK_FAILED),
        ("TASK_CANCELLED", CompanionEventKind.TASK_CANCELLED),
    ])
    def test_conceptual_prompt_names(self, name: str, expected: CompanionEventKind) -> None:
        event = map_backend_event({"type": name})
        assert event is not None and event.kind is expected

    @pytest.mark.parametrize("name,expected", [
        ("step_started", CompanionEventKind.STEP),
        ("step-completed", CompanionEventKind.STEP),
        ("Step Completed", CompanionEventKind.STEP),
        ("  TASK_COMPLETED  ", CompanionEventKind.TASK_COMPLETED),
    ])
    def test_spelling_variants(self, name: str, expected: CompanionEventKind) -> None:
        event = map_backend_event({"type": name})
        assert event is not None and event.kind is expected

    @pytest.mark.parametrize("status,expected", [
        ("thinking", CompanionEventKind.THINKING),
        ("executing", CompanionEventKind.EXECUTING),
        ("verifying", CompanionEventKind.VERIFICATION),
        ("success", CompanionEventKind.TASK_COMPLETED),
        ("cancelled", CompanionEventKind.TASK_CANCELLED),
        ("error", CompanionEventKind.ERROR),
    ])
    def test_ui_status_envelopes(self, status: str, expected: CompanionEventKind) -> None:
        event = map_backend_event({"type": "status", "status": status})
        assert event is not None and event.kind is expected

    def test_agent_event_objects_are_accepted(self) -> None:
        event = map_backend_event(
            AgentEvent(type="success", message="Calculator opened.",
                       status="success", task_id="task-1"))
        assert event is not None
        assert event.kind is CompanionEventKind.TASK_COMPLETED
        assert event.message == "Calculator opened."
        assert event.task_id == "task-1"

    def test_activity_envelope_uses_its_status(self) -> None:
        event = map_backend_event(
            AgentEvent(type="activity", message="tool: running", status="executing"))
        assert event is not None and event.kind is CompanionEventKind.EXECUTING

    @pytest.mark.parametrize("tool_status,expected", [
        ("pending_confirmation", CompanionEventKind.PERMISSION),
        ("denied", CompanionEventKind.PERMISSION),
        ("blocked", CompanionEventKind.PERMISSION),
        ("error", CompanionEventKind.ERROR),
        ("running", CompanionEventKind.STEP),
    ])
    def test_tool_status_wins_over_envelope(self, tool_status: str,
                                            expected: CompanionEventKind) -> None:
        event = map_backend_event({"type": "activity", "status": "ready",
                                   "tool": "desktop.open", "tool_status": tool_status})
        assert event is not None and event.kind is expected

    def test_a_cancelled_tool_is_not_a_cancelled_task(self) -> None:
        """One cancelled tool must not make the companion claim a task outcome."""
        event = map_backend_event({"type": "activity", "tool": "desktop.find",
                                   "tool_status": "cancelled"})
        assert event is None

    def test_heartbeat_statuses_are_not_rendered(self) -> None:
        for status in ("ready", "idle", "closed"):
            assert map_backend_event({"type": "status", "status": status}) is None

    @pytest.mark.parametrize("payload", [
        None, 42, "a string", [], {},
        {"type": "debug_trace"}, {"type": "heartbeat"}, {"message": "no type"},
        {"type": ""}, {"type": None},
    ])
    def test_unmappable_payloads_are_dropped_not_guessed(self, payload: Any) -> None:
        assert map_backend_event(payload) is None

    def test_unknown_object_shapes_do_not_raise(self) -> None:
        class Weird:
            pass

        assert map_backend_event(Weird()) is None


# ═════════════════════════════════════════════════════════ presentation safety


class TestPresentationSafety:
    def test_metadata_is_never_forwarded(self) -> None:
        """The raw source event must not reach the companion."""
        raw = {"hwnd": 0x00FF00FF, "api_key": "sk-leaked", "prompt": "system prompt"}
        event = map_backend_event(
            AgentEvent(type="activity", message="working", status="executing",
                       metadata={"source_event": raw}))
        assert event is not None
        rendered = f"{event.message} {event.summary} {event.task_id}"
        for leak in ("sk-leaked", "system prompt", "source_event", "0x00FF00FF"):
            assert leak not in rendered

    def test_secrets_in_messages_are_redacted_end_to_end(self) -> None:
        event = map_backend_event({"type": "step_started",
                                   "message": "using api_key=sk-abcdefgh12345"})
        assert event is not None
        assert "sk-abcdefgh12345" not in event.message
        assert "sk-abcdefgh12345" not in event.summary

    def test_handles_in_messages_are_redacted_end_to_end(self) -> None:
        event = map_backend_event({"type": "step_started",
                                   "message": "target hwnd=0x00AB12CD"})
        assert event is not None
        assert "0x00AB12CD" not in event.message

    def test_terminal_failures_get_a_plain_line(self) -> None:
        event = map_backend_event({"type": "task_failed"})
        assert event is not None
        assert event.message == "I couldn't complete that."

    def test_terminal_success_gets_a_plain_line(self) -> None:
        event = map_backend_event({"type": "task_completed"})
        assert event is not None
        assert event.message == "Done."

    def test_cancelled_gets_a_plain_line(self) -> None:
        event = map_backend_event({"type": "task_cancelled"})
        assert event is not None
        assert event.message == "Stopped."

    def test_task_id_is_cleaned_for_comparison(self) -> None:
        event = map_backend_event({"type": "step_started", "task_id": "task-1\n/../x"})
        assert event is not None
        assert event.task_id == "task-1x"

    def test_progress_summary_prefers_the_real_subject(self) -> None:
        event = map_backend_event({"type": "activity", "status": "executing",
                                   "tool": "desktop.open", "message": "running"})
        assert event is not None
        assert "desktop.open" in event.summary

    def test_summary_falls_back_when_nothing_is_available(self) -> None:
        event = map_backend_event({"type": "verifying"})
        assert event is not None
        assert event.summary == "Verifying"

    def test_bad_timestamps_do_not_raise(self) -> None:
        for stamp in ("soon", None, -5, float("nan")):
            event = map_backend_event({"type": "step_started", "timestamp": stamp})
            assert event is not None and event.timestamp > 0

    def test_already_mapped_events_are_resanitised(self) -> None:
        """Trusting a pre-built companion event would bypass redaction."""
        from ui.companion_state import CompanionEvent

        event = map_backend_event(
            CompanionEvent(kind=CompanionEventKind.STEP,
                           message="hwnd=0x00ABCDEF", summary="api_key=sk-abcdefgh12345"))
        assert event is not None
        assert "0x00ABCDEF" not in event.message
        assert "sk-abcdefgh12345" not in event.summary

    def test_mapped_event_round_trips_through_json_shape(self) -> None:
        event = map_backend_event({"type": "task_completed", "message": "All done.",
                                   "task_id": "t1"})
        assert event is not None
        data = event.to_dict()
        assert data["kind"] == "task_completed"
        assert data["message"] == "All done."
        assert data["task_id"] == "t1"