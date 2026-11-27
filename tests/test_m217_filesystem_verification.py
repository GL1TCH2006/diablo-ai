"""M2.17.x Task 2 - independent file existence verification.

The capability under test is ``core.verification.Verifier.observe_path``: after a
filesystem mutation, it re-reads the filesystem and reports what it actually found,
as a ``VerificationResult`` the existing ledger can adjudicate.

What these tests are really guarding
-----------------------------------
Every one of them exists because a *claim* and an *observation* are different
things. The verifier must not be satisfied by:

* a tool reporting ``success=True`` for a file that was never written;
* a mutation function's own return value, including its own byte count;
* a model saying the work happened;
* the target having existed a moment ago.

and it must not manufacture a PASSED for a path it was not allowed to observe.

Three semantics from Task 1 are pinned deliberately, because they look like bugs
and are not:

1. A negative observation requests **FAILED**, never ``PASSED`` with
   ``exists=False``. ``exists`` is an ASSERTION_KEY, so the latter would be
   downgraded to UNVERIFIED by ``adjudicate`` - reporting "could not verify" for
   something positively observed to be wrong.
2. An unobservable target (unresolvable, uncontained, no roots) is **UNVERIFIED**,
   never PASSED. UNVERIFIED is not a soft pass: it yields no goal evidence.
3. ``adjudicate`` and ``_validate_evidence`` are not modified by any of this.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.verification import (
    FILESYSTEM_PROBE_KIND,
    VerificationResult,
    VerificationState,
    Verifier,
)


@pytest.fixture()
def roots(tmp_path: Path) -> list[Path]:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return [allowed]


@pytest.fixture()
def verifier() -> Verifier:
    return Verifier()


def _observe(verifier: Verifier, path, *, expected_type="file", roots=None):
    return verifier.observe_path(path, expected_type=expected_type,
                                 allowed_roots=roots)


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 1: existing file Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_an_existing_file_is_observed_as_present_and_typed(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "notes.txt"
    target.write_text("hello", encoding="utf-8")

    result = _observe(verifier, target, expected_type="file", roots=roots)

    assert result.state is VerificationState.PASSED
    assert result.evidence["exists"] is True
    assert result.evidence["observed_type"] == "file"
    assert result.evidence["expected_type"] == "file"
    assert result.evidence["size"] == 5
    assert result.evidence["contained"] is True


def test_an_empty_file_is_still_a_file(verifier: Verifier,
                                       roots: list[Path]) -> None:
    """``check_file`` rejects an empty unknown-extension file; a probe must not.

    ``Verifier.check_file`` refuses a zero-byte file with an unrecognised suffix.
    That heuristic is about whether an *edit* is meaningful, not about whether a
    *path* is a file, so reusing it for existence verification would have made a
    legitimately empty write unverifiable.
    """
    target = roots[0] / "empty.bin"
    target.write_bytes(b"")

    result = _observe(verifier, target, expected_type="file", roots=roots)

    assert result.state is VerificationState.PASSED
    assert result.evidence["size"] == 0


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 2: existing directory Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_an_existing_directory_is_observed_as_a_directory(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "subdir"
    target.mkdir()

    result = _observe(verifier, target, expected_type="directory", roots=roots)

    assert result.state is VerificationState.PASSED
    assert result.evidence["exists"] is True
    assert result.evidence["observed_type"] == "directory"


def test_any_accepts_either_type(verifier: Verifier, roots: list[Path]) -> None:
    a_file = roots[0] / "f.txt"
    a_file.write_text("x", encoding="utf-8")
    a_dir = roots[0] / "d"
    a_dir.mkdir()

    assert _observe(verifier, a_file, expected_type="any",
                    roots=roots).state is VerificationState.PASSED
    assert _observe(verifier, a_dir, expected_type="any",
                    roots=roots).state is VerificationState.PASSED


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 3: missing target Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_a_missing_target_is_not_claimed_to_exist(
        verifier: Verifier, roots: list[Path]) -> None:
    result = _observe(verifier, roots[0] / "nope.txt", expected_type="file",
                      roots=roots)

    assert result.state is not VerificationState.PASSED
    assert result.evidence["exists"] is False
    assert result.evidence["observed_type"] == "missing"
    # A definite negative observation is a FAILED, not a PASSED-with-a-false-flag.
    assert result.state is VerificationState.FAILED


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 4: wrong type Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_a_directory_where_a_file_was_expected_is_not_passed(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "actually_a_dir"
    target.mkdir()

    result = _observe(verifier, target, expected_type="file", roots=roots)

    assert result.state is VerificationState.FAILED
    assert result.evidence["exists"] is True          # it does exist...
    assert result.evidence["observed_type"] == "directory"  # ...but not as a file
    assert result.evidence["expected_type"] == "file"
    assert "directory" in result.detail


def test_a_file_where_a_directory_was_expected_is_not_passed(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "actually_a_file.txt"
    target.write_text("x", encoding="utf-8")

    result = _observe(verifier, target, expected_type="directory", roots=roots)

    assert result.state is VerificationState.FAILED
    assert result.evidence["observed_type"] == "file"


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 5: a mutation result cannot fake verification Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_a_successful_tool_result_does_not_substitute_for_observation(
        roots: list[Path]) -> None:
    """The claim says success. The filesystem says otherwise. The filesystem wins."""
    ghost = roots[0] / "ghost.txt"

    # A tool result claiming a write that never happened.
    claimed = type("FakeResult", (), {"success": True,
                                      "output": f"Wrote {ghost} (12 bytes)"})()

    result = Verifier().observe_path(ghost, expected_type="file",
                                     allowed_roots=roots)

    assert claimed.success is True                 # the claim
    assert not ghost.exists()                      # reality
    assert result.state is VerificationState.FAILED  # the verdict follows reality


def test_the_observation_helper_observes_a_real_write(
        tmp_path: Path) -> None:
    """The un-wired helper still performs a genuine independent observation."""
    from config.settings import load_settings
    from tools.filesystem import observe_after_write, write_file

    settings = load_settings()
    settings.allowed_roots = [tmp_path]
    target = tmp_path / "written.txt"
    assert write_file(str(target), "content here", settings).success is True

    result = observe_after_write(str(target), len("content here"), settings)

    assert isinstance(result, VerificationResult)
    assert result.state is VerificationState.PASSED
    assert result.evidence["exists"] is True
    assert result.evidence["observed_type"] == "file"
    # The probe's size is its own observation, not the string the writer printed.
    assert result.evidence["size"] == len("content here")


def test_a_deep_write_is_created_and_then_observed(tmp_path: Path) -> None:
    from config.settings import load_settings
    from tools.filesystem import observe_after_write, write_file

    settings = load_settings()
    settings.allowed_roots = [tmp_path]
    target = tmp_path / "a" / "b" / "deep.txt"

    assert write_file(str(target), "deep", settings).success is True
    assert observe_after_write(
        str(target), 4, settings).state is VerificationState.PASSED
    assert target.is_file()


# Ã¢â€â‚¬Ã¢â€â‚¬ THE REGRESSION THIS TASK FOUND, PINNED Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_write_file_attaches_content_verification(
        tmp_path: Path) -> None:
    """Task 3: write_file now attaches independent content verification.

    This replaces the Task 2 guard test. The failure it guarded against was
    an existence-only probe that would promote a write task to VERIFIED even
    when content was wrong. Task 3 fixes this by attaching a content verification
    that independently reads the file and compares its content with what was
    requested.

    The verification must:
    - Be present (not None)
    - Carry a content_match assertion (not just exists)
    - Be adjudicated PASSED when content matches exactly
    - Be adjudicated FAILED when content differs
    - Never include file content in evidence (only digests)
    """
    from config.settings import load_settings
    from tools.filesystem import write_file
    from core.verification import VerificationState, FILESYSTEM_PROBE_KIND
    from core.verification_ledger import ASSERTION_KEYS

    settings = load_settings()
    settings.allowed_roots = [tmp_path]
    target = tmp_path / "content_verified.txt"
    expected_content = "hello world\nwith newlines"

    result = write_file(str(target), expected_content, settings)

    assert result.success is True
    assert result.verification is not None, (
        "Task 3: write_file must attach a content verification")
    vr = result.verification

    # Must be a content verification (using the existing file_and_output kind
    # but with content_match assertion)
    assert vr.kind == FILESYSTEM_PROBE_KIND
    assert vr.state is VerificationState.PASSED

    # Evidence must contain content_match assertion (an ASSERTION_KEY)
    assert "content_match" in vr.evidence, (
        "evidence must carry content_match assertion")
    assert vr.evidence["content_match"] is True
    assert vr.evidence["exists"] is True
    assert vr.evidence["observed_type"] == "file"
    assert vr.evidence["observed_independently"] is True

    # Must include safe digests, not raw content
    assert "observed_digest" in vr.evidence
    assert "expected_digest" in vr.evidence
    assert vr.evidence["observed_digest"] == vr.evidence["expected_digest"]

    # Must NOT include raw file content in evidence or detail
    secret = expected_content
    assert secret not in str(vr.evidence)
    assert secret not in vr.detail

    # Must be adjudicatable by the real ledger
    from core.task_manager import TaskManager
    tasks = TaskManager()
    entry = tasks.record_verification(vr)
    assert entry.state is VerificationState.PASSED, entry.reason
    assert entry.subject_kind.value == "file"
    assert entry.trust.value == "observed"

    # File must actually exist with correct content
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == expected_content


def test_an_existence_probe_alone_cannot_certify_content(
        verifier: Verifier, roots: list[Path]) -> None:
    """The precise limit of what Task 2's probe can justify.

    A file with entirely the wrong contents is still an existing file. The probe
    says so honestly - which is exactly why its verdict must not be read as "the
    write was correct". This test documents the boundary Task 3 closes.
    """
    target = roots[0] / "wrong_contents.txt"
    target.write_text("this is not what was asked for", encoding="utf-8")

    result = _observe(verifier, target, expected_type="file", roots=roots)

    assert result.state is VerificationState.PASSED   # it does exist...
    assert result.evidence["exists"] is True
    # No value other than the path itself may speak about content. (An earlier
    # version of this assertion matched the *filename* "wrong_contents.txt".)
    claims = {key: value for key, value in result.evidence.items()
              if key != "path"}
    assert not any("content" in str(value).lower()
                   for value in claims.values()), (
        "the evidence makes no claim about content - it cannot")
    assert set(claims) == {"exists", "observed_type", "expected_type",
                           "contained", "size", "observed_independently"}


# Ã¢â€â‚¬Ã¢â€â‚¬ TASK 3: content verification for write_file Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_a_file_removed_before_verification_is_not_passed(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "transient.txt"
    target.write_text("here for now", encoding="utf-8")
    assert _observe(verifier, target, roots=roots).state is VerificationState.PASSED

    target.unlink()

    after = _observe(verifier, target, roots=roots)
    assert after.state is VerificationState.FAILED
    assert after.evidence["exists"] is False


def test_a_verifier_holding_a_stale_path_cannot_report_a_pass(
        verifier: Verifier, roots: list[Path]) -> None:
    """Re-observing after deletion must not reuse an earlier verdict."""
    target = roots[0] / "reused.txt"
    target.write_text("first", encoding="utf-8")
    first = _observe(verifier, target, roots=roots)
    target.unlink()
    second = _observe(verifier, target, roots=roots)

    assert first.state is VerificationState.PASSED
    assert second.state is VerificationState.FAILED


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 7: outside allowed root Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_a_path_outside_the_allowed_root_is_never_passed(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret-ish", encoding="utf-8")

    result = _observe(verifier, outside, expected_type="file", roots=roots)

    assert result.state is VerificationState.UNVERIFIED
    assert result.state is not VerificationState.PASSED
    assert result.evidence["contained"] is False


def test_no_allowed_roots_means_no_observation_at_all(
        verifier: Verifier, roots: list[Path]) -> None:
    """Without a containment set the probe declines rather than guessing."""
    target = roots[0] / "in_root.txt"
    target.write_text("x", encoding="utf-8")

    result = _observe(verifier, target, expected_type="file", roots=None)

    assert result.state is VerificationState.UNVERIFIED
    assert "allowed roots" in result.detail


def test_an_uncontained_target_produces_no_passed_observation(
        tmp_path: Path) -> None:
    from config.settings import load_settings
    from tools.filesystem import observe_after_write

    settings = load_settings()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    settings.allowed_roots = [allowed]
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")

    result = observe_after_write(str(outside), 1, settings)

    assert result.state is VerificationState.UNVERIFIED
    assert result.state is not VerificationState.PASSED
    assert result.evidence["contained"] is False


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 8: traversal Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


@pytest.mark.parametrize("escape", [
    "../escaped.txt",
    "sub/../../escaped.txt",
    "./../escaped.txt",
    "..",
])
def test_traversal_out_of_the_allowed_root_is_refused(
        verifier: Verifier, roots: list[Path], escape: str) -> None:
    inner = roots[0] / "sub"
    inner.mkdir()
    candidate = inner / escape

    result = _observe(verifier, candidate, expected_type="file", roots=roots)

    assert result.state is not VerificationState.PASSED


def test_a_traversal_that_stays_inside_is_allowed(
        verifier: Verifier, roots: list[Path]) -> None:
    """Containment is about the destination, not about the presence of ".."."""
    inner = roots[0] / "sub"
    inner.mkdir()
    target = roots[0] / "inside.txt"
    target.write_text("ok", encoding="utf-8")

    result = _observe(verifier, inner / ".." / "inside.txt",
                      expected_type="file", roots=roots)

    assert result.state is VerificationState.PASSED


def test_a_dot_dot_naming_a_real_outside_file_is_refused(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    inner = roots[0] / "sub"
    inner.mkdir()

    result = _observe(verifier, inner / ".." / ".." / "outside.txt",
                      expected_type="file", roots=roots)

    assert result.state is not VerificationState.PASSED


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 9: symlink / junction safety Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


@pytest.mark.skipif(not hasattr(os, "symlink"),
                    reason="this platform has no symlink support")
def test_a_symlink_pointing_out_of_the_allowed_root_is_not_passed(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside_target.txt"
    outside.write_text("outside content", encoding="utf-8")
    link = roots[0] / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable here: {exc}")

    result = _observe(verifier, link, expected_type="file", roots=roots)

    # path_is_within resolves before comparing, so a link that leaves the root is
    # refused rather than followed into evidence about somewhere else.
    assert result.state is not VerificationState.PASSED


@pytest.mark.skipif(not hasattr(os, "symlink"),
                    reason="this platform has no symlink support")
def test_a_symlink_pointing_inside_the_allowed_root_is_observed(
        verifier: Verifier, roots: list[Path]) -> None:
    real = roots[0] / "real.txt"
    real.write_text("inside", encoding="utf-8")
    link = roots[0] / "inside_link.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable here: {exc}")

    result = _observe(verifier, link, expected_type="file", roots=roots)

    assert result.state is VerificationState.PASSED


def _make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction. No elevation required, unlike symlink.

    Junction coverage matters here precisely because symlink coverage is
    environment-dependent: on a stock Windows account ``symlink_to`` fails with
    WinError 1314, so without a junction the traversal-through-a-link case would
    go untested everywhere it is actually run.
    """
    import subprocess

    try:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and link.exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows facility")
