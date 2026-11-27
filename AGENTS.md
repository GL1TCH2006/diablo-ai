# Diablo AI - rules for coding agents

Diablo is a local-first Windows computer agent. Improve it without breaking its
architecture, user data, security boundaries, or git history.

## Before any change

- Run `git status --short --branch` and inspect recent commits and diffs.
- Read the real source and tests for the task. Old handovers may be stale.
- Identify untracked files and pre-existing modifications and preserve them.
- Work on a new branch. One task at a time.

## Architecture rules

- One authoritative Agent, Executor, TaskManager/TaskStore, PermissionManager,
  VerificationLedger, and capability registry. No competing pipelines.
- Keep reasoning, permissions, execution, verification, and persistence apart.
- Tool output and model claims are untrusted until verified independently.
- Keep execution failure, assertion failure, and UNVERIFIED distinct.
- The UI only displays runtime state; it never decides success itself.

## Safety rules

- No commit, reset, clean, stash, push, or overwrite without explicit approval.
- Never weaken path containment, permissions, secret handling, or approvals
  to make a test pass. Never skip or delete failing tests.
- Bound retries, tool calls, and run time. Use disposable workspaces for
  real-model tests.

## Reporting

- Report root cause, files changed, exact commands, exit codes, and
  pass/fail/skip counts, remaining risks, and final git status.
- Do not claim success without evidence.