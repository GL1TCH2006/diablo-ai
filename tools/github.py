from __future__ import annotations

import subprocess

from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("gh")


def _gh(args: list[str], root: str = "", timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh"] + args, cwd=root or None, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)


def gh_status() -> ToolResult:
    code, out, err = _gh(["auth", "status"])
    return ToolResult(code == 0, output=out or err, error=err if code else "")


def gh_issue_list(root: str, state: str = "open", limit: int = 20) -> ToolResult:
    code, out, err = _gh(["issue", "list", "--state", state, "--limit", str(limit)], root=root)
    return ToolResult(code == 0, output=out, error=err if code else "")


def gh_pr_list(root: str, state: str = "open", limit: int = 20) -> ToolResult:
    code, out, err = _gh(["pr", "list", "--state", state, "--limit", str(limit)], root=root)
    return ToolResult(code == 0, output=out, error=err if code else "")


def gh_repo_view(root: str) -> ToolResult:
    code, out, err = _gh(["repo", "view"], root=root)
    return ToolResult(code == 0, output=out, error=err if code else "")


def gh_issue_create(root: str, title: str, body: str = "") -> ToolResult:
    args = ["issue", "create", "--title", title]
    if body:
        args += ["--body", body]
    code, out, err = _gh(args, root=root)
    return ToolResult(code == 0, output=out or f"issue created: {title}", error=err if code else "")


def gh_pr_create(root: str, title: str, head: str = "", base: str = "", body: str = "") -> ToolResult:
    args = ["pr", "create", "--title", title]
    if head:
        args += ["--head", head]
    if base:
        args += ["--base", base]
    if not body:
        args += ["--fill"]
    else:
        args += ["--body", body]
    code, out, err = _gh(args, root=root)
    return ToolResult(code == 0, output=out or f"PR created: {title}", error=err if code else "")


def gh_release_create(root: str, tag: str, title: str = "") -> ToolResult:
    args = ["release", "create", tag]
    if title:
        args += ["--title", title]
    code, out, err = _gh(args, root=root)
    return ToolResult(code == 0, output=out or f"release created: {tag}", error=err if code else "")