def test_a_junction_escaping_the_allowed_root_is_not_passed(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "target.txt").write_text("outside", encoding="utf-8")
    junction = roots[0] / "escape"
    if not _make_junction(junction, outside):
        pytest.skip("junction creation unavailable on this host")

    result = _observe(verifier, junction, expected_type="directory", roots=roots)

    assert result.state is not VerificationState.PASSED
    assert result.evidence.get("contained") is False


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows facility")
def test_a_junction_targeting_inside_the_allowed_root_is_observed(
        verifier: Verifier, roots: list[Path]) -> None:
    real_dir = roots[0] / "real_dir"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_text("inside", encoding="utf-8")
    junction = roots[0] / "inside_junction"
    if not _make_junction(junction, real_dir):
        pytest.skip("junction creation unavailable on this host")

    result = _observe(verifier, junction, expected_type="directory", roots=roots)

    assert result.state is VerificationState.PASSED
    assert result.evidence["observed_type"] == "directory"


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows facility")
def test_a_file_reached_through_an_escaping_junction_is_not_passed(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    """The file case, not just the directory case: reading through the escape."""
    outside = tmp_path / "outside_dir2"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be observable", encoding="utf-8")
    junction = roots[0] / "escape2"
    if not _make_junction(junction, outside):
        pytest.skip("junction creation unavailable on this host")

    result = _observe(verifier, junction / "secret.txt", expected_type="file",
                      roots=roots)

    assert result.state is not VerificationState.PASSED
    assert "must not be observable" not in str(result.evidence)


# Ã¢â€â‚¬Ã¢â€â‚¬ TEST 10: evidence shape Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_evidence_carries_an_explicit_assertion_not_a_narrative(
        verifier: Verifier, roots: list[Path]) -> None:
    from core.verification_ledger import ASSERTION_KEYS

    target = roots[0] / "shape.txt"
    target.write_text("x", encoding="utf-8")

    evidence = _observe(verifier, target, roots=roots).evidence

    assert "exists" in evidence
    assert isinstance(evidence["exists"], bool)
    assert ASSERTION_KEYS & set(evidence), "no ledger assertion key present"
    assert all(isinstance(key, str) for key in evidence)


