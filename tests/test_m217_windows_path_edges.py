"""Windows path edge cases for the filesystem verification and containment code.

Purpose
=======

These are **characterization** tests. They pin what the containment code actually
does on the awkward path spellings Windows accepts, so a future change that
widens access shows up as a failing test rather than as a quiet regression.

The rule every test here obeys: a path may only ever come back
``PASSED``/``UNVERIFIED``/``FAILED`` *without the check having read or claimed
something it did not actually see*. ``FAILED`` is an acceptable answer - it is
the conservative one. ``PASSED`` is the dangerous one, because it is the only
state that lets the agent say "done".

Where a test found something genuinely unsafe in production code it is marked
``xfail(strict=True)`` and named after the finding, so the suite stays green while
the defect stays recorded, and the day somebody fixes it the marker becomes a
failure that asks to be removed. See ``data/run_reports/b4_09_path_findings.md``.

Nothing here touches the real filesystem outside a pytest temp directory, and
nothing opens a window. Two symlink tests skip when the process lacks the
SeCreateSymbolicLinkPrivilege (Windows error 1314).
"""

from __future__ import annotations

import ctypes
import tempfile
from pathlib import Path

import pytest

from config.permissions import path_is_within
from config.settings import Settings
from core.verification import (
    FILESYSTEM_ABSENCE_KIND,
    Verifier,
    VerificationState,
)
from tools import build_registry, filesystem


# ═══════════════════════════════════════════════════════════════ fixtures


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir(parents=True, exist_ok=True)
    (root / "inside.txt").write_text("inside\n", encoding="utf-8")
    return root


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    return outside


def _settings(root: Path) -> Settings:
    return Settings(base_dir=root, workspace_root=root, allowed_roots=[root], raw={})


def _assert_never_passed(result, label: str) -> None:
    """The one assertion that matters most: the check must not claim success."""
    assert result.state is not VerificationState.PASSED, (
        f"{label}: the verifier claimed PASSED for a path it should have refused "
        f"or could not see - {result.detail}")


def _assert_contained_or_unverified(result, label: str) -> None:
    assert result.state is not VerificationState.FAILED or result.evidence.get(
        "observed_type") == "missing", (
        f"{label}: FAILED with a reason other than 'the filesystem reports "
        f"nothing there', which would mean a real read went wrong: {result.detail}")


# ═══════════════════════════════════════════ spellings inside the allowed root


def test_mixed_case_spellings_are_contained(tmp_path: Path):
    """Windows paths are case-insensitive; the containment check must agree."""
    root = _root(tmp_path)

    for name in ("INSIDE.TXT", "inside.txt", "InSiDe.TxT"):
        result = Verifier().observe_path(root / name, allowed_roots=[root])
        assert result.state is VerificationState.PASSED, (
            f"{name} exists inside the allowed root but was not recognised as "
            f"contained: {result.state.value} - {result.detail}")
        assert result.evidence["contained"] is True


def test_forward_and_backward_slashes_are_equivalent(tmp_path: Path):
    root = _root(tmp_path)
    mixed = str(root).replace("\\", "/") + "/inside.txt"

    result = Verifier().observe_path(Path(mixed), allowed_roots=[root])

    assert result.state is VerificationState.PASSED, (
        f"a forward-slash spelling of an in-root file was not recognised: "
        f"{result.detail}")


def test_a_trailing_dot_resolves_to_the_real_in_root_file(tmp_path: Path):
    """Win32 strips a trailing dot, so ``inside.txt.`` IS ``inside.txt``.

    Containment holds here because the name resolves *into* the root. That is the
    behaviour worth pinning: it must never resolve the other way.
    """
    root = _root(tmp_path)

    result = Verifier().observe_path(str(root / "inside.txt") + ".", allowed_roots=[root])

    assert result.state is VerificationState.PASSED, (
        f"the trailing-dot spelling of an in-root file was not recognised: "
        f"{result.detail}")
    assert result.evidence["contained"] is True


def test_a_trailing_space_resolves_to_the_real_in_root_file(tmp_path: Path):
    """Win32 strips trailing spaces from a path component."""
    root = _root(tmp_path)

    result = Verifier().observe_path(str(root / "inside.txt") + "   ",
                                     allowed_roots=[root])

    assert result.state is VerificationState.PASSED, result.detail
    assert result.evidence["contained"] is True


