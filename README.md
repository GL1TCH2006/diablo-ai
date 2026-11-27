# DIABLO — local-first AI assistant & development agent for Windows

DIABLO is a local-first, JARVIS-style AI assistant for Windows. It plans tasks,
calls **structured tools** through a permission-checked executor, verifies its own
results, remembers context on disk, speaks through Windows SAPI, and can automate
the desktop and Blender — all running on a local Ollama model.

Everything runs on your machine. There is no cloud provider and no telemetry.

## Status

Phases 1 and 2 are complete. Milestones through **M2.17** are built; the project's
own audit trail is in `docs/`, and the most recent complete audit is
`docs/DIABLO_M2_16_UI_M2_17_COMPLETE_AUDIT.txt`.

Built and wired: core agent, tool/permission system, project intelligence,
targeted patches, git/GitHub tools, change tracking, debug loop with
auto-verification, memory + restrict-only rules, adaptive model router, a
persistent project knowledge graph, native Windows automation (processes, windows,
pointer, keyboard, displays, screen capture, UI Automation), high-level desktop
workflows, Blender, a bounded vision layer, persistent autonomy, and three
front-ends.

Not built: speech-to-text, audio wake word, barge-in, `web_search`, a general
model. These are listed under **Known limitations** rather than implied.

---

## Architecture

```
USER → AGENT → PLANNER → TOOL ROUTER → PERMISSION CHECK → EXECUTOR → VERIFICATION → RESULT
```

- The AI **never executes arbitrary code directly**. It emits structured tool calls,
  each gated by a permission level before the executor runs it.
- Well-known phrases (greeting / open app / "hello world" / system info) take a
  fast path in the planner; everything else is decided by the LLM tool-calling loop.
- **Tool output and the model's own claims are untrusted.** A verdict is only
  `PASSED` when an independent observer produced observed evidence; the ledger
  downgrades anything else to `UNVERIFIED` rather than accepting it.
- Execution failure, assertion failure, and `UNVERIFIED` stay distinct. A task can
  finish without ever having been verified, and the system says so.
- The UI only displays runtime state. It never decides success itself.

## Capabilities

**74 tools and 77 capability operations across 6 registered capabilities.**

There are two counting bases, and they differ by exactly 6. Both are correct for
what they count; quoting either without saying which is how this file previously
ended up wrong.

| | Bare registry | Production runtime |
|---|---|---|
| Construction | `build_registry(Settings())` | `main.build_capability_runtime(settings, events)` then `build_registry(settings, capability_registry=cap, capability_events=events)` |
| Tools | **68** | **74** |
| Extra tools | — | `capability.execute`, `desktop.open`, `desktop.find`, `desktop.interact`, `desktop.read`, `desktop.verify` |

The production figure is the one that describes a running agent: the capability
runtime registers the six capability layers, and those add their dispatch tools to
the registry. The 68 figure is the registry with no capability layers attached.

| Number | Counting basis | Reproduce with |
|---|---|---|
| 74 tools | production: capability runtime attached | `len(build_registry(Settings(), capability_registry=cap, capability_events=events).names())` |
| 68 tools | bare registry, no capability runtime | `len(build_registry(Settings()).names())` |
| 18 `git.*`, 7 `gh.*` | prefix split of either list | same |
| 6 registered capabilities | names registered in the runtime | `len(cap.capabilities())` |
| 77 capability operations | summed over the registered capabilities | `sum(len(c['operations']) for c in cap.capabilities())` |
| 38 Windows / 16 Blender / 5 Vision / 5 desktop / 7 blender_vision / 6 blender_vision_workflow | per-capability operation lists | `cap.capabilities()` |

> **Not to be confused with** the number of *classes* whose name ends in
> `CapabilityAdapter` (5 in `integration/contracts.py`, 8 exported from
> `integration`). That counts source files, not capabilities: several adapters
> cover more than one registered capability, and `DesktopWorkflow` is composed
> from the Windows layer rather than having an adapter of its own.

