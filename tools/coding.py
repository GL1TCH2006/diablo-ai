from __future__ import annotations

import re
from pathlib import Path

from config.settings import Settings
from core.logging_setup import get_logger
from core.verification import VerificationResult, VerificationState

from .registry import ToolResult

log = get_logger("coding")

_MAX_GREP_RESULTS = 100
_MAX_GREP_FILE_BYTES = 256 * 1024

#: Legacy verification kind for "the project's own test suite was run".
_VERIFY_KIND = "project_tests"

#: pytest summary counters, e.g. "2 failed, 5 passed in 0.31s" or "5 passed".
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: pytest's wording when a suite collected nothing. Distinct from a run whose
#: tests failed, and it must not be reported as a failure.
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)", re.I)


def _parse_test_counts(text: str) -> tuple[int, int]:
    """Return ``(tests_passed, tests_failed)`` from a pytest summary.

    Best effort by design. An unparsable summary yields ``(0, 0)``, which the
    caller treats as "no positive evidence" rather than as a pass. A missing or
    unreadable count must never be able to manufacture a PASSED.
    """
    if not text:
        return 0, 0
    passed = 0
    failed = 0
    for count, label in _COUNT_RE.findall(text):
        value = int(count)
        label = label.lower()
        if label == "passed":
            passed = max(passed, value)
        elif label in {"failed", "error", "errors"}:
            failed += value
    return passed, failed


def _verification_from_run(
    root: Path,
    command: str,
    ok: bool,
    output: str,
    stderr: str,
) -> VerificationResult:
    """Turn an observed test run into an evidence-backed verdict.

    ``PASSED`` is requested only when the suite ran, reported at least one
    passing test, reported no failures, and the process exited zero. "The
    command returned 0" is never on its own sufficient.
    """
    combined = _ANSI_RE.sub("", f"{output}\n{stderr}")
    passed, failed = _parse_test_counts(combined)
    evidence = {
        "root": str(root),
        "command": command,
        # "passed" is an assertion key in the ledger, so a False value blocks a
        # PASSED verdict independently of this function's own logic.
        "passed": bool(ok) and passed > 0 and failed == 0,
        "tests_passed": passed,
        "tests_failed": failed,
        "summary": combined.strip()[:400],
    }

    if _NO_TESTS_RE.search(combined):
        # Nothing was collected, so there is nothing to verify. This is not a
        # failure of the project; it is an absence of evidence.
        evidence["passed"] = False
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The project test suite collected no tests, so there is nothing to verify.",
            _VERIFY_KIND,
            evidence,
        )

    if evidence["passed"]:
        return VerificationResult(
            VerificationState.PASSED,
            f"Project test suite passed ({passed} test(s)).",
            _VERIFY_KIND,
            evidence,
        )

    # Everything else - failing tests, a collection error, a crash, an
    # unparsable summary - is a failure to establish that the project works.
    return VerificationResult(
        VerificationState.FAILED,
        f"Project test suite did not pass ({failed} failing, {passed} passing).",
        _VERIFY_KIND,
        evidence,
    )


