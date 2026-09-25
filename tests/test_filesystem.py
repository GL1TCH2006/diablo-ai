import pytest

from tools.filesystem import delete_file, list_dir, read_file, write_file


def test_write_read_list_delete_roundtrip(tmp_settings):
    target = tmp_settings.scratch_dir / "sample" / "a.txt"
    result = write_file(str(target), "hello", tmp_settings)
    assert result.success

    read = read_file(str(target))
    assert read.success and read.output == "hello"

    listed = list_dir(str(target.parent))
    assert listed.success and "a.txt" in listed.output

    deleted = delete_file(str(target), tmp_settings)
    assert deleted.success
    assert not target.exists()


def test_backup_created_on_overwrite(tmp_settings):
    target = tmp_settings.scratch_dir / "b.txt"
    write_file(str(target), "v1", tmp_settings)
    result2 = write_file(str(target), "v2", tmp_settings)
    assert result2.success and "backup" in result2.output
    backups = list(tmp_settings.backups_dir.glob("b.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "v1"


def test_read_missing_file_error(tmp_settings):
    result = read_file(str(tmp_settings.scratch_dir / "missing.txt"))
    assert not result.success
    assert "not found" in result.error.lower()


def test_delete_directory_refused(tmp_settings):
    target = tmp_settings.scratch_dir / "adir"
    target.mkdir(parents=True, exist_ok=True)
    result = delete_file(str(target), tmp_settings)
    assert not result.success
    assert "directory" in result.error.lower()


def test_write_creates_parent_dirs(tmp_settings):
    deep = tmp_settings.scratch_dir / "x" / "y" / "z" / "deep.txt"
    result = write_file(str(deep), "deep", tmp_settings)
    assert result.success and deep.exists()