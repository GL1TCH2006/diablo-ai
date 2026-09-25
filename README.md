# DIABLO — Personal AI Assistant & Development Agent (Phase 1+2)

DIABLO is a local-first, JARVIS-style AI assistant for Windows. It plans tasks,
calls **structured tools** through a permission-checked executor, verifies its own
results, remembers context on disk, talks through Windows SAPI, and controls apps
and Blender — all running on a local Ollama model.

Status: **Phase 1 + Phase 2 complete** — core agent, tool/permission system,
project intelligence, targeted patches, git/GitHub tools, change tracking, debug
loop with auto-verification, memory + restrict-only rules, adaptive model router,
and `--demo` / `--demo-phase2` verification sequences.

---

## Architecture

```
USER → AGENT → PLANNER → TOOL ROUTER → PERMISSION CHECK → EXECUTOR → VERIFICATION → RESULT
```

- The AI **never executes arbitrary code directly**. It emits structured tool calls,
  each gated by a permission level before the executor runs it.
- Well-known phrases (greeting / open app / "hello world" / system info) take a
  fast path in the planner; everything else is decided by the LLM tool-calling loop.
- Verification runs after execution (file exists, output matches, process running).

## Features (Phase 1)

- **Local LLM backend** — Ollama with `qwen3` family models (native tool calling), plus a
  rule-based offline fallback that is honestly labeled "limited mode".
- **Tool system** — `filesystem.*`, `terminal.execute`, `python.run_script`,
  `applications.open`, `system.info`.
- **Permission system** — SAFE / NORMAL / SENSITIVE / BLOCKED; destructive-command
  blocklist (`shutdown /s`, `format`, …); auto-allow patterns (`git status`, …);
  per-tool allow/deny persisted; Level-2 confirmation prompts.
- **Coding agent** — creates `workspace/hello_world.py`, runs it, verifies output.
- **App launching** — curated whitelist (auto) + Windows Start-Apps dynamic lookup
  (confirmation).
- **Memory** — conversation history, preferences, and detected projects as atomic
  JSON under `data/memory/`.
- **Blender detection** — locates the Blender executable and reports its version
  (verified: **v5.2.2**).
- **Voice** — Windows SAPI text-to-speech works now; STT & wake word are
  documented interfaces only (Phase 6).
- **Terminal UI** — Rich banner/status, commands `:model :memory :prefs :projects
  :wipe :quit :help`.
- **Logging** — rotating file log at `data/logs/diablo.log` + console, with secret
  redaction.

## Features (Phase 2)

- **Project intelligence** — `project.info` builds a project profile (`coding/`):
  language, framework, build system, detected commands, and git state.
- **Context & tokens** — CJK-aware `est_tokens`, truncation, and a weighted
  `ContextBuilder` keep LLM context within `context_max_tokens`.
- **Change tracking** — `:mission` (`:ms`) shows the plan/goal and `:gitdiff`
  (`:gd`) shows uncommitted diff; coding sessions persist to `data/sessions/`.
- **Targeted patches** — `code.patch_file` applies `=== path ===` hunks with
  replace/insert/delete actions; the affected file is backed up first.
- **Git tools** — read-only (status/diff/log/fetch) and destructive (add/commit/
  push/restore/reset/pull/rebase); push/restore/reset are **blocked by default**
  and destructive ops back up a patch first (`_gate`).
- **GitHub CLI** — `gh.status`, `gh.issue_list`, `gh.pr_list`, `gh.repo_view`
  (SAFE) and `gh.issue_create`, `gh.pr_create`, `gh.release_create` (SENSITIVE).
- **Debug loop** — `respond()` routes failing-command work to a step-limited
  `DebugLoop` (`debug_max_attempts=3` per error signature, dedup prevents loops)
  and auto-verifies with the project's detected test command.
- **Memory & rules** — long-term store (`memory_remember/search/…`) and a
  **restrict-only** rule engine (block > confirm > base) enforced by
  `PermissionManager`; project sections (architecture, decisions, …). UI
  `:rules` (`:r`) and `:why` (`:why`) reveal the moment-to-moment context and
  permission findings.
- **Adaptive model router** — `ai/model_router.py` picks `qwen3:8b` (fast) vs
  `qwen3:14b` (strong) by complexity, and **escalates to strong after a failed
  verification**; identities are parsed from Ollama `parameter_size` metadata.
  Modes: `auto` / `force8b` / `force14b` / `project`; `:model` shows the choice
  and its source. Escalation, manual override, and project preference are
  verified by the Phase 2 demo.

## Project structure

