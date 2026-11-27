import pytest

from core.debug_loop import DebugLoop, DebugReport, _brief
from core.planner import Planner
from core.verification import Verifier


def test_verifier_detects_traceback():
    v = Verifier()
    has, sig = v.check_failure("Traceback (most recent call last):\n  File x\nValueError: bad")
    assert has
    assert "ValueError" in sig or "Traceback" in sig


def test_verifier_detects_failed_marker():
    v = Verifier()
    has, sig = v.check_failure("FAILED test_math.py::test_sqrt")
    assert has


def test_verifier_ignores_normal_output():
    v = Verifier()
    has, sig = v.check_failure("All good, 5 passed")
    assert not has


def test_verifier_extracts_pytest_failure_signature():
    v = Verifier()
    has, sig = v.check_failure("2 failed. FAILED tests/test_a.py::test_x")
    assert has
    assert sig


def test_extract_error_blocks():
    text = "nothing here\nTraceback (most recent call last):\nValueError: boom\nrest"
    block = DebugLoop.extract_error(text)
    assert "Traceback (most recent call last)" in block
    assert "ValueError: boom" in block
    assert DebugLoop.extract_error("plain message") == "plain message"


def test_brief_truncates():
    assert len(_brief({"command": "x" * 300})) <= 150


class _ToolResult:
    def __init__(self, success, output="", error=""):
        self.success, self.output, self.error = success, output, error
        self.allowed = True
        self.executed = success


class _SafeExecutor:
    def __init__(self, script):
        self._script = script
        self.calls = []

    def run(self, tool, args):
        self.calls.append(tool)
        if self._script and self._script[0] == (tool, args):
            del self._script[0]
        return _ToolResult(True)


class _FakingLLM:
    """Return a script of responses; each response has text + tool_calls or stop."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.prompted = 0

    def __call__(self, messages, tools=None, model=None):
        self.prompted += 1
        deliver = self._plan[0] if self._plan else ("done", [])
        if len(self._plan) > 1:
            del self._plan[0]
        text, calls = deliver
        return type("R", (), {"text": text, "tool_calls": calls})


def test_debug_loop_runs_until_no_tool_calls(tmp_settings):
    llm = _FakingLLM([
        ("inspect", [type("C", (), {"name": "project.info", "arguments": {"path": "C:\\x"}})()]),
        ("fixed it", []),
    ])
    ex = _SafeExecutor([("project.info", {"path": "C:\\x"})])
    loop = DebugLoop(llm, ex, tmp_settings)
    report = loop.diagnose("Traceback: ValueError", {"mission": "fix", "project": "proj", "files": "", "project_name": "p"})
    assert ex.calls == ["project.info"]
    assert report.final_message == "fixed it"
    assert report.verified


def test_debug_loop_stops_on_repeated_signature(tmp_settings):
    tmp_settings.debug_max_attempts = 2
    tmp_settings.debug_max_steps = 20
    calls = [type("C", (), {"name": "terminal.execute", "arguments": {"command": "pytest"}})()]

    class _FailExec:
        def __init__(self):
            self.calls = 0

        def run(self, tool, args):
            self.calls += 1
            return _ToolResult(False, error="SyntaxError: bad import")

    class _SameLLM:
        def __init__(self, c):
            self._c = c

        def __call__(self, messages, tools=None, model=None):
            return type("R", (), {"text": "try again", "tool_calls": list(self._c)})()

    # Hand-build: same failing tool call forever
    ex = _FailExec()
    llm = _SameLLM(calls)
    loop = DebugLoop(llm, ex, tmp_settings)
    report = loop.diagnose("Traceback: SyntaxError", {"mission": "fix", "project": "", "files": ""})
    assert "Repeated failure signature" in report.exhausted
    assert ex.calls >= 2  # dedup prevented runaway


def test_debug_loop_step_cap(tmp_settings):
    tmp_settings.debug_max_steps = 2
    llm = _FakingLLM([
        ("a", [type("C", (), {"name": "project.info", "arguments": {"path": "C:\\y"}})()]),
        ("b", [type("C", (), {"name": "project.info", "arguments": {"path": "C:\\y"}})()]),
        ("c", [type("C", (), {"name": "project.info", "arguments": {"path": "C:\\y"}})()]),
    ])
    ex = _SafeExecutor([("project.info", {"path": "C:\\y"})] * 3)
    loop = DebugLoop(llm, ex, tmp_settings)
    report = loop.diagnose("fix X", {"mission": "m", "project": "", "files": ""})
    assert len(ex.calls) <= tmp_settings.debug_max_steps


def test_planner_coding_intent_debug(tmp_settings):
    p = Planner(tmp_settings)
    assert p.coding_intent("Please fix the broken endpoint, getting a Traceback") == "debug"
    assert p.coding_intent("Add a new feature to the app") == "develop"
    assert p.coding_intent("What is the weather?") is None


def test_debug_loop_auto_verify(tmp_settings):
    llm = _FakingLLM([
        ("patched", [type("C", (), {"name": "code.patch_file", "arguments": {"patch": "=== x.py ===\nreplace a\n+b"}})()]),
        ("done", []),
    ])
    ex = _SafeExecutor([
        ("code.patch_file", {"patch": "=== x.py ===\nreplace a\n+b"}),
        ("terminal.execute", {"command": "pytest -q", "working_dir": "C:\\proj"}),
    ])
    loop = DebugLoop(llm, ex, tmp_settings, auto_verify="pytest -q", verify_dir="C:\\proj")
    report = loop.diagnose("Traceback in x", {"mission": "fix", "project": "", "files": ""})
    assert ex.calls[-1] == "terminal.execute"
    assert "pytest -q passed" in (report.final_message or "")
    assert report.verified
def test_debug_tool_scoping(tmp_settings):
    from core.debug_loop import DebugLoop
    assert DebugLoop._is_debug_tool("filesystem.read_file")
    assert DebugLoop._is_debug_tool("project.info")
    assert DebugLoop._is_debug_tool("code.patch_file")
    assert DebugLoop._is_debug_tool("terminal.execute")
    assert not DebugLoop._is_debug_tool("filesystem.write_file")
    assert not DebugLoop._is_debug_tool("gh.issue_create")
    assert not DebugLoop._is_debug_tool("memory.remember")

def test_debug_loop_tracks_coding_steps(tmp_settings):
    from coding.change_tracker import ChangeTracker
    llm = _FakingLLM([
        ("start", [type("C", (), {"name": "code.patch_file", "arguments": {"patch": "=== x.py ===\nreplace a\n+b"}})()]),
        ("done", []),
    ])
    ex = _SafeExecutor([("code.patch_file", {"patch": "=== x.py ===\nreplace a\n+b"})])
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    loop = DebugLoop(llm, ex, tmp_settings, tracker=tracker)
    report = loop.diagnose("fix x", {"mission": "fix", "project": "", "files": ""})
    assert report.steps[0].action.startswith("code.patch_file")
    assert tracker.active is not None
    assert "code.patch_file" in tracker.summary()

