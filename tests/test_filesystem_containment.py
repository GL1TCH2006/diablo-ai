"""Tests for filesystem tool containment enforcement."""

import tempfile
from pathlib import Path

import pytest

from config.settings import Settings
from tools.filesystem import read_file, write_file, list_dir, delete_file


class TestFilesystemContainment:
    """Test that filesystem tools enforce allowed_roots containment."""

    def setup_method(self):
        # Create a temporary directory structure for testing
        self.temp_dir = Path(tempfile.mkdtemp(prefix="diablo_test_"))
        self.allowed_root = self.temp_dir / "allowed"
        self.allowed_root.mkdir()
        self.outside_root = self.temp_dir / "outside"
        self.outside_root.mkdir()

        # Create test files
        (self.allowed_root / "test.txt").write_text("allowed content")
        (self.allowed_root / "subdir").mkdir()
        (self.allowed_root / "subdir" / "nested.txt").write_text("nested content")
        (self.outside_root / "secret.txt").write_text("outside content")

        # Settings with only allowed_root permitted
        self.settings = Settings(
            allowed_roots=[self.allowed_root],
            base_dir=self.temp_dir,
            data_dir=self.temp_dir / "data",
            memory_dir=self.temp_dir / "data" / "memory",
            logs_dir=self.temp_dir / "data" / "logs",
            backups_dir=self.temp_dir / "data" / "backups",
            scratch_dir=self.temp_dir / "workspace",
            sessions_dir=self.temp_dir / "data" / "sessions",
        )

    def teardown_method(self):
        # Cleanup
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # --- read_file tests ---

    def test_read_file_inside_allowed_root(self):
        result = read_file(str(self.allowed_root / "test.txt"), self.settings)
        assert result.success is True
        assert "allowed content" in result.output

    def test_read_file_outside_allowed_root_rejected(self):
        result = read_file(str(self.outside_root / "secret.txt"), self.settings)
        assert result.success is False
        assert "outside the allowed roots" in result.error.lower() or "containment" in result.error.lower()

    def test_read_file_nonexistent_rejected(self):
        result = read_file(str(self.allowed_root / "nonexistent.txt"), self.settings)
        assert result.success is False
        assert "not found" in result.error.lower()

    # --- write_file tests ---

    def test_write_file_inside_allowed_root(self):
        new_file = self.allowed_root / "new_file.txt"
        result = write_file(str(new_file), "new content", self.settings)
        assert result.success is True
        assert new_file.read_text() == "new content"

    def test_write_file_outside_allowed_root_rejected(self):
        new_file = self.outside_root / "evil.txt"
        result = write_file(str(new_file), "evil content", self.settings)
        assert result.success is False
        assert "outside the allowed roots" in result.error.lower() or "containment" in result.error.lower()
        # File should not have been created
        assert not new_file.exists()

    def test_write_file_creates_parent_dirs_inside_root(self):
        new_file = self.allowed_root / "new" / "deep" / "file.txt"
        result = write_file(str(new_file), "deep content", self.settings)
        assert result.success is True
        assert new_file.read_text() == "deep content"

    def test_write_file_no_settings_rejected(self):
        """write_file without settings should fail closed."""
        result = write_file(str(self.allowed_root / "test.txt"), "content", None)
        assert result.success is False
        assert "no settings" in result.error.lower() or "containment" in result.error.lower()

    # --- list_dir tests ---

    def test_list_dir_inside_allowed_root(self):
        result = list_dir(str(self.allowed_root), self.settings)
        assert result.success is True
        assert "test.txt" in result.output
        assert "subdir/" in result.output

    def test_list_dir_outside_allowed_root_rejected(self):
        result = list_dir(str(self.outside_root), self.settings)
        assert result.success is False
        assert "outside the allowed roots" in result.error.lower() or "containment" in result.error.lower()

    # --- delete_file tests (already had containment) ---

    def test_delete_file_inside_allowed_root(self):
        to_delete = self.allowed_root / "test.txt"
        assert to_delete.exists()
        result = delete_file(str(to_delete), self.settings)
        assert result.success is True
        assert not to_delete.exists()

    def test_delete_file_outside_allowed_root_rejected(self):
        result = delete_file(str(self.outside_root / "secret.txt"), self.settings)
        assert result.success is False
        assert "outside the allowed roots" in result.error.lower() or "containment" in result.error.lower()

    # --- Path traversal tests ---

    def test_write_file_parent_traversal_rejected(self):
        """Paths with .. that resolve outside root should be rejected."""
        # This creates a file that would resolve outside allowed_root
        # Note: Path resolution handles .. before containment check
        traversal_path = self.allowed_root / "subdir" / ".." / ".." / "outside" / "evil.txt"
        result = write_file(str(traversal_path), "evil", self.settings)
        # Should be rejected because resolved path is outside allowed_root
        assert result.success is False


class TestFilesystemContainmentNoSettings:
    """Test behavior when settings is None or has no allowed_roots."""

    def test_read_file_no_settings_fails_closed(self):
        result = read_file("any_path.txt", None)
        assert result.success is False

    def test_write_file_no_settings_fails_closed(self):
        result = write_file("any_path.txt", "content", None)
        assert result.success is False

    def test_list_dir_no_settings_fails_closed(self):
        result = list_dir("any_path", None)
        assert result.success is False

    def test_delete_file_no_settings_fails_closed(self):
        result = delete_file("any_path.txt", None)
        assert result.success is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])