from __future__ import annotations

from config.permissions import PermissionLevel, path_is_within
from config.settings import Settings
from core.knowledge_graph import NodeType, RelationType
from core.knowledge_tools import QUERY_TYPES
from core.logging_setup import get_logger

from . import applications, coding as coding_tool, filesystem, git as git_tool, github as gh_tool, memory as memory_tool, models as models_tool, patches as patches_tool, python as python_tool, rules as rules_tool, system, tasks as tasks_tool, terminal, data_tool, chart_tool
from .registry import ToolRegistry, ToolSpec

log = get_logger("tools")


class runtime_context:
    router = None
    prefs = None


def build_registry(
    settings: Settings,
    capability_registry=None,
    capability_events=None,
    event_bus=None,
) -> ToolRegistry:
    registry = ToolRegistry()
    if capability_events is None:
        capability_events = event_bus

    registry.register(
        ToolSpec(
            name="filesystem.read_file",
            description="Read a text file and return its contents. Restricted to allowed project roots unless confirmed.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: filesystem.read_file(kw["path"], settings),
            describe=lambda args: f"Read file: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="filesystem.list_dir",
            description="List the entries of a directory.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: filesystem.list_dir(kw["path"], settings),
            describe=lambda args: f"List directory: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="filesystem.write_file",
            description="Create or overwrite a text file with the given contents. Overwrites create a backup first.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            required=["path", "content"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: filesystem.write_file(kw["path"], kw.get("content", ""), settings),
            describe=lambda args: f"Write file: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="filesystem.delete_file",
            description="Permanently delete a single file. Always requires confirmation; directories are refused.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SENSITIVE,
            fn=lambda **kw: filesystem.delete_file(kw["path"], settings),
            describe=lambda args: f"Delete file: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="terminal.execute",
            description="Run a PowerShell command and return its output. Commands matching the destructive blocklist are never allowed.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}, "working_dir": {"type": "string"}, "timeout": {"type": "integer"}},
                "required": ["command"],
            },
            required=["command"],
            level=PermissionLevel.SENSITIVE,
            fn=lambda **kw: terminal.run_command(kw["command"], kw.get("working_dir"), int(kw.get("timeout", 60) or 60), settings),
            describe=lambda args: f"Run shell command: {args.get('command')}",
        )
    )
    registry.register(
        ToolSpec(
            name="python.run_script",
            description=(
                "Run a Python script and return stdout/stderr. NOT a sandbox - a script has Diablo's "
                "privileges, so execution is gated by permissions and working-directory restrictions. "
                "Cancellation is supported: when a cancellation token is present (via _tool_context), "
                "the script process will be terminated with a bounded kill fallback."
            ),
            parameters={
                "type": "object",
                "properties": {"script": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}},
                "required": ["script"],
            },
            required=["script"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("script"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: python_tool.run_script(
                kw["script"], kw.get("args"), int(kw.get("timeout", 120) or 120), _tool_context=kw.get("_tool_context")
            ),
            describe=lambda args: f"Run Python script: {args.get('script')}",
            wants_context=True,
        )
    )
    registry.register(
        ToolSpec(
            name="applications.open",
            # M2.16.UI.1. Rewritten deliberately. The old text was "Open a desktop
            # application. Whitelisted apps launch automatically; other installed
            # apps launch after confirmation." - which reads as though this tool
            # can also *operate* an application, and gave the model no reason to
            # reach for desktop.* when a request needed interaction. This tool can
            # only START a program; it cannot click, type, read or verify. The
            # description now says so, and names the alternative.
            description=(
                "START (launch) a desktop application. This tool only launches a "
                "program - it cannot click, type, press keys, read a window or "
                "check a result. Use it when the whole request is just 'start this "
                "program'. If you need to interact with the application (click a "
                "button, press a key, type text, read or verify a control), use "
                "desktop.open to launch and anchor to its window, then "
                "desktop.find and desktop.interact."
            ),
            parameters={"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]},
            required=["app"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if _in_whitelist(args.get("app"), settings)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: applications.open_app(kw["app"], settings),
            describe=lambda args: f"Open application: {args.get('app')}",
            capabilities=("applications", "desktop"),
        )
    )
    registry.register(
        ToolSpec(
            name="system.info",
            description="Read system information: CPU usage, memory usage, and OS version.",
            parameters={"type": "object", "properties": {"components": {"type": "array", "items": {"type": "string"}}}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: system.system_info(kw.get("components")),
            describe=lambda args: "Read system information",
        )
    )
    registry.register(
        ToolSpec(
            name="task.info",
            description="Read one persisted task with outcome, status, Git summary, snapshots, change steps, and its durable execution plan. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Canonical task ID"},
                    "include_plan_args": {
                        "type": "boolean",
                        "description": "Include raw plan step arguments. Withheld by default because plans are model-authored and can embed sensitive payloads.",
                    },
                },
                "required": ["task_id"],
            },
            required=["task_id"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: tasks_tool.task_info(
                kw["task_id"], settings, bool(kw.get("include_plan_args", False))
            ),
            describe=lambda args: f"Inspect task: {args.get('task_id')}",
        )
    )
    registry.register(
        ToolSpec(
            name="task.list",
            description="List persisted tasks newest first, optionally filtered by exact project and task status. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Exact project name"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "active", "completed", "failed", "cancelled"],
                    },
                    "limit": {"type": "integer", "minimum": 0, "description": "Maximum recent tasks; default 20"},
                },
                "required": [],
            },
            level=PermissionLevel.SAFE,
            fn=lambda **kw: tasks_tool.task_list(
                project=kw.get("project", ""),
                status=kw.get("status", ""),
                limit=kw.get("limit", 20),
                settings=settings,
            ),
            describe=lambda args: f"List tasks (project={args.get('project', '')}, status={args.get('status', '')})",
        )
    )
    registry.register(
        ToolSpec(
            name="project.verify",
            description=(
                "Run this project's own test suite and report whether it passes. Use this to "
                "verify coding work: it detects the project's test command, runs it, and returns "
                "an evidence-backed verdict (tests run, tests failed, exit code). Call it after "
                "creating or changing code instead of claiming the work works."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Project root directory"}},
                "required": ["path"],
            },
            required=["path"],
            # Running a project's suite executes its code, so this is gated like
            # python.run_script: NORMAL inside an allowed root, SENSITIVE outside.
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: coding_tool.project_verify(kw["path"], settings),
            describe=lambda args: f"Run project tests: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="project.info",
            description="Analyse a project and return its profile: language, framework, entry points, dependencies, test/build/run commands, git branch.",
            parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Project root directory"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: coding_tool.project_info(kw["path"], settings),
            describe=lambda args: f"Analyse project: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="project.files",
            description="List files in a project, optionally filtered by filename substring or top-level folder.",
            parameters={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Project root directory"},
                    "pattern": {"type": "string", "description": "Optional filename substring filter"},
                    "folder": {"type": "string", "description": "Optional subfolder filter"},
                },
                "required": ["root"],
            },
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: coding_tool.project_files(kw["root"], kw.get("pattern", ""), kw.get("folder", ""), settings),
            describe=lambda args: f"List project files: {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="project.grep",
            description="Search file contents in a project with a regular expression. Returns file:line matches.",
            parameters={
                "type": "object",
                "properties": {"root": {"type": "string", "description": "Project root directory"}, "pattern": {"type": "string", "description": "Regular expression"}},
                "required": ["root", "pattern"],
            },
            required=["root", "pattern"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: coding_tool.project_grep(kw["root"], kw["pattern"], settings),
            describe=lambda args: f"Grep project {args.get('root')} for {args.get('pattern')}",
        )
    )

    registry.register(
        ToolSpec(
            name="git.status",
            description="Show working-tree status and current branch. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_status(kw["root"]),
            describe=lambda args: f"Git status in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.diff",
            description="Show uncommitted changes (working tree vs HEAD). Read-only.",
            parameters={
                "type": "object",
                "properties": {"root": {"type": "string"}, "stat": {"type": "boolean"}},
                "required": ["root"],
            },
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_diff(kw["root"], include_staged=False, stat=bool(kw.get("stat", False))),
            describe=lambda args: f"Git diff in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.log",
            description="Show recent commit history. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_log(kw["root"]),
            describe=lambda args: f"Git log in {args.get('root')}",
        )
    )

    registry.register(
        ToolSpec(
            name="code.patch_file",
            description=(
                "Apply a targeted, anchored patch to source files. Format per section:\n"
                "=== <path> ===\n"
                "replace <exact existing line to anchor on>\n"
                "-<line you expect, optional for validation>\n"
                "+<new line(s)>\n"
                "Actions: replace, insert_after <anchor>, insert_before <anchor>, delete <anchor>.\n"
                "Anchors must match exactly one line. All changes for a file apply atomically; "
                "a backup is created before writing. Use dry_run=true to preview."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Patch text in Diablo anchored syntax"},
                    "dry_run": {"type": "boolean", "description": "Validate without writing (default false)"},
                    "workspace": {"type": "string", "description": "Root used to resolve relative paths"},
                },
                "required": ["patch"],
            },
            required=["patch"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("workspace") or "", settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: patches_tool.patch_file(kw["patch"], bool(kw.get("dry_run", False)), kw.get("workspace", ""), settings),
            describe=lambda args: f"Patch code files ({'dry run' if args.get('dry_run') else 'apply'})",
        )
    )

    registry.register(
        ToolSpec(
            name="git.add",
            description="Stage files for the next commit (git add). Recoverable.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "all": {"type": "boolean"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_stage(kw["root"], kw.get("paths"), bool(kw.get("all", False))),
            describe=lambda args: f"git add in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.commit",
            description="Create a local commit with the given message (git commit -m).",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "message": {"type": "string"}}, "required": ["root", "message"]},
            required=["root", "message"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_commit(kw["root"], kw["message"]),
            describe=lambda args: f"git commit in {args.get('root')}: {args.get('message')[:80]}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.push",
            description="Push commits to a remote. DISABLED by default (config [git] push). Enabling requires confirmation each time.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "remote": {"type": "string"}, "branch": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_push_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_push(kw["root"], kw.get("remote", "origin"), kw.get("branch", ""), settings),
            describe=lambda args: f"git push in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.restore",
            description="Discard uncommitted changes in the working tree. DISABLED by default (config [git] restore). A backup patch is created first.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "all": {"type": "boolean"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_restore_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_restore(kw["root"], kw.get("paths"), bool(kw.get("all", False)), settings),
            describe=lambda args: f"git restore in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.reset",
            description="Reset repository state (soft/mixed/hard). hard mode DISABLED by default and creates a backup patch first.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "mode": {"type": "string"}, "target": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_restore_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_reset(kw["root"], kw.get("mode", "mixed"), kw.get("target", "HEAD"), settings),
            describe=lambda args: f"git reset --{args.get('mode', 'mixed')} in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.pull",
            description="Fetch and merge changes from a remote branch.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "remote": {"type": "string"}, "branch": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_pull(kw["root"], kw.get("remote", "origin"), kw.get("branch", "")),
            describe=lambda args: f"git pull in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.rebase",
            description="Rebase the current branch onto another commit/branch. DISABLED by default; backup-first.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "onto": {"type": "string"}}, "required": ["root", "onto"]},
            required=["root", "onto"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_restore_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_rebase(kw["root"], kw["onto"], settings),
            describe=lambda args: f"git rebase onto {args.get('onto')} in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.fetch",
            description="Download objects from a remote without merging. Read-only for the working tree.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "remote": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_fetch(kw["root"], kw.get("remote", "origin")),
            describe=lambda args: f"git fetch in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.branch",
            description="Show the current branch name. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_branch(kw["root"]),
            describe=lambda args: f"git current branch in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.branch.list",
            description="List all branches. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_branch_list(kw["root"]),
            describe=lambda args: f"git list branches in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.branch.create",
            description="Create a new branch from the current HEAD and switch to it. Gated by policy (config [git] branch_policy).",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "name": {"type": "string"}}, "required": ["root", "name"]},
            required=["root", "name"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_branch_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_branch_create(kw["root"], kw["name"], settings),
            describe=lambda args: f"git branch create {args.get('name')} in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.branch.switch",
            description="Switch to an existing branch. Gated by policy (config [git] branch_policy).",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "name": {"type": "string"}}, "required": ["root", "name"]},
            required=["root", "name"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_branch_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_branch_switch(kw["root"], kw["name"], settings),
            describe=lambda args: f"git switch {args.get('name')} in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.snapshot",
            description="Create a workspace snapshot (git commit, branch, status, diff) for the current task. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "task_id": {"type": "string"},
                    "label": {"type": "string"},
                    "creation_reason": {"type": "string"},
                },
                "required": ["root", "task_id", "label"],
            },
            required=["root", "task_id", "label"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("root"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: git_tool.git_snapshot(
                kw["root"], kw["task_id"], kw["label"], settings, kw.get("creation_reason", "manual")
            ),
            describe=lambda args: f"git snapshot {args.get('label')} for task {args.get('task_id')} in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.snapshot.list",
            description="List all snapshots for a task. Read-only.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            required=["task_id"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: git_tool.git_snapshot_list(kw["task_id"], settings),
            describe=lambda args: f"list git snapshots for task {args.get('task_id')}",
        )
    )
    registry.register(
        ToolSpec(
            name="git.snapshot.restore",
            description="Restore a workspace snapshot. DISABLED by default (config [git] restore). Requires confirmation.",
            parameters={
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "task_id": {"type": "string"},
                    "snapshot_id": {"type": "string"},
                },
                "required": ["root", "task_id", "snapshot_id"],
            },
            required=["root", "task_id", "snapshot_id"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: _git_policy_level(
                settings, settings.git_restore_policy, args.get("root")
            ),
            fn=lambda **kw: git_tool.git_snapshot_restore(
                kw["root"],
                kw["task_id"],
                kw["snapshot_id"],
                settings,
                confirmer=(lambda _prompt: True) if settings.git_restore_policy == "confirm" else None,
            ),
            describe=lambda args: f"git snapshot restore {args.get('snapshot_id')} for task {args.get('task_id')} in {args.get('root')}",
        )
    )

    for spec in _memory_tools(settings):
        registry.register(spec)
    for spec in _rules_tools(settings):
        registry.register(spec)
    for spec in _knowledge_tools(settings):
        registry.register(spec)
    registry.register(
        ToolSpec(
            name="models.status",
            description="Show current model router state: mode, fast/strong model identity, installed Ollama models. Read-only.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: models_tool.models_status(settings),
            describe=lambda args: "Show model router status",
        )
    )
    registry.register(
        ToolSpec(
            name="models.set_mode",
            description=(
                "Change the model routing mode: auto (adaptive), force8b, force14b, project "
                "(uses project preference). Optionally set the fast/strong model names. "
                "Persistent across sessions."
            ),
            parameters={
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": list(models_tool._MODES)}, "fast": {"type": "string"}, "strong": {"type": "string"}},
                "required": ["mode"],
            },
            required=["mode"],
            level=PermissionLevel.NORMAL,
            fn=lambda **kw: models_tool.models_set_mode(kw["mode"], kw.get("fast", ""), kw.get("strong", ""), settings),
            describe=lambda args: f"Set model mode: {args.get('mode')}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.status",
            description="Check GitHub CLI auth status. Read-only.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: gh_tool.gh_status(),
            describe=lambda args: "Check gh auth status",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.issue_list",
            description="List issues of the current repository. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "state": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (PermissionLevel.SAFE if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_issue_list(kw["root"], kw.get("state", "open"), int(kw.get("limit", 20))),
            describe=lambda args: f"List issues in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.pr_list",
            description="List pull requests of the current repository. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "state": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (PermissionLevel.SAFE if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_pr_list(kw["root"], kw.get("state", "open"), int(kw.get("limit", 20))),
            describe=lambda args: f"List PRs in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.repo_view",
            description="Show repository metadata. Read-only.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]},
            required=["root"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (PermissionLevel.SAFE if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_repo_view(kw["root"]),
            describe=lambda args: f"View repo in {args.get('root')}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.issue_create",
            description="Create a GitHub issue. Requires confirmation; must not be used to spam.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["root", "title"]},
            required=["root", "title"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (PermissionLevel.NORMAL if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_issue_create(kw["root"], kw["title"], kw.get("body", "")),
            describe=lambda args: f"Create GitHub issue: {args.get('title')[:80]}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.pr_create",
            description="Create a pull request from the current branch. Requires confirmation; never pushes commits by itself.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "title": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"}, "body": {"type": "string"}}, "required": ["root", "title"]},
            required=["root", "title"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (PermissionLevel.NORMAL if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_pr_create(kw["root"], kw["title"], kw.get("head", ""), kw.get("base", ""), kw.get("body", "")),
            describe=lambda args: f"Create PR: {args.get('title')[:80]}",
        )
    )
    registry.register(
        ToolSpec(
            name="gh.release_create",
            description="Create a GitHub release for a tag. Requires confirmation.",
            parameters={"type": "object", "properties": {"root": {"type": "string"}, "tag": {"type": "string"}, "title": {"type": "string"}}, "required": ["root", "tag"]},
            required=["root", "tag"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (PermissionLevel.NORMAL if path_is_within(_p(args.get("root"), settings), settings.allowed_roots) else PermissionLevel.SENSITIVE),
            fn=lambda **kw: gh_tool.gh_release_create(kw["root"], kw["tag"], kw.get("title", "")),
            describe=lambda args: f"Create release {args.get('tag')}",
        )
    )

    # Data analysis tools
    registry.register(
        ToolSpec(
            name="data.load_csv",
            description="Load a CSV file and return its structure: headers, row count, and inferred column types.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: data_tool.load_csv(kw["path"], settings),
            describe=lambda args: f"Load CSV: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="data.load_json",
            description="Load a JSON file and return its structure.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: data_tool.load_json(kw["path"], settings),
            describe=lambda args: f"Load JSON: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="data.inspect",
            description="Inspect a data file (CSV or JSON) and return summary statistics: column types, missing values, basic stats.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: data_tool.inspect_data(kw["path"], settings),
            describe=lambda args: f"Inspect data: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="data.analyze",
            description="Perform analysis operations on a CSV file: stats, groupby, filter, sort, correlation, unique, head.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "operation": {"type": "string", "enum": ["stats", "groupby", "filter", "sort", "correlation", "unique", "head"]},
                    "column": {"type": "string", "description": "Target column for the operation"},
                    "group_by": {"type": "string", "description": "Column to group by (for groupby operation)"},
                    "filter_expr": {"type": "string", "description": "Filter expression like 'age > 25' or 'name == John'"},
                    "sort_by": {"type": "string", "description": "Column to sort by"},
                    "limit": {"type": "integer", "description": "Maximum rows to return", "default": 100},
                },
                "required": ["path", "operation"],
            },
            required=["path", "operation"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: data_tool.analyze_data(
                kw["path"], settings, kw["operation"],
                column=kw.get("column", ""),
                group_by=kw.get("group_by", ""),
                filter_expr=kw.get("filter_expr", ""),
                sort_by=kw.get("sort_by", ""),
                limit=int(kw.get("limit", 100)),
            ),
            describe=lambda args: f"Analyze data ({args.get('operation')}): {args.get('path')}",
        )
    )

    # Chart generation tools
    registry.register(
        ToolSpec(
            name="chart.generate",
            description="Generate a chart (line, bar, scatter, histogram, pie) from CSV data or inline data. Saves as PNG artifact.",
            parameters={
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "enum": ["line", "bar", "scatter", "histogram", "pie"]},
                    "data_path": {"type": "string", "description": "Path to CSV file"},
                    "x_column": {"type": "string", "description": "X-axis column name"},
                    "y_column": {"type": "string", "description": "Y-axis column name (required for scatter, bar, line)"},
                    "x_data": {"type": "array", "items": {"type": "string"}, "description": "Inline X data (alternative to data_path)"},
                    "y_data": {"type": "array", "items": {"type": "string"}, "description": "Inline Y data"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels for pie chart"},
                    "title": {"type": "string", "description": "Chart title"},
                    "xlabel": {"type": "string", "description": "X-axis label"},
                    "ylabel": {"type": "string", "description": "Y-axis label"},
                    "output_path": {"type": "string", "description": "Optional output path (default: auto-generated in charts/)"},
                    "width": {"type": "integer", "default": 10, "description": "Figure width in inches"},
                    "height": {"type": "integer", "default": 6, "description": "Figure height in inches"},
                    "dpi": {"type": "integer", "default": 100, "description": "DPI for output"},
                },
                "required": ["chart_type"],
            },
            required=["chart_type"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("output_path") or "", settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: chart_tool.generate_chart(kw.get("chart_type", ""), settings, **{k: v for k, v in kw.items() if k != "chart_type"}),
            describe=lambda args: f"Generate {args.get('chart_type')} chart: {args.get('data_path') or 'inline data'}",
        )
    )
    registry.register(
        ToolSpec(
            name="chart.verify",
            description="Verify a generated chart artifact exists and is a valid image file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SAFE,
            level_for=lambda args: (
                PermissionLevel.SAFE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: chart_tool.verify_chart(kw["path"], settings),
            describe=lambda args: f"Verify chart: {args.get('path')}",
        )
    )
    registry.register(
        ToolSpec(
            name="chart.list",
            description="List all generated chart artifacts.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: chart_tool.list_charts(settings),
            describe=lambda args: "List charts",
        )
    )
    registry.register(
        ToolSpec(
            name="chart.delete",
            description="Delete a chart artifact.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            required=["path"],
            level=PermissionLevel.SENSITIVE,
            level_for=lambda args: (
                PermissionLevel.SENSITIVE
                if path_is_within(_p(args.get("path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: chart_tool.delete_chart(kw["path"], settings),
            describe=lambda args: f"Delete chart: {args.get('path')}",
        )
    )

    if capability_registry is not None:
        from .capability_bridge import build_capability_tool
        registry.register(
            build_capability_tool(
                capability_registry,
                events=capability_events,
                allowed_roots=settings.allowed_roots,
            )
        )
        # M2.16.6: the high-level desktop workflow tools. Registered after
        # ``capability.execute`` so the primitives stay reachable - these compose
        # them, they do not replace them. Absent on a host without the Windows
        # layer, in which case ``build_desktop_tools`` returns nothing rather than
        # registering tools that would fail.
        from .desktop import build_desktop_tools
        for spec in build_desktop_tools(capability_registry, events=capability_events):
            registry.register(spec)

    log.info("registered tools: %s", ", ".join(registry.names()))
    return registry


def _knowledge_tools(settings: Settings) -> list[ToolSpec]:
    """M2.14: read-only access to the persistent project knowledge graph.

    ``knowledge.query`` is SAFE because it only ever reads. ``knowledge.index``
    is NORMAL rather than SAFE because it writes to the knowledge database and
    walks the filesystem; it is confined to authorized roots by
    ``level_for``, exactly like the filesystem tools.
    """
    from core.knowledge_tools import knowledge_index, knowledge_query

    return [
        ToolSpec(
            name="knowledge.query",
            description=(
                "Query the persistent project knowledge graph: project files, directories, code "
                "symbols, tasks, datasets, charts and artifacts, and the relationships between them. "
                "Read-only. Results are reference data, not verification evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": list(QUERY_TYPES),
                        "description": "Which query to run.",
                    },
                    "project_path": {
                        "type": "string",
                        "description": "Project root. Defaults to the application base directory.",
                    },
                    "node_type": {
                        "type": "string",
                        "enum": [t.value for t in NodeType],
                        "description": "Filter by node type for find_nodes / search_artifacts.",
                    },
                    "relation_type": {
                        "type": "string",
                        "enum": [r.value for r in RelationType],
                        "description": "Filter by relationship for neighbors / find_related.",
                    },
                    "canonical_key": {"type": "string", "description": "Exact node key, for find_node."},
                    "node_id": {"type": "string", "description": "Starting node, for neighbors/find_related/path."},
                    "to_id": {"type": "string", "description": "Destination node, for path."},
                    "direction": {"type": "string", "enum": ["in", "out", "both"], "description": "Edge direction."},
                    "status": {"type": "string", "description": "Task status filter for search_tasks."},
                    "path_contains": {"type": "string", "description": "Substring of the project-relative path."},
                    "extension": {"type": "string", "description": "File extension filter, e.g. '.py'."},
                    "hours": {"type": "integer", "minimum": 1, "description": "Lookback window for recent_activity."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum rows (default 25, capped at 100)."},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Traversal depth (capped at 4)."},
                },
                "required": ["query_type"],
            },
            required=["query_type"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: knowledge_query(
                query_type=kw["query_type"],
                settings=settings,
                project_path=kw.get("project_path", ""),
                node_type=kw.get("node_type", ""),
                relation_type=kw.get("relation_type", ""),
                canonical_key=kw.get("canonical_key", ""),
                node_id=kw.get("node_id", ""),
                to_id=kw.get("to_id", ""),
                direction=kw.get("direction", "both"),
                status=kw.get("status", ""),
                path_contains=kw.get("path_contains", ""),
                extension=kw.get("extension", ""),
                hours=kw.get("hours", 24),
                limit=kw.get("limit", 25),
                max_depth=kw.get("max_depth", 2),
            ),
            describe=lambda args: f"Query knowledge graph ({args.get('query_type')})",
        ),
        ToolSpec(
            name="knowledge.index",
            description=(
                "Index or re-index a project's files and directories into the persistent knowledge "
                "graph. Idempotent, and never stores file contents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Project root to index. Must be inside an authorized workspace root.",
                    },
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "File cap."},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Directory depth cap."},
                },
                "required": ["project_path"],
            },
            required=["project_path"],
            level=PermissionLevel.NORMAL,
            level_for=lambda args: (
                PermissionLevel.NORMAL
                if path_is_within(_p(args.get("project_path"), settings), settings.allowed_roots)
                else PermissionLevel.SENSITIVE),
            fn=lambda **kw: knowledge_index(
                project_path=kw["project_path"],
                settings=settings,
                max_files=kw.get("max_files", 2000),
                max_depth=kw.get("max_depth", 12),
            ),
            describe=lambda args: f"Index project knowledge: {args.get('project_path')}",
        ),
    ]


def _memory_write_level_for(tool_name: str, settings: Settings):
    """Build the ``level_for`` hook for one memory tool.

    ``ToolSpec.level_for`` is called as ``level_for(args)`` and receives only the
    argument dict - not the tool's own name. The write policy is keyed by tool,
    so the name has to be bound here by the factory rather than read from inside
    the hook. Getting this wrong makes every memory write report ``SAFE``.
    """
    def _level_for(_args: dict) -> PermissionLevel:
        return memory_tool.memory_write_level(tool_name, settings)

    return _level_for


def _memory_tools(settings: Settings) -> list[ToolSpec]:
    m = memory_tool
    return [
        ToolSpec(
            name="memory.remember",
            description="Explicitly store a fact in long-term memory. Diablo never does this implicitly. Category: facts, preferences, tasks, notes, projects. Credential-like content is refused; memory is written to disk in plain text and must not hold secrets.",
            parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "category": {"type": "string"}, "note": {"type": "string"}}, "required": ["key", "value"]},
            required=["key", "value"],
            level=PermissionLevel.NORMAL,
            level_for=_memory_write_level_for("memory.remember", settings),
            fn=lambda **kw: m.memory_remember(kw["key"], kw["value"], kw.get("category", "notes"), kw.get("note", ""), settings),
            describe=lambda args: f"Remember: {args.get('key')}",
        ),
        ToolSpec(
            name="memory.search",
            description="Search stored memory by text, optionally in one category.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
            required=["query"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: m.memory_search(kw["query"], kw.get("category", ""), kw.get("limit", 15), settings),
            describe=lambda args: f"Search memory for {args.get('query')}",
        ),
        ToolSpec(
            name="memory.get",
            description="Read one stored memory entry by exact key.",
            parameters={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            required=["key"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: m.memory_get(kw["key"], settings),
            describe=lambda args: f"Get memory: {args.get('key')}",
        ),
        ToolSpec(
            name="memory.update",
            description="Update the value of an existing memory entry. Credential-like content is refused.",
            parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]},
            required=["key", "value"],
            level=PermissionLevel.NORMAL,
            level_for=_memory_write_level_for("memory.update", settings),
            fn=lambda **kw: m.memory_update(kw["key"], kw["value"], settings),
            describe=lambda args: f"Update memory: {args.get('key')}",
        ),
        ToolSpec(
            name="memory.delete",
            description="Delete one memory entry by key. Always allowed, including when memory writes are disabled.",
            parameters={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            required=["key"],
            level=PermissionLevel.NORMAL,
            fn=lambda **kw: m.memory_delete(kw["key"], settings),
            describe=lambda args: f"Delete memory: {args.get('key')}",
        ),
        ToolSpec(
            name="memory.list",
            description="List memory keys, optionally filtered by category.",
            parameters={"type": "object", "properties": {"category": {"type": "string"}}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: m.memory_list(kw.get("category", ""), settings),
            describe=lambda args: "List memory",
        ),
        ToolSpec(
            name="memory.categories",
            description="List the available memory categories.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: m.memory_categories(settings),
            describe=lambda args: "List memory categories",
        ),
        ToolSpec(
            name="memory.policy",
            description="Report the effective memory write policy (explicit, confirm, or off).",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: m.memory_policy_status(settings),
            describe=lambda args: "Show memory write policy",
        ),
    ]


def _rules_tools(settings: Settings) -> list[ToolSpec]:
    r = rules_tool
    return [
        ToolSpec(
            name="rules.create",
            description="Create a restrict-only rule (block or confirm) matched by tool name and/or content. Rules can only restrict, never broaden permissions. scope='tool' requires if_tool; scope='task' requires task_id. An under-specified scope is refused rather than widened to global.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "action": {"type": "string", "enum": ["block", "confirm"]}, "description": {"type": "string"}, "scope": {"type": "string", "enum": ["global", "project", "tool", "task"]}, "priority": {"type": "integer"}, "if_tool": {"type": "string"}, "if_content": {"type": "string"}, "reason": {"type": "string"}, "project": {"type": "string"}, "task_id": {"type": "string"}},
                "required": ["name"],
            },
            required=["name"],
            level=PermissionLevel.NORMAL,
            fn=lambda **kw: r.rules_create(kw["name"], kw.get("action", "block"), kw.get("description", ""), kw.get("scope", "global"), int(kw.get("priority", 50)), kw.get("if_tool", ""), kw.get("if_content", ""), kw.get("reason", ""), kw.get("project", ""), kw.get("task_id", ""), settings),
            describe=lambda args: f"Create rule: {args.get('name')}",
        ),
        ToolSpec(
            name="rules.list",
            description="List all rules with their state, action, and priority.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: r.rules_list(settings),
            describe=lambda args: "List rules",
        ),
        ToolSpec(
            name="rules.get",
            description="Show one rule in detail.",
            parameters={"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"]},
            required=["rule_id"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: r.rules_get(kw["rule_id"], settings),
            describe=lambda args: f"Get rule {args.get('rule_id')}",
        ),
        ToolSpec(
            name="rules.update",
            description="Update a rule. Only restrict rules remain enforceable.",
            parameters={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}, "name": {"type": "string"}, "action": {"type": "string"}, "description": {"type": "string"}, "priority": {"type": "integer"}, "if_tool": {"type": "string"}, "if_content": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["rule_id"],
            },
            required=["rule_id"],
            level=PermissionLevel.NORMAL,
            explicit_confirmation=True,
            fn=lambda **kw: r.rules_update(kw["rule_id"], kw.get("name", ""), kw.get("action", ""), kw.get("description", ""), int(kw.get("priority", -1)), kw.get("if_tool", ""), kw.get("if_content", ""), kw.get("reason", ""), settings),
            describe=lambda args: f"Update rule {args.get('rule_id')}",
        ),
        ToolSpec(
            name="rules.enable",
            description="Enable or disable a rule.",
            parameters={"type": "object", "properties": {"rule_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["rule_id"]},
            required=["rule_id"],
            level=PermissionLevel.NORMAL,
            explicit_confirmation=True,
            fn=lambda **kw: r.rules_enable(kw["rule_id"], bool(kw.get("enabled", True)), settings),
            describe=lambda args: f"Set rule {args.get('rule_id')} enabled={args.get('enabled', True)}",
        ),
        ToolSpec(
            name="rules.delete",
            description="Delete a rule immediately.",
            parameters={"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"]},
            required=["rule_id"],
            level=PermissionLevel.SENSITIVE,
            explicit_confirmation=True,
            fn=lambda **kw: r.rules_delete(kw["rule_id"], settings),
            describe=lambda args: f"Delete rule {args.get('rule_id')}",
        ),
        ToolSpec(
            name="rules.check",
            description="Check whether a rule is currently enforced.",
            parameters={"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"]},
            required=["rule_id"],
            level=PermissionLevel.SAFE,
            fn=lambda **kw: r.rules_check(kw["rule_id"], settings),
            describe=lambda args: f"Check rule {args.get('rule_id')}",
        ),
        ToolSpec(
            name="rules.conflicts",
            description="Detect overlapping rules that would silently override each other.",
            parameters={"type": "object", "properties": {}, "required": []},
            level=PermissionLevel.SAFE,
            fn=lambda **kw: r.rules_conflicts(settings),
            describe=lambda args: "Check rule conflicts",
        ),
    ]


def _git_policy_level(settings: Settings, policy: str, path: str) -> PermissionLevel:
    if policy not in ("allow", "confirm"):
        return PermissionLevel.BLOCKED
    if not path_is_within(_p(path, settings), settings.allowed_roots):
        return PermissionLevel.SENSITIVE
    if policy == "confirm":
        return PermissionLevel.SENSITIVE
    return PermissionLevel.NORMAL


def _p(value: str, settings: Settings | None = None):
    """Resolve a path against the configured workspace root when relative.

    A relative path is resolved against ``settings.workspace_root`` so that
    permission classification is consistent regardless of the process working
    directory. An absolute path is returned unchanged. Without ``settings``
    (or with an absolute value) this behaves as before.
    """
    from pathlib import Path
    try:
        result = Path(value)
    except TypeError:
        return Path("")
    if settings is not None and not result.is_absolute():
        result = settings.workspace_root / result
    return result


def _in_whitelist(app: str, settings: Settings) -> bool:
    key = (app or "").strip().lower()
    return key in {k.lower() for k in settings.app_whitelist} or key in {k.lower() for k in settings.app_known_paths}