| Area | What is available |
|---|---|
| Files & code | `filesystem.*`, `code.patch_file`, `project.info/files/grep/verify` |
| Git / GitHub | 18 `git.*` tools, 7 `gh.*` tools; `push`/`restore`/`reset`/`rebase` **blocked by policy** |
| Windows desktop | 38 native operations: processes, windows, pointer, keyboard, displays, screen capture, UI Automation, visual target claims |
| Desktop workflows | 5 intent-level tools (`desktop.open/find/interact/read/verify`) over the above |
| Blender | 16 scene/material/camera/render operations, plus 6 `blender_vision_workflow` operations (this file previously said 13 here; the measured count is 6) |
| Vision | 5 operations, deterministic fallback backend; optional local VLM (not wired by default) |
| Data & charts | CSV/JSON loading, statistics, chart generation |
| Memory & rules | long-term memory, restrict-only rules, project knowledge graph |

## User interfaces

Three front-ends, all views over the **same** Agent, TaskManager, Executor and
permission path. None of them owns a backend.

| Flag | Front-end | Notes |
|---|---|---|
| *(default)* | `rich` console REPL | commands `:model :memory :prefs :projects :rules :why :wipe :quit :help` |
| `--companion` | Tk floating window | the "product" window; falls back to the console if Tk is unavailable |
| `--edge-companion` | Tk borderless edge-docked overlay | sleeps at a screen edge, wakes on click; `--edge left\|right\|top\|bottom`, `--edge-speak` |

`--edge-speak` routes replies through the existing SAPI text-to-speech. There is
**no** speech-to-text and **no** wake word: activation is by click or by typing.

## Autonomous operation

`--autonomy` turns on the persistent goal layer (M2.17). It is **off by default**
because a goal becomes a Task without the user asking for that specific request.

- Goals are durable (`GoalStore`), and a goal completes only when it has
  **verified** evidence — never because a task happened to finish.
- A bounded scheduler (`--autonomy-interval`, default 30 s) enforces ceilings on
  active goals, tasks and concurrency.
- Startup reconciliation re-reads persisted goals and decides, per goal, whether to
  resume, pause, fail, cancel or block on permission.
- A step that was in flight when the process died is **not** replayed if its side
  effect is uncertain.

## Voice

| Component | State |
|---|---|
| Text-to-speech (Windows SAPI) | **implemented**, works, off by default (`VOICE_ENABLED=false`) |
| Speech-to-text | **not implemented** — every backend raises `SpeechToTextUnavailable`; none decodes audio |
| Wake word | **not implemented as audio** — a prefix match over text that already exists, not a microphone |
| Barge-in | **not implemented** — no microphone, and playing audio cannot be stopped |
| Task cancellation | **implemented** and real (`voice/cancellation.py`) |

`voice/capabilities.py` reports this truthfully, and no `voice.*` capability is
registered with the model, so nothing model-facing claims speech exists.

---

## Project structure

```
diablo-ai/
├── main.py                 # entry point: UI selection, --demo, --autonomy, flags
├── run.ps1                 # launcher (uses .venv)
├── scripts/
│   └── run_deterministic.ps1   # the test runner (chunks + 120 s timeout)
├── requirements.txt        # rich, httpx, Pillow, numpy, matplotlib, pywin32, comtypes
├── pyproject.toml
├── config/                 # settings, permissions, default_config.toml
├── core/                   # agent, planner, executor, task manager/store,
│                           # verification + ledger, autonomy + goals, recovery,
│                           # debug loop, knowledge graph, desktop intent,
│                           # windows/ (native Win32 layer)
├── ai/                     # providers (Ollama/Offline), model manager, router
├── tools/                  # registry + capability bridge + tool implementations
├── integration/            # capability contracts/registry, desktop workflows
├── coding/                 # project profile, targeted patches, change tracking
├── memory/                 # store, conversation, preferences, rules, sanitizer
├── blender/                # detection, runner, capability layer, ops, templates
├── vision/                 # protocol, inspection, backends, capability layer
├── voice/                  # tts (works), stt/wake word (raise), cancellation
├── ui/                     # rich terminal UI, Tk companions, state machine
├── workspace/              # generated code + samples/flask-auth-demo fixture
├── data/                   # runtime state (gitignored)
├── docs/                   # milestone audits, handovers, cross-layer contracts
└── tests/                  # the test suite
```