def test_evidence_never_contains_file_content(
        verifier: Verifier, roots: list[Path]) -> None:
    secret = "sk-should-never-reach-the-ledger-0123456789"
    target = roots[0] / "secret.txt"
    target.write_text(secret, encoding="utf-8")

    result = _observe(verifier, target, roots=roots)

    assert secret not in str(result.evidence)
    assert secret not in result.detail


def test_evidence_survives_the_existing_ledger_adjudication(
        verifier: Verifier, roots: list[Path]) -> None:
    """A PASSED probe must actually be admissible by the real funnel."""
    from core.task_manager import TaskManager

    target = roots[0] / "adjudicated.txt"
    target.write_text("payload", encoding="utf-8")
    result = _observe(verifier, target, roots=roots)
    assert result.state is VerificationState.PASSED

    tasks = TaskManager()
    entry = tasks.record_verification(result)

    assert entry.state is VerificationState.PASSED, entry.reason
    assert entry.subject_kind.value == "file"
    assert entry.trust.value == "observed"
    assert tasks.verification is VerificationState.PASSED


def test_a_failed_probe_stays_failed_through_the_ledger(
        verifier: Verifier, roots: list[Path]) -> None:
    from core.task_manager import TaskManager

    result = _observe(verifier, roots[0] / "absent.txt", roots=roots)
    assert result.state is VerificationState.FAILED

    entry = TaskManager().record_verification(result)

    assert entry.state is VerificationState.FAILED