```
diablo-ai/
├── main.py                 # entry point: interactive UI, --demo, --demo-phase2
├── run.ps1                 # launcher (uses .venv)
├── requirements.txt        # rich, httpx, pytest
├── pyproject.toml
├── config/                 # settings, permissions, default_config.toml
├── core/                   # agent, planner, executor, task manager, verification,
│                           # context, context_builder, debug_loop, events, constants
├── ai/                     # providers (Ollama/Offline), model manager, tokenizer,
│                           # model_router (adaptive 8b/14b routing)
├── tools/                  # registry + filesystem/terminal/python/apps/system/
│                           # coding/git/github/patches/memory/rules/models
├── coding/                 # profile, patch (targeted edits), change_tracker,
│                           # relevance, project_index, dependency_analyzer,
│                           # entry_points, frameworks, commands, code_analyzer
├── memory/                 # storage, conversation, preferences, project memory,
│                           # rules_store, rule_engine
├── blender/                # BlenderController, scripts/, templates/
├── voice/                  # tts (works), stt + wake word (interfaces)
├── ui/                     # Rich terminal UI
├── workspace/              # generated code + samples/flask-auth-demo fixture
├── data/                   # memory/, sessions/, logs/, backups/  (gitignored)
└── tests/                  # 166 pytest tests
```

## Requirements

- Python 3.14+ (works on 3.10+), Windows 10/11
- [Ollama](https://ollama.com) with a tool-capable model (`qwen3:latest` default)
- Blender (optional, for Phase 4)

## Install

```powershell
cd diablo-ai
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
ollama pull qwen3:latest     # if not already present
```

## Run

```powershell
.\run.ps1                          # interactive terminal UI
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py --demo        # Phase 1 verification sequence
.\.venv\Scripts\python.exe main.py --demo-phase2 # Phase 2 verification sequence
#     (requires Ollama up + qwen3:8b/qwen3:14b; runs the live debug-loop fix on
#      workspace/samples/flask-auth-demo, then the router/escalation steps)
# flags: --offline  --model <name>  --host <url>  --log-level DEBUG
```

## Permission model

| Level       | Example                         | Behaviour                     |
|-------------|---------------------------------|-------------------------------|
| SAFE        | `filesystem.read_file`          | auto-run                      |
| NORMAL      | whitelisted `open notepad`      | auto-run                      |
| SENSITIVE   | `pip install`, unknown app     | confirmation prompt           |
| BLOCKED     | `shutdown /s`, `format`         | refused, never executed       |

Destructive commands are checked twice: in `PermissionManager.evaluate` and again
inside `tools/terminal.run_command` (defense in depth). Writes to existing files
are backed up to `data/backups/` first.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q     # 166 passed
```

Covers permissions, registry, filesystem, terminal (blocklist), python runner,
app launching, project detection, offline provider, verification, agent tool-call
round-trip, integration flows, plus Phase 2: project intelligence, context/token
budgets, change tracking, targeted patches, git tools, debug loop, memory &
rules, and the adaptive model router.

## Roadmap

- **Phase 1 ✅** core agent, tools, permissions, coding Hello World, memory, TTS, UI
- **Phase 2 ✅** git/GitHub tools, project intelligence, targeted patches & debug
  loop, memory + restrict-only rules, adaptive model router
- **Phase 3** proactive behaviors, scheduled tasks, richer planning
- **Phase 4** Blender scene building from natural language
- **Phase 5** multi-agent / multi-machine
- **Phase 6** speech-to-text + wake word ("Diablo")

## Known limitations

- `web_search` remains an **interface only** (stub) — Phase 2 goal, not shipped.
- STT and wake word are **interfaces only** (`NotImplementedError`) — Phase 6.
- `python.run_script` and `terminal.execute` are **not sandboxes**: subprocesses
  run with your user's full privileges; gated only by permissions and directory
  restrictions.
- Rules are **restrict-only**: they can block or confirm tool use but never raise
  the base permission of a tool.
- Rule detection is **pattern-based** (`if_content`, matched by substring).
- Long-term memory writes are **explicit-only** by default
  (`memory_write_policy=explicit`).
- The resource-awareness gate is **optional and off by default**
  (`resource_check_enabled=false`); it is a documented extension hook, not a
  sandbox.
- The model router trusts Ollama's `parameter_size` metadata and falls back to
  name parsing (`qwen3:14b` → 14B, `qwen3:8b` → 8B) when metadata is absent.
- Fast-path planner intents cover only greeting / open-app / hello-world / sysinfo;
  all other tool use requires Ollama.
- Ollama `/api/tags` reports `context_length: 0` for these models (display only).
- UWP-only apps found by Start-Apps search may not be launchable.
- Windows-only (SAPI TTS, Start-Apps lookup, PowerShell).
