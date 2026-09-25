from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from config.permissions import Decision


class VerificationState(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class VerificationResult:
    state: VerificationState
    detail: str
    kind: str = ""
    evidence: dict = field(default_factory=dict)


class Verifier:
    """Checks that tool results match expectations before success is reported."""

    VALID_FILE_EXTENSIONS = (".py", ".pyw", ".js", ".ts", ".sh", ".ps1", ".txt", ".md", ".json", ".toml", ".yaml", ".yml", ".html", ".css", ".sql")

    def check_file(self, path: Path) -> tuple[bool, str]:
        if not path.exists():
            return False, f"File does not exist: {path}"
        if not path.is_file():
            return False, f"Not a file: {path}"
        size = path.stat().st_size
        if path.suffix.lower() not in self.VALID_FILE_EXTENSIONS and size == 0:
            return False, f"Empty file with unknown extension: {path}"
        return True, f"File exists ({size} bytes): {path}"

    def check_run(self, decision: Decision, exit_code, stdout: str, expected: str | None = None) -> tuple[bool, str]:
        if not decision.allow:
            return False, decision.reason or "Action was not permitted."
        if exit_code is None:
            return False, "Execution completed without a verifiable exit code."
        if exit_code != 0:
            return False, f"Process exited with code {exit_code}."
        if expected is not None:
            found = re.search(re.escape(expected), stdout, re.I)
            if not found:
                return False, f"Output did not contain expected text {expected!r}."
        return True, "Execution succeeded and matched expectations."

    def check_output(self, stdout: str, expected: str) -> bool:
        return bool(re.search(re.escape(expected), stdout, re.I))

    _ERROR_MARKERS = (
        r"\b(Traceback \(most recent call last\))\b",
        r"\b(\w+(?:Error|Exception))\b",
        r"\b(FAILED|FAILURES?)\b",
        r"\b(AssertionError)\b",
        r"exit(?:ed)? with code (\d+)",
    )

    def check_failure(self, output: str) -> tuple[bool, str]:
        """Return (has_failure, signature). Signature is a stable key for dedup."""
        if not output:
            return False, ""
        for pattern, group in ((p, 1) for p in self._ERROR_MARKERS):
            match = re.search(pattern, output)
            if match:
                token = match.group(group) or match.group(0)
                return True, token[:60].strip()
        first = next((ln.strip() for ln in output.splitlines() if ln.strip()), "")
        return False, first[:60].strip()

    def terminal_failure(self, output: str) -> bool:
        return self.check_failure(output)[0]