def test_an_unverified_probe_stays_unverified_through_the_ledger(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    from core.task_manager import TaskManager

    outside = tmp_path / "far.txt"
    outside.write_text("x", encoding="utf-8")
    result = _observe(verifier, outside, roots=roots)
    assert result.state is VerificationState.UNVERIFIED

    entry = TaskManager().record_verification(result)

    assert entry.state is VerificationState.UNVERIFIED
    assert entry.state is not VerificationState.PASSED


# Ã¢â€â‚¬Ã¢â€â‚¬ NEGATIVE ASSERTION SEMANTICS (explicitly protected) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_the_probe_never_requests_passed_with_a_false_assertion(
        verifier: Verifier, roots: list[Path], tmp_path: Path) -> None:
    """``exists=False`` must never accompany a requested PASSED.

    If this ever regresses, ``adjudicate`` would downgrade the entry to UNVERIFIED
    and a *positively observed wrong state* would be reported as merely
    unverified. The ledger is unchanged; this asserts the producer respects it.
    """
    from core.verification_ledger import ASSERTION_KEYS

    candidates = [
        roots[0] / "missing.txt",
        roots[0],                       # a directory seen as a file
        tmp_path / "outside.txt",       # out of root
    ]
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")

    for path in candidates:
        result = _observe(verifier, path, expected_type="file", roots=roots)
        for key in ASSERTION_KEYS & set(result.evidence):
            if result.state is VerificationState.PASSED:
                assert result.evidence[key] is True, (
                    f"PASSED with {key}=False for {path}")
        assert result.state is not VerificationState.PASSED or \
            result.evidence["exists"] is True


