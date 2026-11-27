from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from config.permissions import Decision, path_is_within


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


#: ``VerificationResult.kind`` for an independent filesystem probe. It resolves
#: through ``verification_ledger.LEGACY_KIND_AXES`` to
#: ``(VerificationSubject.FILE, VerificationMethod.FILE_AND_OUTPUT)`` and, because
#: the method is not ``NONE``, to ``EvidenceTrust.OBSERVED`` - which is the only
#: trust level that may support ``PASSED``. Reusing the existing kind rather than
#: adding one means the whole adjudication path works unchanged: no new ledger
#: entry, no new method, no new trust level.
FILESYSTEM_PROBE_KIND = "file_and_output"

#: ``VerificationResult.kind`` for an independent **absence** probe - a delete.
#:
#: It maps to the same ``(FILE, FILE_AND_OUTPUT)`` axes as
#: :data:`FILESYSTEM_PROBE_KIND`, so it carries the same ``OBSERVED`` trust and
#: needs no new subject, method or enum. Only the assertion differs, and the
#: assertion is expressed in the evidence (see :meth:`Verifier.observe_absence`),
#: not in the kind.
FILESYSTEM_ABSENCE_KIND = "file_absence"


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

    # ── independent filesystem observation (M2.17.x) ──────────────────

    def observe_path(
        self,
        path: Path | str,
        *,
        expected_type: str = "file",
        allowed_roots: Sequence[Path] | None = None,
    ) -> VerificationResult:
        """Independently observe the filesystem and return an adjudicated verdict.

        The one question this answers: *after the mutation, does this path exist,
        and is it the type that was intended?*

        Independence is the whole point, so it is worth being precise about what
        this does **not** consult: not the tool's ``ToolResult.success``, not the
        mutation function's return value, not the model's account of what happened,
        and not any output the mutation produced. Every fact below comes from a
        fresh ``stat`` of the path. A tool that reports success for a file it never
        wrote gets a FAILED verdict here, which is the entire reason this exists.

        Containment is checked *before* observing, and against the resolved path,
        so a symlink or junction pointing out of an allowed root cannot be used to
        manufacture evidence about somewhere else. An unverifiable target is
        UNVERIFIED rather than PASSED - never a soft pass.

        Negative observations request ``FAILED`` rather than ``PASSED`` with
        ``exists=False``. That is deliberate and is existing ledger semantics, not
        a workaround: ``"exists"`` is an ASSERTION_KEY, so ``PASSED`` +
        ``exists=False`` would be downgraded to ``UNVERIFIED`` by ``adjudicate``,
        which would report "I could not verify this" for something that was
        positively observed to be wrong. Requesting ``FAILED`` states what was
        actually seen. ``adjudicate`` only ever gates ``PASSED`` and never upgrades
        a writer's own non-PASSED verdict, so FAILED passes through untouched.

        No file content is read or recorded. The evidence is existence, type and
        size, which is enough for the ledger to adjudicate and cannot carry a
        secret into a durable record.
        """
        want = str(expected_type or "file").strip().lower()
        if want not in ("file", "directory", "any"):
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Unsupported expected_type {expected_type!r}; "
                "no observation was attempted.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": str(expected_type)},
            )

        try:
            target = Path(path)
        except TypeError:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Unusable path {path!r}; no observation was attempted.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want},
            )

        roots = [Path(root) for root in (allowed_roots or [])]
        if not roots:
            # Without a known containment set there is no way to confirm this
            # observation is about a path the agent was allowed to touch. Saying
            # so is correct; guessing would not be.
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "No allowed roots were supplied, so containment could not be "
                "established and no observation was made.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "contained": False},
            )
        if not path_is_within(target, roots):
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Refusing to observe a path outside the allowed roots.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "contained": False,
                 "path": str(target)},
            )

        # Independent observation. resolve() follows symlinks, so what is measured
        # is the real target rather than the link that pointed at it.
        try:
            resolved = target.resolve()
        except OSError as exc:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Path could not be resolved for observation: {exc}",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "path": str(target)},
            )

        exists = resolved.exists()
        observed_type = ("missing" if not exists
                         else "directory" if resolved.is_dir()
                         else "file" if resolved.is_file()
                         else "other")

        size = -1
        if observed_type == "file":
            try:
                size = resolved.stat().st_size
            except OSError:
                # A file that cannot be statted is not a file we can vouch for.
                observed_type = "other"

        evidence: dict = {
            # "exists" is an ASSERTION_KEY: True permits PASSED, False blocks it.
            "exists": bool(exists),
            "path": str(resolved),
            "observed_type": observed_type,
            "expected_type": want,
            "contained": True,
            "size": size,
            "observed_independently": True,
        }

        if not exists:
            return VerificationResult(
                VerificationState.FAILED,
                f"Expected a {want} at {resolved} and the filesystem reports "
                "nothing there.",
                FILESYSTEM_PROBE_KIND, evidence)
        if want != "any" and observed_type != want:
            return VerificationResult(
                VerificationState.FAILED,
                f"Expected a {want} at {resolved} but observed a {observed_type}.",
                FILESYSTEM_PROBE_KIND, evidence)
        if observed_type == "other":
            return VerificationResult(
                VerificationState.FAILED,
                f"{resolved} is neither a readable file nor a directory.",
                FILESYSTEM_PROBE_KIND, evidence)

        return VerificationResult(
            VerificationState.PASSED,
            f"Independent probe observed a {observed_type} of {size} bytes "
            f"at {resolved}.",
            FILESYSTEM_PROBE_KIND, evidence)

    # ── independent filesystem content verification (M2.17.x Task 3) ──────

    def observe_file_content(
        self,
        path: Path | str,
        *,
        expected_content: str,
        allowed_roots: Sequence[Path] | None = None,
    ) -> VerificationResult:
        """Independently observe a file's content and compare with expected content.

        This answers: *after the mutation, does this file contain exactly what was
        requested?*

        Independence is the whole point, so it is worth being precise about what
        this does **not** consult: not the tool's ``ToolResult.success``, not the
        mutation function's return value, not the model's account of what happened,
        and not any output the mutation produced. Every fact below comes from a
        fresh read of the file. A tool that reports success for a file it never
        wrote, or wrote with different content, gets a FAILED verdict here.

        Containment is checked *before* observing, and against the resolved path,
        so a symlink or junction pointing out of an allowed root cannot be used to
        manufacture evidence about somewhere else. An unverifiable target is
        UNVERIFIED rather than PASSED - never a soft pass.

        Content mismatch requests ``FAILED`` rather than ``PASSED`` with
        ``content_match=False``. That is deliberate and is existing ledger semantics:
        ``"content_match"`` is an ASSERTION_KEY, so ``PASSED`` +
        ``content_match=False`` would be downgraded to ``UNVERIFIED`` by
        ``adjudicate``, which would report "could not verify" for something that was
        positively observed to be wrong. Requesting ``FAILED`` states what was
        actually seen. ``adjudicate`` only ever gates ``PASSED`` and never upgrades
        a writer's own non-PASSED verdict, so FAILED passes through untouched.

        File content is read for comparison but never recorded in evidence or logs.
        The evidence carries a safe digest (SHA256) of the observed content and a
        boolean ``content_match`` flag, which is enough for the ledger to adjudicate
        and cannot carry a secret into a durable record.
        """
        want = "file"

        try:
            target = Path(path)
        except TypeError:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Unusable path {path!r}; no observation was attempted.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want},
            )

        roots = [Path(root) for root in (allowed_roots or [])]
        if not roots:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "No allowed roots were supplied, so containment could not be "
                "established and no observation was made.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "contained": False},
            )
        if not path_is_within(target, roots):
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Refusing to observe a path outside the allowed roots.",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "contained": False,
                 "path": str(target)},
            )

        # Independent observation. resolve() follows symlinks, so what is measured
        # is the real target rather than the link that pointed at it.
        try:
            resolved = target.resolve()
        except OSError as exc:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Path could not be resolved for observation: {exc}",
                FILESYSTEM_PROBE_KIND,
                {"expected_type": want, "path": str(target)},
            )

        exists = resolved.exists()
        observed_type = ("missing" if not exists
                         else "directory" if resolved.is_dir()
                         else "file" if resolved.is_file()
                         else "other")

        if not exists:
            return VerificationResult(
                VerificationState.FAILED,
                f"Expected a {want} at {resolved} and the filesystem reports "
                "nothing there.",
                FILESYSTEM_PROBE_KIND,
                {"exists": False, "observed_type": "missing", "expected_type": want,
                 "contained": True, "content_match": False})
        if want != "any" and observed_type != want:
            return VerificationResult(
                VerificationState.FAILED,
                f"Expected a {want} at {resolved} but observed a {observed_type}.",
                FILESYSTEM_PROBE_KIND,
                {"exists": True, "observed_type": observed_type, "expected_type": want,
                 "contained": True, "content_match": False})
        if observed_type == "other":
            return VerificationResult(
                VerificationState.FAILED,
                f"{resolved} is neither a readable file nor a directory.",
                FILESYSTEM_PROBE_KIND,
                {"exists": True, "observed_type": "other", "expected_type": want,
                 "contained": True, "content_match": False})

        # Read the actual content for comparison
        try:
            observed_content = resolved.read_text(encoding="utf-8", newline="")
        except (UnicodeDecodeError, OSError) as exc:
            return VerificationResult(
                VerificationState.FAILED,
                f"File at {resolved} could not be read as UTF-8 text: {exc}",
                FILESYSTEM_PROBE_KIND,
                {"exists": True, "observed_type": "file", "expected_type": want,
                 "contained": True, "content_match": False,
                 "read_error": str(exc)})

        # Compare content exactly (including newlines and whitespace)
        content_match = observed_content == expected_content

        # Compute a safe digest of the observed content for evidence
        # This allows verification that the content was observed without storing it
        observed_digest = hashlib.sha256(observed_content.encode("utf-8")).hexdigest()
        expected_digest = hashlib.sha256(expected_content.encode("utf-8")).hexdigest()

        evidence: dict = {
            "exists": True,
            "path": str(resolved),
            "observed_type": "file",
            "expected_type": want,
            "contained": True,
            "size": len(observed_content.encode("utf-8")),
            "observed_independently": True,
            "content_match": content_match,
            "observed_digest": observed_digest,
            "expected_digest": expected_digest,
        }

        if not content_match:
            return VerificationResult(
                VerificationState.FAILED,
                f"File at {resolved} exists but content does not match expected. "
                f"Observed digest: {observed_digest[:16]}..., expected: {expected_digest[:16]}...",
                FILESYSTEM_PROBE_KIND, evidence)

        return VerificationResult(
            VerificationState.PASSED,
            f"Independent probe verified file content matches expected at {resolved}.",
            FILESYSTEM_PROBE_KIND, evidence)

    # ── independent filesystem absence verification (M2.17.x Task 3) ───────

    def observe_absence(
        self,
        path: Path | str,
        *,
        allowed_roots: Sequence[Path] | None = None,
    ) -> VerificationResult:
        """Independently observe that a path is **gone**, and say so.

        The one question this answers: *after the delete, is there really nothing
        at this path?* It is the mirror image of :meth:`observe_path`, and it
        exists because :meth:`observe_path` cannot answer it. Asking
        ``observe_path(expected_type="file")`` about a successfully deleted file
        returns ``FAILED`` - correctly, for the question it was asked ("is a file
        here?") and wrongly for the operation that ran ("I deleted it"). That
        mismatch drove every successful delete to ``TaskState.FAILED``.

        Like every other probe here, independence is the point: not the tool's
        ``ToolResult.success``, not ``unlink``'s return, and not the model's
        account. Every fact below comes from a fresh ``stat`` of the path.

        Three rules, all inherited from :meth:`observe_path` rather than invented:

        * containment is established **before** any observation, against the
          resolved path, so a symlink or junction pointing out of an allowed root
          cannot manufacture evidence about somewhere else;
        * anything that cannot be established or cannot be observed is
          ``UNVERIFIED`` - never a soft pass;
        * content is never read, so no secret can reach the evidence.

        The assertion is recorded as ``absent`` (see
        ``verification_ledger.ASSERTION_KEYS``). ``exists`` is deliberately
        **not** recorded: for this assertion the two keys would contradict each
        other, and the ledger is right to refuse a contradiction.
        """
        try:
            target = Path(path)
        except TypeError:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Unusable path {path!r}; no observation was attempted.",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent"},
            )

        roots = [Path(root) for root in (allowed_roots or [])]
        if not roots:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "No allowed roots were supplied, so containment could not be "
                "established and no observation was made.",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent", "contained": False},
            )
        if not path_is_within(target, roots):
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Refusing to observe a path outside the allowed roots.",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent", "contained": False,
                 "path": str(target)},
            )

        try:
            resolved = target.resolve()
        except OSError as exc:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                f"Path could not be resolved for observation: {exc}",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent", "path": str(target)},
            )

        # Independent observation. resolve() follows symlinks, so what is
        # measured is the real target rather than the link that pointed at it.
        still_there = resolved.exists() or resolved.is_symlink()

        evidence: dict = {
            # "absent" is an ASSERTION_KEY: True permits PASSED, False blocks it.
            "absent": not still_there,
            "observed_type": "present" if still_there else "missing",
            "expected_type": "absent",
            "contained": True,
            "path": str(resolved),
            "observed_independently": True,
        }

        if still_there:
            return VerificationResult(
                VerificationState.FAILED,
                f"Expected nothing at {resolved} after the delete, but the "
                "filesystem still reports something there.",
                FILESYSTEM_ABSENCE_KIND, evidence)

        return VerificationResult(
            VerificationState.PASSED,
            f"Independent probe observed nothing at {resolved}.",
            FILESYSTEM_ABSENCE_KIND, evidence)

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