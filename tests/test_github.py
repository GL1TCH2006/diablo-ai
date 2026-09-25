import pytest

from tools import build_registry
from tools.github import gh_issue_list, gh_status


def test_gh_status_returns_something():
    result = gh_status()
    assert result.success in (True, False)  # environment-dependent, never crashes
    assert isinstance(result.output, str)


def test_gh_issue_list_no_repo_fails_gracefully(tmp_path):
    result = gh_issue_list(str(tmp_path))
    assert isinstance(result.error, str)


def test_gh_tools_registered(tmp_settings):
    registry = build_registry(tmp_settings)
    names = set(registry.names())
    assert {"gh.status", "gh.issue_list", "gh.pr_list", "gh.repo_view", "gh.issue_create", "gh.pr_create", "gh.release_create"} <= names
    assert registry.get("gh.status").level.value == 0
    assert registry.get("gh.pr_create").level.value == 2
    assert registry.get("gh.issue_list").level.value == 0