def test_adjudicate_still_downgrades_a_false_assertion(
        tmp_path: Path) -> None:
    """Prove the protection is real, by exercising the ledger directly.

    Deliberately constructs the illegal request the probe must never make. If the
    ledger stopped downgrading it, Task 2's producer-side discipline would stop
    mattering - so this test guards the ledger too, without modifying it.
    """
    from core.task_manager import TaskManager
    from core.verification import FILESYSTEM_PROBE_KIND

    illegal = VerificationResult(
        VerificationState.PASSED,
        "deliberately illegal: claims PASSED while asserting absence",
        FILESYSTEM_PROBE_KIND,
        {"exists": False, "observed_type": "missing"},
    )

    entry = TaskManager().record_verification(illegal)

    assert entry.state is VerificationState.UNVERIFIED
    assert entry.requested_state is VerificationState.PASSED
    assert "exists" in entry.reason


def test_narration_alone_cannot_produce_a_passed_entry() -> None:
    """A string is narration, not evidence - existing ledger semantics."""
    from core.task_manager import TaskManager

    narrative = VerificationResult(
        VerificationState.PASSED,
        "I wrote the file and it is definitely there.",
        FILESYSTEM_PROBE_KIND,
        "the file exists and is correct",   # a bare string, deliberately
    )

    entry = TaskManager().record_verification(narrative)

    assert entry.state is VerificationState.UNVERIFIED
    assert "string" in entry.reason or "evidence" in entry.reason