## Requirements

- Python 3.11+ (developed on 3.14), Windows 10/11
- [Ollama](https://ollama.com) with a tool-capable model (`qwen3:8b` / `qwen3:14b`)
- `pywin32` and `comtypes` for the native Windows layer (Windows only; the agent
  degrades rather than failing without them)
- Blender, optional, for the Blender capability

## Install

```powershell
cd diablo-ai
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
ollama pull qwen3:8b
```

`python` on `PATH` may be the Microsoft Store alias stub. Always invoke the
project interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pytest ...
```

## Configuration

Copy `.env.example` to `.env` and edit. `.env` is gitignored; never commit it.

| Variable | Meaning |
|---|---|
| `AI_PROVIDER` | `ollama` (default) or `offline` |
| `OLLAMA_HOST` | default `http://localhost:11434` |
| `AI_MODEL` | optional; empty means auto-detect an installed tool-capable model |
| `WORKSPACE_ROOT` | root scanned for project awareness |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `VOICE_ENABLED` | `true` to speak replies through SAPI |

The rest of the configuration lives in `config/default_config.toml`.

## Run

```powershell
.\run.ps1                                     # interactive terminal UI
.\.venv\Scripts\python.exe main.py --companion        # floating companion window
.\.venv\Scripts\python.exe main.py --edge-companion    # edge-docked companion
.\.venv\Scripts\python.exe main.py --edge-companion --edge left --edge-speak
.\.venv\Scripts\python.exe main.py --autonomy          # persistent goals (opt-in)
.\.venv\Scripts\python.exe main.py --demo               # Phase 1 verification sequence
.\.venv\Scripts\python.exe main.py --demo-phase2        # Phase 2 verification sequence
```

All flags:

| Flag | Effect |
|---|---|
| `--offline` | force the rule-based backend even if Ollama is up |
| `--model <name>` | override the Ollama model for this run |
| `--host <url>` | override the Ollama host |
| `--log-level <level>` | logging level for this run |
| `--companion` | floating Tk companion; falls back to the console UI |
| `--edge-companion` | edge-docked Tk companion; falls back to the console UI |
| `--edge <side>` | `left` / `right` / `top` / `bottom` (default `right`) |
| `--edge-speak` | speak state changes through SAPI |
| `--autonomy` | reconcile durable goals at startup and run the bounded scheduler |
| `--autonomy-interval <s>` | seconds between scheduler passes (default 30) |
| `--demo`, `--demo-phase2` | non-interactive verification sequences |

## Permission model

| Level       | Example                         | Behaviour                     |
|-------------|---------------------------------|-------------------------------|
| SAFE        | `filesystem.read_file`          | auto-run                      |
| NORMAL      | whitelisted `open notepad`      | auto-run                      |
| SENSITIVE   | `pip install`, unknown app      | confirmation prompt           |
| BLOCKED     | `shutdown /s`, `format`         | refused, never executed       |

Destructive commands are checked twice: in `PermissionManager.evaluate` and again
inside `tools/terminal.run_command`. Writes to existing files are backed up to
`data/backups/` first. Git push/restore/reset/rebase are blocked by default and
enable only through configuration.

Rules are **restrict-only**: they can block or confirm a tool, never raise its base
permission. Rule matching is pattern-based (`if_tool`, `if_content` by substring).

## Tests

```powershell
.\scripts\run_deterministic.ps1            # full deterministic suite, chunked
.\scripts\run_deterministic.ps1 -SmokeTest # the runner's own self-test, ~30 s
```

The runner uses `.venv\Scripts\python.exe`, prints the collected count first, runs
the suite in chunks of 15 files with a 120 second timeout per invocation (bisecting
automatically when a chunk times out), and exits nonzero on any failure. It never
sets an opt-in gate and refuses to run if one is already set.

Current state: the deterministic suite collects and reconciles exactly; see
`data/run_reports/FINAL7.md` for the current figures. Three tests fail: two are
environmental (`python` on PATH is the Microsoft Store alias stub - see Known
limitations), and the third is a known load-sensitive race in the companion
worker, documented in `data/run_reports/b5_race.md` and **not fixed**.

### Opt-in suites

These require hardware, a model, or both, and are **skipped** unless explicitly
enabled. They are the only tests that are not part of the default run.

| Variable | Enables |
|---|---|
| `DIABLO_REAL_MODEL_TESTS=1` | real-model suites (needs Ollama up and a tool-capable model) |
| `DIABLO_REAL_WINDOWS_TESTS=1` | real desktop automation against a live session |
| `DIABLO_REAL_BLENDER_TESTS=1` | real Blender tests (needs Blender installed) |
| `DIABLO_P9_MODEL` | which model the real-model suites use (default `qwen3:8b`) |
| `DIABLO_P9_TIMEOUT` | per-turn provider timeout, seconds (default 900) |

```powershell
$env:DIABLO_REAL_MODEL_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_m217_real_model_autonomy.py
Remove-Item Env:\DIABLO_REAL_MODEL_TESTS
```

## Roadmap

Delivered: Phase 1 (core agent, tools, permissions, coding, memory, TTS, UI) and
Phase 2 (git/GitHub, project intelligence, targeted patches, debug loop, memory and
restrict-only rules, adaptive model router), plus the M2.13–M2.17 milestones
documented in `docs/` — durable plans, verification ledger, cross-layer capability
contracts, the native Windows layer, high-level desktop workflows, the companion
UIs, and persistent autonomy.

Not delivered, and not partially claimed here:

- speech-to-text, audio wake word, barge-in
- `web_search`
- multi-agent / multi-machine

See `docs/DIABLO_M2_16_UI_M2_17_COMPLETE_AUDIT.txt` for the milestone-by-milestone
roadmap state, including what is complete-with-limitations.

## Known limitations

- **Speech-to-text, audio wake word and barge-in are not implemented.** The STT
  backends raise `SpeechToTextUnavailable`; the wake-word matcher is a prefix match
  over text that already exists, not audio detection; there is no microphone
  capture. Only text-to-speech works.
- `web_search` is an interface only (stub); not shipped.
- Two tests fail on a host where `python` on `PATH` is the Microsoft Store alias
  stub — they invoke `python` by name. Not a code defect; install Python or put a
  real `python.exe` on `PATH`.
- Two filesystem-containment tests **skip** without `SeCreateSymbolicLinkPrivilege`
  (symlink creation). Run the suite elevated to execute them.
- `python.run_script` and `terminal.execute` are **not sandboxes**: subprocesses run
  with your user's full privileges, gated only by permissions and directory
  restrictions.
- Long-term memory writes are explicit-only by default
  (`memory_write_policy=explicit`).
- The resource-awareness gate is optional and off by default
  (`resource_check_enabled=false`); it is a documented extension hook, not a
  sandbox.
- The model router trusts Ollama's `parameter_size` metadata and falls back to name
  parsing when metadata is absent.
- Fast-path planner intents cover only greeting / open-app / hello-world / sysinfo;
  all other tool use requires Ollama.
- Vision uses a deterministic fallback backend by default. The optional local
  vision-language backend requires `torch` + `transformers` and is never
  auto-loaded.
- Real-model, real-Windows and real-Blender coverage is opt-in and has not been run
  as part of the default suite.
- Windows-only (SAPI TTS, Start-Apps lookup, native layer, PowerShell).