def project_verify(path: str, settings: Settings | None = None) -> ToolResult:
    """Run the project's detected test suite and report an evidence-backed verdict.

    Reuses the existing project-intelligence layer: ``coding.commands`` decides
    *which* command verifies this project, and ``coding.test_runner`` runs it.
    Nothing here decides what "correct" means for a project - it only reports
    what was observed, and lets the VerificationLedger adjudicate.
    """
    from coding.commands import detect_commands
    from coding.test_runner import run_pytest

    root = Path(path)
    if not root.is_dir():
        return ToolResult(False, error=f"Not a directory: {root}")

    try:
        commands = detect_commands(root)
    except Exception as exc:  # pragma: no cover - detect_commands never raises
        return ToolResult(False, error=f"Could not detect project commands: {exc}")

    spec = commands.best_test()
    if not spec.exists:
        return ToolResult(
            False,
            error=(
                "No test command could be detected for this project, so it cannot be "
                "verified by running tests."
            ),
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "No test command was detected for this project.",
                _VERIFY_KIND,
                {"root": str(root), "detected": commands.to_context()},
            ),
        )

    # Only the pytest family is executed here. Other ecosystems are reported
    # honestly as unverified rather than being run through a guessed command.
    if "pytest" not in spec.command:
        return ToolResult(
            False,
            error=f"Detected test command is not a pytest command: {spec.command}",
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                f"Detected test command {spec.command!r} is outside the supported pytest family.",
                _VERIFY_KIND,
                {"root": str(root), "command": spec.command},
            ),
        )

    ok, output, stderr = run_pytest(str(root))
    verification = _verification_from_run(root, spec.command, ok, output, stderr)
    exit_code = 0 if ok else 1
    if verification.state is VerificationState.PASSED:
        return ToolResult(
            True,
            output=(output or stderr or "tests passed").strip()[:4000],
            exit_code=exit_code,
            data={"command": spec.command, "tests_passed": verification.evidence.get("tests_passed", 0)},
            verification=verification,
        )
    # The tool did its job (the suite ran and reported); the *verdict* is not a
    # pass. Reporting success=False here is what lets the existing
    # RecoveryPolicy see a retryable failure instead of the task terminating on
    # the spot, so a broken test can still be repaired.
    return ToolResult(
        False,
        output=(output or "").strip()[:4000],
        error=(stderr or output or "test run did not pass").strip()[:2000],
        exit_code=exit_code,
        data={"command": spec.command},
        verification=verification,
    )


def project_info(path: str, settings: Settings | None = None) -> ToolResult:
    from coding.profile import build_profile

    cache = (settings.sessions_dir / "project-index") if settings else None
    try:
        profile = build_profile(Path(path), cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not analyse project: {exc}")
    return ToolResult(True, output=profile.to_context())


def project_files(root: str, pattern: str = "", folder: str = "", settings: Settings | None = None) -> ToolResult:
    base = Path(root)
    if not base.is_dir():
        return ToolResult(False, error=f"Not a directory: {base}")
    try:
        cache = (settings.sessions_dir / "project-index") if settings else None
        from coding.project_index import index_cached
        records = index_cached(base, cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not index project: {exc}")

    pat = pattern.strip().lower()
    folder = folder.strip().lower()
    hits = []
    for rec in sorted(records, key=lambda r: r.relative):
        if folder and not rec.relative.lower().startswith(folder):
            continue
        if pat and pat not in rec.name.lower() and pat not in rec.relative.lower():
            continue
        hits.append(rec.relative)
        if len(hits) >= 200:
            hits.append("... (limit reached)")
            break
    if not hits:
        return ToolResult(True, output="(no matching files)")
    return ToolResult(True, output="\n".join(hits))


def project_grep(root: str, pattern: str, settings: Settings | None = None, limit: int = _MAX_GREP_RESULTS) -> ToolResult:
    base = Path(root)
    if not base.is_dir():
        return ToolResult(False, error=f"Not a directory: {base}")
    if not pattern:
        return ToolResult(False, error="Empty search pattern.")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(False, error=f"Invalid regex: {exc}")

    try:
        cache = (settings.sessions_dir / "project-index") if settings else None
        from coding.project_index import index_cached
        records = index_cached(base, cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not index project: {exc}")

    matches: list[str] = []
    try:
        for rec in sorted(records, key=lambda r: r.relative):
            if rec.is_binary_like or rec.size > _MAX_GREP_FILE_BYTES:
                continue
            try:
                path = base / rec.relative
                if not path.is_file() or path.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rec.relative}:{idx}: {line.strip()[:220]}")
                    if len(matches) >= limit:
                        matches.append("... (limit reached)")
                        return ToolResult(True, output="\n".join(matches))
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Grep failed: {exc}")
    if not matches:
        return ToolResult(True, output="(no matches)")
    return ToolResult(True, output="\n".join(matches))