# Ã¢â€â‚¬Ã¢â€â‚¬ housekeeping Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def test_an_unsupported_expected_type_is_unverified_not_passed(
        verifier: Verifier, roots: list[Path]) -> None:
    target = roots[0] / "x.txt"
    target.write_text("x", encoding="utf-8")

    result = _observe(verifier, target, expected_type="socket", roots=roots)

    assert result.state is VerificationState.UNVERIFIED


def test_the_probe_uses_the_existing_kind_and_adds_no_new_vocabulary() -> None:
    from core.verification_ledger import LEGACY_KIND_AXES, resolve_legacy_kind

    assert FILESYSTEM_PROBE_KIND == "file_and_output"
    assert FILESYSTEM_PROBE_KIND in LEGACY_KIND_AXES
    subject, method = resolve_legacy_kind(FILESYSTEM_PROBE_KIND)
    assert subject.value == "file"
    assert method.value == "file_and_output"


def test_the_verifier_creates_no_second_ledger_or_state_vocabulary() -> None:
    """Architecture guard: the probe is a producer, not a subsystem."""
    import inspect

    import core.verification as module

    source = inspect.getsource(module)
    # Definitions, not words: an earlier version of this guard matched the string
    # "adjudicate" inside the new docstring and failed on its own prose.
    for forbidden in ("class VerificationLedger", "class EvidenceTrust",
                      "class VerificationSubject", "class VerificationMethod",
                      "class TerminalVerdict", "def adjudicate",
                      "def _validate_evidence", "def record_verification"):
        assert forbidden not in source, (
            f"core/verification.py defined {forbidden}; the probe must reuse the "
            "existing verification architecture, not duplicate it")
    # The single existing Verifier gained a method; no second verifier class.
    assert [name for name, obj in vars(module).items()
            if inspect.isclass(obj) and name.endswith("Verifier")
            and obj.__module__ == module.__name__] == ["Verifier"]


def test_no_new_permission_or_path_resolution_mechanism() -> None:
    """Containment must stay the one that already exists."""
    import inspect

    import core.verification as module
    from config.permissions import path_is_within

    source = inspect.getsource(module.Verifier.observe_path)
    assert "path_is_within" in source, (
        "the probe must reuse PermissionManager's containment helper")
    for forbidden in ("PermissionManager(", "PermissionLevel",
                      "allowed_roots =", "settings.workspace_root"):
        assert forbidden not in source, (
            f"observe_path introduced {forbidden}; containment and permissions "
            "are owned elsewhere")
    assert callable(path_is_within)
