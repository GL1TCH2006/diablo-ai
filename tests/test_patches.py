import pytest

from coding.patch import apply_plan, parse_patch
from tools.patches import patch_file
from tools import build_registry


def _file(tmp_path, name="app.py", content="def foo():\n    return 1\n"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_patch_insert_after(tmp_path):
    f = _file(tmp_path, content="a = 1\nb = 2\n")
    patch = "=== app.py ===\ninsert_after a = 1\n+c = 3\n"
    plan = parse_patch(patch)
    assert plan.valid
    results = apply_plan(plan, tmp_path, dry_run=False)
    assert all(ok for _, ok, _ in results)
    assert f.read_text(encoding="utf-8").splitlines() == ["a = 1", "c = 3", "b = 2"]


def test_patch_replace_validates_removed_lines(tmp_path):
    f = _file(tmp_path, content="x = 0\ny = 1\n")
    patch = "=== app.py ===\nreplace y = 1\n-y = 1\n+y = 2\n"
    plan = parse_patch(patch)
    results = apply_plan(plan, tmp_path, dry_run=False)
    assert results[0][1]
    assert f.read_text(encoding="utf-8").splitlines() == ["x = 0", "y = 2"]


def test_patch_replace_mismatch_rejected(tmp_path):
    _file(tmp_path, content="x = 0\ny = 1\n")
    patch = "=== app.py ===\nreplace y = 1\n-y = 99\n+y = 2\n"
    plan = parse_patch(patch)
    ok, message = apply_plan(plan, tmp_path, dry_run=False)[0][1:3]
    assert not ok
    assert "do not match" in message


def test_patch_ambiguous_anchor_rejected(tmp_path):
    _file(tmp_path, content="z = 1\nz = 1\n")
    patch = "=== app.py ===\nreplace z = 1\n+z = 3\n"
    plan = parse_patch(patch)
    ok, message = apply_plan(plan, tmp_path, dry_run=False)[0][1:3]
    assert not ok
    assert "ambiguous" in message


def test_patch_missing_anchor_rejected(tmp_path):
    _file(tmp_path)
    patch = "=== app.py ===\ninsert_after def nowhere():\n+a = 1\n"
    plan = parse_patch(patch)
    ok, message = apply_plan(plan, tmp_path, dry_run=False)[0][1:3]
    assert not ok
    assert "anchor not found" in message


def test_patch_delete(tmp_path):
    f = _file(tmp_path, content="keep\nremove-me\ntail\n")
    patch = "=== app.py ===\ndelete remove-me\n"
    plan = parse_patch(patch)
    results = apply_plan(plan, tmp_path, dry_run=False)
    assert results[0][1]
    assert f.read_text(encoding="utf-8").splitlines() == ["keep", "tail"]


def test_patch_dry_run_no_write(tmp_path):
    f = _file(tmp_path, content="a = 1\nb = 2\n")
    patch = "=== app.py ===\ninsert_after a = 1\n+c = 3\n"
    results = apply_plan(parse_patch(patch), tmp_path, dry_run=True)
    assert results[0][1]
    assert "dry run" in results[0][2]
    assert f.read_text(encoding="utf-8").splitlines() == ["a = 1", "b = 2"]


def test_patch_multiple_sections_atomic_failure(tmp_path):
    f1 = _file(tmp_path, name="one.py", content="a\nb\n")
    f2 = _file(tmp_path, name="two.py", content="a\nb\n")
    patch = (
        "=== one.py ===\nreplace a\n+a=2\n"
        "=== two.py ===\nreplace nope\n+nope\n"
    )
    results = apply_plan(parse_patch(patch), tmp_path, dry_run=False)
    ok_for_one, ok_for_two = results[0][1], results[1][1]
    assert ok_for_two is False
    assert f1.read_text(encoding="utf-8").splitlines() == ["a=2", "b"]  # valid ones applied
    assert f2.read_text(encoding="utf-8").splitlines() == ["a", "b"]


def test_patch_creates_backup(tmp_path):
    f = _file(tmp_path, content="old\n")
    patch = "=== app.py ===\nreplace old\n+new\n"
    results = apply_plan(parse_patch(patch), tmp_path, dry_run=False, backup_dir=tmp_path / "bk")
    assert results[0][1]
    backups = list((tmp_path / "bk").glob("*.bak"))
    assert backups
    assert "old" in backups[0].read_text(encoding="utf-8")


def test_patch_file_tool_end_to_end(tmp_settings):
    from pathlib import Path
    proj = Path(tmp_settings.base_dir) / "_patch_demo"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "lib.py"
    f.write_text("def greet():\n    return None\n", encoding="utf-8")
    patch = "=== lib.py ===\nreplace     return None\n-    return None\n+    return 'hi'\n"
    result = patch_file(patch, dry_run=False, workspace=str(proj), settings=tmp_settings)
    assert result.success
    assert "lib.py" in result.output
    assert "return 'hi'" in f.read_text(encoding="utf-8")


def test_patch_file_tool_rejects_before_write(tmp_settings):
    from pathlib import Path
    proj = Path(tmp_settings.base_dir) / "_patch_demo2"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "lib.py"
    f.write_text("x = 1\n", encoding="utf-8")
    patch = "=== lib.py ===\nreplace missing\n+y=2\n"
    result = patch_file(patch, dry_run=False, workspace=str(proj), settings=tmp_settings)
    assert not result.success
    assert "anchor not found" in result.error


def test_code_patch_tool_registered(tmp_settings):
    registry = build_registry(tmp_settings)
    assert "code.patch_file" in registry.names()
    spec = registry.get("code.patch_file")
    assert spec.level.value == 1