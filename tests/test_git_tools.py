import subprocess

import pytest

from config.permissions import ToolBlockedError
from config.settings import load_settings
from tools import build_registry
from tools.git import (
    git_branch_create, git_branch_switch, git_commit, git_diff, git_fetch, git_pull,
    git_push, git_reset, git_restore, git_stage, _git,
)


def _init(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
    return tmp_path


def _commit(tmp_path, name="a.txt", content="hello\n"):
    (tmp_path / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), capture_output=True)


def _settings_blocked(tmp_settings):
    tmp_settings.git_push_policy = "blocked"
    tmp_settings.git_restore_policy = "blocked"
    return tmp_settings


@pytest.fixture
def repo(tmp_path):
    root = _init(tmp_path)
    _commit(root)
    return root


def test_git_stage_and_commit_roundtrip(repo):
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    assert git_stage(str(repo), ["b.txt"]).success
    ok = git_commit(str(repo), "add b")
    assert ok.success
    code, out, _ = _git(str(repo), ["log", "--oneline"])
    assert "add b" in out.splitlines()[0]


def test_git_commit_requires_message(repo):
    result = git_commit(str(repo), "   ")
    assert not result.success


def test_git_push_blocked_by_default(repo, tmp_settings):
    tmp_settings.git_push_policy = "blocked"
    with pytest.raises(ToolBlockedError):
        git_push(str(repo), settings=tmp_settings)


def test_git_restore_blocked_by_default(repo, tmp_settings):
    tmp_settings.git_restore_policy = "blocked"
    with pytest.raises(ToolBlockedError):
        git_restore(str(repo), all=True, settings=tmp_settings)


def test_git_reset_hard_blocked_by_default(repo, tmp_settings):
    tmp_settings.git_restore_policy = "blocked"
    with pytest.raises(ToolBlockedError):
        git_reset(str(repo), mode="hard", settings=tmp_settings)


def test_git_restore_backup_first(repo, tmp_settings):
    tmp_settings.git_restore_policy = "confirm"
    backups = tmp_settings.base_dir / "data" / "backups"
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    result = git_restore(str(repo), all=True, settings=tmp_settings)
    assert result.success
    assert "backup" in result.output
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hello\n"
    patches = list(backups.glob("git-before-*.patch")) if backups.is_dir() else []
    assert patches


def test_git_restore_specific_paths(repo, tmp_settings):
    tmp_settings.git_restore_policy = "confirm"
    (repo / "a.txt").write_text("bad\n", encoding="utf-8")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    result = git_restore(str(repo), paths=["a.txt"], settings=tmp_settings)
    assert result.success
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_git_reset_soft_allowed(repo, tmp_settings):
    tmp_settings.git_restore_policy = "blocked"
    _commit(repo, name="b.txt", content="two\n")
    result = git_reset(str(repo), mode="soft", target="HEAD~1", settings=tmp_settings)
    assert result.success


def test_git_fetch_no_remote_fails_gracefully(repo):
    result = git_fetch(str(repo))
    assert result.success is False


def test_git_pull_no_remote_fails_gracefully(repo):
    result = git_pull(str(repo))
    assert result.success is False


def test_git_branch_policy_attr_defaults_blocked(tmp_settings):
    assert tmp_settings.git_branch_policy == "blocked"


def test_git_branch_policy_loaded_from_toml():
    from config.settings import _str
    toml = {"git": {"branch": "confirm"}}
    assert _str(toml, ["git", "branch"], "blocked") == "confirm"


def test_git_branch_policy_attr_defaults_blocked(tmp_settings):
    assert tmp_settings.git_branch_policy == "blocked"


def test_git_branch_policy_to_setting_object(tmp_settings):
    assert tmp_settings.git_branch_policy == "blocked"


def test_git_branch_create_gated_blocked(repo, tmp_settings):
    with pytest.raises(ToolBlockedError):
        git_branch_create(str(repo), "feat/x", settings=tmp_settings)


def test_git_branch_switch_gated_blocked(repo, tmp_settings):
    with pytest.raises(ToolBlockedError):
        git_branch_switch(str(repo), "feat/x", settings=tmp_settings)


def test_git_branch_create_no_attr_error(repo, tmp_settings):
    with pytest.raises(ToolBlockedError):
        git_branch_create(str(repo), "feat/x", settings=tmp_settings)


def test_git_branch_switch_no_attr_error(repo, tmp_settings):
    with pytest.raises(ToolBlockedError):
        git_branch_switch(str(repo), "feat/x", settings=tmp_settings)


def test_destructive_git_tools_not_safe(repo, tmp_settings):
    _settings_blocked(tmp_settings)
    registry = build_registry(tmp_settings)
    for name in ("git.push", "git.restore", "git.reset", "git.rebase"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.level.value >= 1
    assert registry.get("git.commit").level.value == 1
    assert registry.get("git.diff").level.value == 0