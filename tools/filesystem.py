from __future__ import annotations

import shutil
from pathlib import Path

from config.permissions import path_is_within
from config.settings import Settings
from core.logging_setup import get_logger
from core.verification import (
    FILESYSTEM_ABSENCE_KIND,
    FILESYSTEM_PROBE_KIND,
    VerificationResult,
    VerificationState,
    Verifier,
)

from .registry import ToolResult

log = get_logger("filesystem")


def _check_containment(target: Path, settings: Settings | None) -> ToolResult | None:
    """Check if target is within allowed roots. Returns error ToolResult if not contained, None if OK."""
    if settings is None:
        return ToolResult(
            False,
            error=f"Refusing to access {target}: no settings provided, containment could not be established.",
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "No settings supplied, so containment could not be established.",
                FILESYSTEM_PROBE_KIND,
                {"contained": False, "observed_independently": False},
            ),
        )
    roots = list(settings.allowed_roots)
    if not roots:
        return ToolResult(
            False,
            error=f"Refusing to access {target}: no allowed roots configured, containment could not be established.",
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "No allowed roots were supplied, so containment could not be established.",
                FILESYSTEM_PROBE_KIND,
                {"contained": False, "observed_independently": False},
            ),
        )
    # Resolve target to follow symlinks/junctions, then check containment
    try:
        resolved = target.resolve()
    except OSError as exc:
        return ToolResult(
            False,
            error=f"Path could not be resolved: {exc}",
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                f"Path could not be resolved for containment check: {exc}",
                FILESYSTEM_PROBE_KIND,
                {"contained": False, "observed_independently": False},
            ),
        )
    if not path_is_within(resolved, roots):
        return ToolResult(
            False,
            error=f"Refusing to access {resolved}: outside the allowed roots.",
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "Refusing to access a path outside the allowed roots.",
                FILESYSTEM_PROBE_KIND,
                {"contained": False, "path": str(resolved), "observed_independently": False},
            ),
        )
    return None


