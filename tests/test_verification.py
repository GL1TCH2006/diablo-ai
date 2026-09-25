from config.permissions import Decision, PermissionLevel
from core.verification import Verifier


def test_check_file(tmp_path):
    verifier = Verifier()
    ok, _ = verifier.check_file(tmp_path / "missing.py")
    assert ok is False
    target = tmp_path / "x.py"
    target.write_text("print(1)\n", encoding="utf-8")
    ok, msg = verifier.check_file(target)
    assert ok is True
    assert "bytes" in msg


def test_check_output():
    verifier = Verifier()
    assert verifier.check_output("some text HELLO World here", "hello world") is True
    assert verifier.check_output("nothing here", "hello world") is False


def test_check_run_success():
    verifier = Verifier()
    decision = Decision(PermissionLevel.NORMAL, allow=True, requires_confirmation=False)
    ok, _ = verifier.check_run(decision, 0, "Hello World", "hello world")
    assert ok is True


def test_check_run_failures():
    verifier = Verifier()
    decision = Decision(PermissionLevel.NORMAL, allow=True, requires_confirmation=False)
    ok, msg = verifier.check_run(decision, 1, "", None)
    assert ok is False and "code 1" in msg
    ok, msg = verifier.check_run(decision, 0, "different", "hello")
    assert ok is False and "expected" in msg