def test_an_eight_dot_three_short_name_is_contained(tmp_path: Path):
    """A short name is an alias for a real file; containment must resolve it."""
    root = _root(tmp_path)
    target = root / "a_very_long_file_name_that_will_get_a_short_alias.txt"
    target.write_text("inside\n", encoding="utf-8")

    buffer = ctypes.create_unicode_buffer(1024)
    try:
        length = ctypes.windll.kernel32.GetShortPathNameW(str(target), buffer, 1024)
    except (AttributeError, OSError) as exc:  # pragma: no cover - non-Windows
        pytest.skip(f"GetShortPathNameW unavailable: {exc}")
    if not length:
        pytest.skip("8.3 name generation is disabled on this volume")
    short_form = buffer.value
    if not short_form or short_form == str(target):
        pytest.skip("the volume generated no distinct short name")

    result = Verifier().observe_path(Path(short_form), allowed_roots=[root])

    assert result.state is VerificationState.PASSED, (
        f"a short-name alias of an in-root file was not recognised: {result.detail}")
    assert result.evidence["contained"] is True


# ═══════════════════════════════════════════ paths that must not be claimed


def test_dot_dot_traversal_out_of_the_root_is_refused(tmp_path: Path):
    root = _root(tmp_path)
    outside = _outside(tmp_path)
    escape = root / ".." / outside.name / "secret.txt"

    result = Verifier().observe_path(escape, allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED, (
        f"a traversal out of the allowed root was not refused: {result.state.value}")
    assert result.evidence["contained"] is False
    assert path_is_within(escape, [root]) is False


def test_dot_dot_in_the_middle_is_refused(tmp_path: Path):
    root = _root(tmp_path)
    outside = _outside(tmp_path)
    (root / "sub").mkdir(exist_ok=True)
    nested = root / "sub" / ".." / ".." / outside.name / "secret.txt"

    result = Verifier().observe_path(nested, allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED, result.detail
    assert path_is_within(nested, [root]) is False


def test_a_unc_path_is_refused(tmp_path: Path):
    """``\\\\server\\share`` cannot be inside a local temp root."""
    root = _root(tmp_path)

    result = Verifier().observe_path(Path("\\\\some-server\\share\\secret.txt"),
                                     allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED, result.detail
    assert result.evidence["contained"] is False


def test_an_extended_length_prefix_path_is_refused_even_when_in_root(tmp_path: Path):
    """``\\\\?\\`` bypasses normal normalisation, so containment cannot be trusted.

    Fail-closed, which is the right direction, but it means a legitimate in-root
    path spelled with the prefix cannot be verified at all. Reported as a
    robustness note, not a safety hole.
    """
    root = _root(tmp_path)

    result = Verifier().observe_path("\\\\?\\" + str(root / "inside.txt"),
                                     allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED, result.detail
    assert result.evidence["contained"] is False


def test_a_reserved_device_name_is_never_reported_as_a_real_file(tmp_path: Path):
    """``CON``/``NUL``/``COM1`` are devices. They must never come back as files.

    ``NUL`` reports ``exists=True, observed_type='other'`` - it really is there,
    but it is a device, not the file the check expected, so the verdict is not
    PASSED. ``CON`` reports ``missing``. Both answers are safe; neither lets the
    agent claim it verified a file.
    """
    root = _root(tmp_path)

    for device in ("CON", "NUL", "COM1"):
        result = Verifier().observe_path(root / device, allowed_roots=[root])
        _assert_never_passed(result, f"reserved device {device}")
        assert result.evidence["observed_type"] != "file", (
            f"{device} was reported as an ordinary file, so a device read could be "
            f"passed off as verified content: {result.evidence}")


def test_a_missing_alternate_data_stream_is_never_reported_as_present(tmp_path: Path):
    root = _root(tmp_path)

    result = Verifier().observe_path(root / "inside.txt:hidden", allowed_roots=[root])

    _assert_never_passed(result, "absent alternate data stream")
    assert result.evidence["observed_type"] == "missing"


def test_an_existing_alternate_data_stream_can_satisfy_a_content_check(tmp_path: Path):
    """FINDING (informational): a hidden stream can verify file content.

    The stream lives on a file inside the allowed root, so this is not a
    containment escape - but the content the agent reports as verified can live
    somewhere the user never sees. Characterized, not fixed.
    """
    root = _root(tmp_path)
    stream = str(root / "inside.txt") + ":hidden"
    try:
        with open(stream, "w", encoding="utf-8", newline="") as handle:
            handle.write("stream-payload\n")
    except OSError as exc:  # pragma: no cover - filesystem refuses ADS
        pytest.skip(f"alternate data streams unavailable here: {exc}")

    result = Verifier().observe_file_content(stream, expected_content="stream-payload\n",
                                             allowed_roots=[root])

    assert result.state is VerificationState.PASSED, (
        f"the ADS behaviour changed; re-read the finding in "
        f"b4_09_path_findings.md. Now: {result.state.value} - {result.detail}")
    assert result.evidence["contained"] is True


def test_a_very_long_path_is_handled_without_crashing(tmp_path: Path):
    """A ~760-char in-root path must verify normally, not crash or be truncated.

    The containment risk with long paths is truncation: a checker that clips the
    string could compare a prefix against a root and wrongly call a foreign path
    contained. So the assertion is that the *full* name is what gets verified.
    """
    root = _root(tmp_path)
    component = "a" * 250
    very_long = root / component / component / component / (component + ".txt")

    try:
        very_long.parent.mkdir(parents=True, exist_ok=True)
        (very_long).write_text("x\n", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"this volume refuses paths this long (MAX_PATH): {exc}")

    assert len(str(very_long)) > 700, "the test path is not long enough to matter"

    result = Verifier().observe_path(very_long, allowed_roots=[root])

    assert result.state is VerificationState.PASSED, result.detail
    assert result.evidence["contained"] is True
    assert result.evidence["path"] == str(very_long), (
        "the verifier reported a different path than the one asked about, so the "
        "long name was truncated somewhere")


def test_a_long_foreign_path_is_refused_when_truncation_would_help_it(tmp_path: Path):
    """The counter-case: a long name whose *prefix* looks like an allowed root."""
    base = tmp_path.resolve()
    root = base / "allowed"
    root.mkdir()
    component = "b" * 250
    sneaky = root.parent / ("allowed" + component[:0] + "x") / component / "secret.txt"

    result = Verifier().observe_path(sneaky, allowed_roots=[root])

    _assert_never_passed(result, "long foreign path")
    assert result.evidence["contained"] is False


# ═════════════════════════════════════════════════════ the tools, not probes


def test_read_file_is_safe_in_root_and_sensitive_outside(tmp_path: Path):
    root = _root(tmp_path)
    spec = build_registry(_settings(root)).get("filesystem.read_file")

    assert spec.level_for({"path": str(root / "x.txt")}).name == "SAFE"
    assert spec.level_for({"path": "C:/Windows/System32/drivers/etc/hosts"}).name \
        == "SENSITIVE"


def test_write_file_is_normal_in_root_and_sensitive_outside(tmp_path: Path):
    root = _root(tmp_path)
    spec = build_registry(_settings(root)).get("filesystem.write_file")

    assert spec.level_for({"path": str(root / "x.txt")}).name == "NORMAL"
    assert spec.level_for({"path": "C:/Windows/notepad.exe"}).name == "SENSITIVE"


def test_delete_file_out_of_root_produces_an_unverified_verdict(tmp_path: Path):
    root = _root(tmp_path)
    outside = _outside(tmp_path)

    result = filesystem.delete_file(str(outside / "secret.txt"), _settings(root))

    assert result.verification is not None
    assert result.verification.state is VerificationState.UNVERIFIED, (
        f"an out-of-root delete produced {result.verification.state.value}")
    assert result.verification.kind == FILESYSTEM_ABSENCE_KIND


def test_observe_absence_of_an_out_of_root_path_is_unverified(tmp_path: Path):
    root = _root(tmp_path)
    outside = _outside(tmp_path)

    result = Verifier().observe_absence(outside / "secret.txt", allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED
    assert result.evidence["contained"] is False


def test_observe_file_content_of_an_out_of_root_path_is_unverified(tmp_path: Path):
    """The real file contains ``secret\\n``; that content must not come back."""
    root = _root(tmp_path)
    outside = _outside(tmp_path)

    result = Verifier().observe_file_content(outside / "secret.txt",
                                             expected_content="secret\n",
                                             allowed_roots=[root])

    assert result.state is VerificationState.UNVERIFIED
    assert result.evidence["contained"] is False
    assert "secret" not in result.detail, (
        f"the refused path's detail still carried the file name: {result.detail}")


# ═════════════════════════════════════════════════════════ symlinks, junctions


def _can_symlink() -> tuple[bool, str]:
    probe = Path(tempfile.mkdtemp(prefix="diablo-symlink-probe-"))
    (probe / "target.txt").write_text("x\n", encoding="utf-8")
    try:
        (probe / "link.txt").symlink_to(probe / "target.txt")
    except OSError as exc:
        return False, f"symlink creation unavailable: {exc}"
    return True, ""


def test_a_symlink_pointing_out_of_the_root_is_refused(tmp_path: Path):
    """The escape case containment exists for."""
    root = _root(tmp_path)
    outside = _outside(tmp_path)
    ok, why = _can_symlink()
    if not ok:
        pytest.skip(why)

    try:
        (root / "escape.txt").symlink_to(outside / "secret.txt")
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = Verifier().observe_path(root / "escape.txt", allowed_roots=[root])

    _assert_never_passed(result, "symlink escape")
    if result.state is VerificationState.UNVERIFIED:
        assert result.evidence["contained"] is False, (
            "a symlink out of the root was refused but still reported contained")


def test_a_symlink_inside_the_root_is_still_contained(tmp_path: Path):
    """Containment must not become so strict that a legitimate link fails."""
    root = _root(tmp_path)
    ok, why = _can_symlink()
    if not ok:
        pytest.skip(why)

    try:
        (root / "alias.txt").symlink_to(root / "inside.txt")
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = Verifier().observe_path(root / "alias.txt", allowed_roots=[root])

    assert result.state is VerificationState.PASSED, (
        f"a symlink inside the root was refused: {result.state.value} - "
        f"{result.detail}")


# ══════════════════════════════════════════════════ containment helper, direct


@pytest.mark.parametrize("spelling", [
    "inside.txt", "INSIDE.TXT", "InSiDe.TxT", "./inside.txt", "sub/../inside.txt",
])
def test_path_is_within_accepts_equivalent_spellings(tmp_path: Path, spelling: str):
    root = _root(tmp_path)
    (root / "sub").mkdir(exist_ok=True)

    assert path_is_within(root / spelling, [root]) is True, (
        f"{spelling!r} is an in-root spelling but was reported outside")


@pytest.mark.parametrize("spelling", [
    "../outside/secret.txt", "../../outside/secret.txt",
    "sub/../../outside/secret.txt",
])
def test_path_is_within_rejects_traversal(tmp_path: Path, spelling: str):
    root = _root(tmp_path)
    (root / "sub").mkdir(exist_ok=True)

    assert path_is_within(root / spelling, [root]) is False, (
        f"{spelling!r} escaped the allowed root")


def test_path_is_within_rejects_a_sibling_with_a_shared_prefix(tmp_path: Path):
    """``/allowed-evil`` must not count as inside ``/allowed``."""
    base = tmp_path.resolve()
    allowed = base / "allowed"
    allowed.mkdir()
    sibling = base / "allowed-evil" / "secret.txt"
    sibling.parent.mkdir()
    sibling.write_text("x\n", encoding="utf-8")

    assert path_is_within(sibling, [allowed]) is False, (
        "a sibling directory sharing a name prefix was treated as inside the root")


def test_path_is_within_with_no_roots_is_false(tmp_path: Path):
    assert path_is_within(_root(tmp_path) / "inside.txt", []) is False


@pytest.mark.xfail(
    reason="FINDING: path_is_within calls Path.resolve() unguarded, so a "
           "non-Path argument raises AttributeError instead of returning False. "
           "A caller that builds a path from untrusted data can turn a containment "
           "check into a crash. Fix: coerce to Path inside a try/except.",
    strict=True,
)
def test_a_non_path_argument_is_refused_rather_than_raising(tmp_path: Path):
    assert path_is_within(object(), [tmp_path]) is False