def read_file(path: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    # Containment check BEFORE any filesystem access
    containment_error = _check_containment(target, settings)
    if containment_error is not None:
        return containment_error

    if not target.exists():
        return ToolResult(False, error=f"File not found: {target}")
    if not target.is_file():
        return ToolResult(False, error=f"Not a file: {target}")
    if target.stat().st_size > 512 * 1024:
        return ToolResult(False, error=f"File too large to read ({target.stat().st_size} bytes).")
    try:
        content = target.read_text(encoding="utf-8", newline="")
        result = ToolResult(True, output=content)
        if settings is not None:
            try:
                roots = list(settings.allowed_roots)
                vr = Verifier().observe_path(target, expected_type="file", allowed_roots=roots)
                result.verification = vr
            except Exception as exc:  # noqa: BLE001 - a verifier must not fail the mutation
                log.warning("independent filesystem observation failed for %s: %s", path, exc)
                result.verification = VerificationResult(
                    VerificationState.UNVERIFIED,
                    f"Independent observation could not be completed: {exc}",
                    FILESYSTEM_PROBE_KIND,
                    {"observed_independently": False},
                )
        return result
    except (UnicodeDecodeError, OSError) as exc:
        return ToolResult(False, error=f"Cannot read {target}: {exc}")


def write_file(path: str, content: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    # Containment check BEFORE any filesystem mutation
    containment_error = _check_containment(target, settings)
    if containment_error is not None:
        return containment_error

    try:
        target = target.resolve()
        # Re-check containment after resolve in case of symlinks
        containment_error = _check_containment(target, settings)
        if containment_error is not None:
            return containment_error

        target.parent.mkdir(parents=True, exist_ok=True)
        backup = _create_backup(target, settings)
        target.write_text(content, encoding="utf-8", newline="")
        extra = f" (backup: {backup.name})" if backup else ""
        result = ToolResult(True, output=f"Wrote {target} ({target.stat().st_size} bytes){extra}")
        if settings is not None:
            try:
                roots = list(settings.allowed_roots)
                vr = Verifier().observe_file_content(target, expected_content=content,
                                                      allowed_roots=roots)
                result.verification = vr
            except Exception as exc:  # noqa: BLE001 - a verifier must not fail the mutation
                log.warning("independent filesystem content verification failed for %s: %s", path, exc)
                result.verification = VerificationResult(
                    VerificationState.UNVERIFIED,
                    f"Independent content verification could not be completed: {exc}",
                    FILESYSTEM_PROBE_KIND,
                    {"observed_independently": False},
                )
        return result
    except OSError as exc:
        return ToolResult(False, error=f"Cannot write {target}: {exc}")


def observe_after_write(path: str, content_length: int, settings: Settings | None = None):
    """Independent observation of a completed write. NOT wired to ``write_file``.

    Deliberately not called by ``write_file``, and that is the whole point of this
    entry existing separately. An experiment wired it in, and the result was a real
    over-claiming regression rather than a stale test premise:

      * the probe observed each written file independently and every observation was
        true (right path, right type, right size);
      * each observation was adjudicated ``PASSED`` by the existing ledger, exactly
        as designed;
      * the enclosing task therefore ended ``COMPLETED + PASSED``, so
        ``terminal_verdict`` reported ``VERIFIED`` and ``task_succeeded()`` returned
        True - on a scaffold whose test file was deliberately broken and whose
        ``project.verify`` step never ran. The only thing that made it "verified"
        was that some files existed.

    An existence probe establishes "a file is there". It does not establish "the
    content I was asked to write is in it", and the task-level verdict is read as
    the latter. Attaching a partial observation to a mutation therefore
    over-claims, which is the precise failure the M2.13 workflow tests exist to
    catch.

    The fix is not to weaken the ledger or the invariant - it is for the observation
    to assert what the step actually established, which means verifying content.
    That is Task 3. Until then this helper is available and unused, and
    ``tests/test_m217_filesystem_verification.py`` pins both the capability and the
    regression it must not reintroduce.
    """
    from core.verification import Verifier

    roots = list(settings.allowed_roots) if settings is not None else []
    try:
        return Verifier().observe_path(path, expected_type="file",
                                       allowed_roots=roots)
    except Exception as exc:  # noqa: BLE001 - a verifier must not fail the mutation
        log.warning("independent filesystem observation failed for %s: %s", path, exc)
        from core.verification import (
            FILESYSTEM_PROBE_KIND,
            VerificationResult,
            VerificationState,
        )

        return VerificationResult(
            VerificationState.UNVERIFIED,
            f"Independent observation could not be completed: {exc}",
            FILESYSTEM_PROBE_KIND,
            {"observed_independently": False},
        )


def list_dir(path: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    # Containment check BEFORE any filesystem access
    containment_error = _check_containment(target, settings)
    if containment_error is not None:
        return containment_error

    if not target.exists():
        return ToolResult(False, error=f"Path not found: {target}")
    if not target.is_dir():
        return ToolResult(False, error=f"Not a directory: {target}")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    result = ToolResult(True, output="\n".join(names) if names else "(empty)")
    if settings is not None:
        try:
            roots = list(settings.allowed_roots)
            vr = Verifier().observe_path(target, expected_type="directory", allowed_roots=roots)
            result.verification = vr
        except Exception as exc:  # noqa: BLE001 - a verifier must not fail the mutation
            log.warning("independent filesystem observation failed for %s: %s", path, exc)
            result.verification = VerificationResult(
                VerificationState.UNVERIFIED,
                f"Independent observation could not be completed: {exc}",
                FILESYSTEM_PROBE_KIND,
                {"observed_independently": False},
            )
    return result


def delete_file(path: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    if not target.exists():
        return ToolResult(False, error=f"File not found: {target}")
    if target.is_dir():
        return ToolResult(False, error="Refusing to delete a directory via this tool. Use an explicit file path.")

    # ── containment, BEFORE any mutation ─────────────────────────────────────
    # This tool is SENSITIVE unconditionally (tools/__init__.py), and the
    # permission layer escalates further for out-of-root targets, so through the
    # Agent a delete outside the roots already needs explicit confirmation. That
    # is not enough on its own: a direct library call bypassed the permission
    # layer entirely and unlinked the file, then merely reported UNVERIFIED
    # afterwards. Containment is checked here as well, before ``unlink()``.
    #
    # ``path_is_within`` resolves both sides, so a symlink or junction pointing
    # out of an allowed root is measured at its real target and refused.
    #
    # Missing settings or missing roots fail CLOSED: with nothing to check
    # containment against, "could not check" is not permission. The result still
    # carries an UNVERIFIED absence verdict so callers and the ledger see that no
    # independent observation backed a mutation - which is exactly true, because
    # no mutation happened.
    roots = list(settings.allowed_roots) if settings is not None else []
    if not roots:
        return ToolResult(
            False,
            error=(f"Refusing to delete {target}: no allowed roots are configured, "
                   f"so containment could not be established. Nothing deleted."),
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "No allowed roots were supplied, so containment could not be "
                "established. Nothing was deleted.",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent", "contained": False,
                 "observed_independently": False},
            ),
        )
    if not path_is_within(target, roots):
        return ToolResult(
            False,
            error=(f"Refusing to delete {target}: outside the allowed roots. "
                   f"Nothing deleted."),
            verification=VerificationResult(
                VerificationState.UNVERIFIED,
                "Refusing to delete a path outside the allowed roots. Nothing "
                "was deleted.",
                FILESYSTEM_ABSENCE_KIND,
                {"expected_type": "absent", "contained": False,
                 "path": str(target), "observed_independently": False},
            ),
        )

    try:
        target.unlink()
        result = ToolResult(True, output=f"Deleted {target}")
        if settings is not None:
            try:
                # An ABSENCE assertion, not a presence one. ``observe_path``
                # answers "is a file here?" and a successful delete guarantees the
                # answer is no, so asking it here adjudicated every working delete
                # FAILED and drove the whole task to ``TaskState.FAILED``.
                vr = Verifier().observe_absence(target, allowed_roots=roots)
                result.verification = vr
            except Exception as exc:  # noqa: BLE001 - a verifier must not fail the mutation
                log.warning("independent filesystem observation failed for %s: %s", path, exc)
                result.verification = VerificationResult(
                    VerificationState.UNVERIFIED,
                    f"Independent observation could not be completed: {exc}",
                    FILESYSTEM_PROBE_KIND,
                    {"observed_independently": False},
                )
        return result
    except OSError as exc:
        return ToolResult(False, error=f"Cannot delete {target}: {exc}")


def _create_backup(target: Path, settings: Settings | None) -> Path | None:
    if not target.exists():
        return None
    backups = (settings.backups_dir if settings else Path("data/backups"))
    backups.mkdir(parents=True, exist_ok=True)
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backups / f"{target.stem}.{stamp}{''.join(target.suffixes)}.bak"
    shutil.copy2(target, backup)
    log.info("backup created: %s -> %s", target, backup)
    return backup


def within_allowed(path: str, settings: Settings) -> bool:
    return path_is_within(Path(path), settings.allowed_roots)