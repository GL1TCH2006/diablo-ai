"""Tests for is_auto_allowed shell command authorization."""

import pytest
from config.permissions import is_auto_allowed


class TestIsAutoAllowed:
    """Test the auto-allow logic for shell commands."""

    def test_exact_match_allows(self):
        patterns = ["git status", "dir", "python --version"]
        assert is_auto_allowed("git status", patterns) is True
        assert is_auto_allowed("dir", patterns) is True
        assert is_auto_allowed("python --version", patterns) is True

    def test_prefix_match_allows(self):
        patterns = ["git status", "git diff"]
        # Prefix match with space after pattern
        assert is_auto_allowed("git status --porcelain", patterns) is True
        assert is_auto_allowed("git diff --stat", patterns) is True

    def test_separator_semicolon_rejected(self):
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status; rm -rf /", patterns) is False

    def test_separator_pipe_rejected(self):
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status | grep foo", patterns) is False

    def test_separator_ampersand_rejected(self):
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status & echo done", patterns) is False

    def test_separator_newline_rejected(self):
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status\ndir", patterns) is False

    def test_separator_carriage_return_rejected(self):
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status\rdir", patterns) is False

    def test_separator_double_ampersand_rejected(self):
        """PowerShell && is a command separator since PS 7+."""
        patterns = ["git status", "dir"]
        assert is_auto_allowed("git status && rm -rf /", patterns) is False
        assert is_auto_allowed("dir && shutdown /s", patterns) is False

    def test_whitespace_normalization(self):
        patterns = ["git status"]
        assert is_auto_allowed("  git   status  ", patterns) is True
        # Internal newlines/tabs are separators and should be rejected
        assert is_auto_allowed("\tgit\nstatus\t", patterns) is False

    def test_case_insensitive(self):
        patterns = ["Git Status"]
        assert is_auto_allowed("git status", patterns) is True
        assert is_auto_allowed("GIT STATUS", patterns) is True

    def test_no_match_requires_confirmation(self):
        patterns = ["git status"]
        assert is_auto_allowed("git commit -m 'foo'", patterns) is False
        assert is_auto_allowed("rm -rf /", patterns) is False
        assert is_auto_allowed("", patterns) is False

    def test_prefix_boundary_not_overmatched(self):
        """Ensure 'git status' doesn't match 'git statusextra'."""
        patterns = ["git status"]
        assert is_auto_allowed("git statusextra", patterns) is False
        # But 'git status ' with trailing space should match
        assert is_auto_allowed("git status --help", patterns) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])