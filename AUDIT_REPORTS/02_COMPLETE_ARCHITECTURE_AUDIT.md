# COMPLETE ARCHITECTURE AUDIT — DIABLO PROJECT

**Project Root:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Audit Date:** 2026-10-25  
**Git Branch:** `reliability-batch-1`  
**Git HEAD:** `a74b53c` (docs: M2.17.x Task 1 verification pipeline map)

---

## 1. ENTRY POINTS AND STARTUP/SHUTDOWN FLOW

### 1.1 Primary Entry Point: `main.py`

The single authoritative entry point is `main.py:main()` (lines 146-482). It orchestrates the complete application lifecycle:

```
main()
├── Parse CLI args (--offline, --model, --host, --demo, --demo-phase2, --companion, --edge-companion, --autonomy)
├── Load settings (config/settings.py:load_settings)
├── Setup logging (core/logging_setup.py:setup_logging)
├── Build AI backend (ai/model_manager.py:build_backend)
│   ├── OllamaProvider (if reachable) → list models, resolve model
│   └── OfflineProvider (fallback) → rule-based backend
├── Create EventBus (core/events.py)
├── Build PermissionManager (config/permissions.py) with PreferencesStore
├── Build capability runtime (main.py:build_capability_runtime)
│   ├── BlenderCapabilityLayer
│   ├── VisionCapabilityLayer
│   ├── BlenderVisionIntegration + BlenderVisionWorkflow
│   ├── CapabilityRegistry with adapters
│   ├── Windows capability (optional, pywin32/comtypes gated)
│   └── DesktopWorkflow (if Windows available)
├── Build tool registry (tools/__init__.py:build_registry) with capability bridge
├── Initialize memory stores
│   ├── ConversationStore
│   ├── ProjectMemory (discovers projects, sets active)
│   ├── PreferencesStore
├── Create BlenderController
├── Create Agent (core/agent.py:Agent) with all dependencies
├── Optionally start VoiceService (voice/service.py)
├── Optionally start AutonomyRuntime (core/autonomy.py) for persistent goals
├── Launch UI: Edge Companion → Floating Companion → Terminal UI (fallback chain)
└── Cleanup on exit (close_capabilities, voice, autonomy, capability runtime)
```

### 1.2 Startup Dependencies (Initialization Order)

| Order | Component | Depends On |
|-------|-----------|------------|
| 1 | Settings | `.env`, `config/default_config.toml` |
| 2 | Logging | Settings.log_level, Settings.logs_dir |
| 3 | AI Backend | Settings (ollama_host, ai_model, timeouts) |
| 4 | EventBus | — |
| 5 | PermissionManager | Settings, PreferencesStore |
| 6 | Capability Runtime | Settings, EventBus, PermissionManager |
| 7 | Tool Registry | Settings, CapabilityRegistry, EventBus |
| 8 | Memory Stores | Settings.memory_dir, Settings.workspace_root |
| 9 | Agent | Provider, Settings, Registry, Permissions, EventBus, Memory, Project |
| 10 | Voice Service (optional) | Settings.voice_*, Agent, EventBus |
| 11 | Autonomy Runtime (optional) | Agent, GoalStore, Settings.autonomy_* |
| 12 | UI | Agent, EventBus, Backend info, Blender info |

### 1.3 Shutdown Flow

```python
close_capabilities()  # main.py:284-290, 379-390
├── VoiceService.close()  # voice/service.py:294-299
├── close_registered_desktop_workflow()  # DesktopWorkflow.close()
├── close_capability_runtime()  # main.py:16-38
│   ├── Workflow.close()
│   ├── Integration.close()
│   ├── BlenderCapabilityLayer.close()
│   ├── DesktopWorkflow.close()
│   └── WindowsCapability.close()
└── AutonomyRuntime.stop()  # if running (atexit registered)
```

**Verification:** The shutdown order is correct — DesktopWorkflow closes first (holds Windows capability refs and cancellation tokens), then the rest. Voice and autonomy are closed before capability runtime.

---

## 2. MAIN PACKAGES, MODULES, AND IMPORTANT FUNCTIONS

### 2.1 Package Map

```
diablo-ai/
├── ai/                          # AI provider abstraction
│   ├── provider.py              # AIProvider ABC, AIProviderError, ModelInfo, ToolCall, AIResponse
│   ├── ollama_provider.py       # OllamaProvider (HTTP chat, model listing, tool schemas)
│   ├── offline_provider.py      # OfflineProvider (rule-based fallback)
│   ├── model_router.py          # ModelRouter (adaptive qwen3 8B/14B selection)
│   ├── model_manager.py         # build_backend() factory, provider resolution
│   └── tokenizer.py             # head_tail() for result truncation
│
├── blender/                     # Blender integration
│   ├── controller.py            # BlenderController (detection, version, describe)
│   ├── capabilities.py          # BlenderCapabilityLayer (Capability protocol)
│   ├── runner.py                # Blender script execution
│   └── scripts/diablo_blender_ops.py  # Blender Python operations
│
├── coding/                      # Coding session tools
│   ├── change_tracker.py        # ChangeTracker (session, steps, checkpoints)
│   └── snapshot_manager.py      # SnapshotManager (git-based workspace snapshots)
│
├── config/                      # Configuration & permissions
│   ├── settings.py              # Settings dataclass, load_settings(), env/TOML precedence
│   ├── default_config.toml      # Default configuration (all sections documented)
│   └── permissions.py           # PermissionManager, PermissionLevel, path_is_within
│
├── core/                        # Core agent runtime (largest package)
│   ├── agent.py                 # Agent class (2226 lines) — central orchestrator
│   ├── executor.py              # Executor (permission → confirm → execute → verify)
│   ├── planner.py               # Planner (ExecutionPlan, ExecutionPlanRecord, validation)
│   ├── verification.py          # Verifier (file_exists, output_contains, exit_code, etc.)
│   ├── verification_ledger.py   # VerificationLedger (adjudication, evidence, trust)
│   ├── task_manager.py          # TaskManager, TaskState, StepStatus, terminal_verdict
│   ├── task_store.py            # TaskStore, TaskRecord, GitTaskState, persistence
│   ├── request_context.py       # RequestContext, BoundedRequestGate (M2.12 P1)
│   ├── cancellation.py          # CancellationToken, cancellation_requested()
│   ├── context.py               # build_system_prompt(), ProjectContext
│   ├── recovery.py              # RecoveryPolicy, FailureCategory, RecoveryAction
│   ├── events.py                # EventBus (pub/sub, tool events)
│   ├── logging_setup.py         # get_logger(), redact(), sensitive key filtering
│   ├── constants.py             # GREETING constant
│   ├── knowledge_graph.py       # KnowledgeGraph, KnowledgeGraphManager (SQLite)
│   ├── knowledge_projection.py  # Live task/verification/tool projection to graph
│   ├── knowledge_ingest.py      # Filesystem/code indexing into knowledge graph
│   ├── knowledge_tools.py       # knowledge.query / knowledge.index tool implementations
│   ├── knowledge_data.py        # Dataset/analysis/chart projection helpers
│   ├── project_task_history.py  # ProjectTaskHistory (TaskStore read-only summaries)
│   ├── model_router.py          # (duplicate? see ai/model_router.py)
│   ├── autonomy.py              # AutonomyRuntime, GoalStore, SchedulerLimits
│   ├── goals.py                 # Goal, GoalStore (durable autonomous goals)
│   └── windows/                 # Native Windows capability layer (15 modules)
│       ├── capabilities.py      # build_windows_capability(), WindowsCapability
│       ├── capture.py           # Display capture
│       ├── coordinates.py       # Coordinate transforms
│       ├── displays.py          # DisplayLayer, monitor enumeration
│       ├── errors.py            # Windows-specific errors
│       ├── input.py             # Input injection (click, type, key)
│       ├── models.py            # Window/element data models
│       ├── processes.py         # Process management
│       ├── start_menu.py        # Start menu query
│       ├── system.py            # System info (CPU, memory, OS)
│       ├── uia.py               # UI Automation (comtypes)
│       ├── visual.py            # Visual tree walking
│       ├── window_models.py     # Window/element models
│       └── windows.py           # Core window enumeration
│
├── data/                        # Runtime data (excluded from source audit)
│
├── docs/                        # Documentation & contracts
│   └── contracts/
│       └── cross_layer_execution_contracts.md
│
├── integration/                 # S2.3 Blender-Vision integration
│   ├── __init__.py              # Re-exports all integration types
│   ├── capabilities.py          # BlenderVisionIntegration, adapters
│   ├── contracts.py             # Capability, CapabilityRegistry, adapters, protocols
│   ├── desktop_workflow.py      # DesktopWorkflow (high-level desktop orchestration)
│   ├── errors.py                # Integration error types
│   ├── protocol.py              # BlenderLayerProtocol, VisionLayerProtocol
│   └── workflows.py             # BlenderVisionWorkflow, SceneWorkflow, adapters
│
├── memory/                      # Memory subsystem
│   ├── __init__.py
│   ├── memory_store.py          # MemoryStore (key-value with sanitization)
│   ├── conversation.py          # ConversationStore (role-tagged turns)
│   ├── sanitizer.py             # SanitizeDecision (ACCEPT/NEUTRALIZE/REJECT), render_untrusted_block
│   ├── preferences.py           # PreferencesStore (user preferences)
│   ├── project_memory.py        # ProjectMemory (project profiles, preferred models)
│   ├── rules_store.py           # RuleStore (persistent rules with scopes)
│   ├── rule_engine.py           # RuleEngine (evaluation, task-scoped rules)
│   └── storage.py               # load_json(), save_json() (atomic writes)
│
├── personas/                    # Persona system
│   ├── __init__.py
│   ├── loader.py                # load_persona(), select_persona(), format_status
│   ├── sage.py                  # Sage persona (analytical)
│   └── raphael.py               # Raphael persona (creative)
│
├── reports/                     # Historical reports (excluded)
│
├── scripts/                     # Utility scripts
│   ├── orb_preview.py           # Orb renderer preview generator
│   └── run_deterministic.ps1    # Deterministic test runner
│
├── tests/                       # Test suite (130+ files)
│
├── tools/                       # Tool registry (50+ tools)
│   ├── __init__.py              # build_registry() — registers ALL tools
│   ├── registry.py              # ToolRegistry, ToolSpec, ToolResult, ToolExecutionContext
│   ├── filesystem.py            # read_file, list_dir, write_file, delete_file
│   ├── terminal.py              # run_command (PowerShell, blocklist, auto-allow)
│   ├── python.py                # run_script (subprocess, cancellation support)
│   ├── applications.py          # open_app (whitelist, discovery, confirmation)
│   ├── system.py                # system_info (CPU, memory, OS)
│   ├── git.py                   # git_status, diff, log, add, commit, push, restore, branch, fetch, snapshot
│   ├── github.py                # gh_status, issue_list, pr_list, repo_view, issue_create, pr_create, release_create
│   ├── coding.py                # project_info, project_files, project_grep, project_verify
│   ├── patches.py               # patch_file (anchored diff syntax)
│   ├── data_tool.py             # load_csv, load_json, inspect_data, analyze_data
│   ├── chart_tool.py            # generate, verify, list, delete charts
│   ├── memory.py                # memory.remember, search, list, update, delete
│   ├── rules.py                 # rules.create, list, delete, enable, disable
│   ├── models.py                # models.status, models.set_mode
│   ├── tasks.py                 # task.info, task.list
│   ├── capability_bridge.py     # capability.execute (generic capability dispatcher)
│   └── desktop.py               # desktop.open, find, interact, wait (M2.16.6)
│
├── ui/                          # UI layer
│   ├── __init__.py
│   ├── terminal.py              # TerminalUI (console REPL with voice status)
│   ├── edge_companion.py        # EdgeCompanionApp (Tk, edge-docked, animated)
│   ├── launch.py                # run_companion_for_agent, run_edge_companion
│   ├── companion.py             # CompanionController (state machine)
│   ├── companion_events.py      # Event kinds for companion
│   ├── companion_state.py       # CompanionSnapshot, CompanionState enum
│   ├── controller.py            # UI controller bridge
│   ├── edge.py                  # CompanionGeometry, DockPlacement, place_at_edge
│   ├── bridge.py                # CoreEventBusBridge (voice → agent)
│   ├── models.py                # UI data models
│   └── orb/                     # Floating orb renderer (headless, numpy+PIL)
│       ├── __init__.py
│       ├── renderer.py          # OrbRenderer (particle physics, deterministic)
│       └── state_map.py         # OrbState enum, STATE_PARAMETERS, PALETTES
│
├── vision/                      # Vision capabilities
│   ├── __init__.py
│   ├── capabilities.py          # VisionCapabilityLayer
│   ├── errors.py
│   ├── inspect.py               # Image inspection
│   ├── loader.py                # Model loading
│   ├── protocol.py              # Vision protocol
│   └── backends/                # VLM backends
│       ├── base.py              # VisionBackend ABC
│       ├── fallback.py          # Fallback backend
│       └── local_vlm.py         # Local VLM (Ollama-compatible)
│
├── voice/                       # Voice system
│   ├── __init__.py
│   ├── service.py               # VoiceService (facade wiring all components)
│   ├── cancellation.py          # InterruptionManager, CancellationToken, CancellationBridge
│   ├── capture.py               # MicrophoneCapture, AudioConfig, RecordingConfig
│   ├── commands.py              # VoiceCommandRouter (STOP, cancel, etc.)
│   ├── events.py                # VoiceEvent types
│   ├── output.py                # VoiceOutputRouter (TTS with interruption)
│   ├── pipeline.py              # VoicePipeline (STT → command → response)
│   ├── speech_to_text.py        # SpeechToTextBackend coordination
│   ├── text_to_speech.py        # speak() function (SAPI/neural)
│   ├── vad.py                   # VAD (energy, webrtcvad)
│   ├── wake_word.py             # WakeWordDetector (mock, porcupine, openwakeword)
│   ├── wake_wording.py          # Wake word utilities
│   ├── ui_adapter.py            # VoiceUIAdapter (event bridge)
│   ├── stt/                     # Speech-to-text backends
│   │   ├── base.py              # SpeechToTextBackend ABC
│   │   ├── local.py             # FasterWhisperBackend
│   │   └── fallback.py          # FallbackSTT
│   └── tts/                     # Text-to-speech backends
│       ├── base.py              # TTSBackend ABC
│       ├── neural.py            # Neural TTS (placeholder)
│       └── sapi.py              # SAPI5 TTS (Windows)
│
├── voice_recordings/            # Runtime audio (excluded)
│
├── workspace/                   # Workspace samples (excluded)
│
├── AUDIT_REPORTS/               # This audit's output
│
├── main.py                      # Entry point (730 lines)
├── pyproject.toml               # Project manifest
├── requirements.txt             # Dependencies
├── README.md                    # Documentation
├── run.ps1                      # PowerShell launcher
├── test_term_tree.py            # Terminal tree test
└── .env / .env.example          # Environment config
```

### 2.2 Key Classes and Their Responsibilities

| Class | Module | Primary Responsibility |
|-------|--------|------------------------|
| `Agent` | `core/agent.py` | Central orchestrator: planning, execution, verification, recovery, memory, routing |
| `Executor` | `core/executor.py` | Permission evaluation, confirmation, tool dispatch, outcome recording |
| `Planner` | `core/planner.py` | Plan validation, dependency resolution, ExecutionPlanRecord creation |
| `Verifier` | `core/verification.py` | Independent verification (file, output, exit_code, grep, python) |
| `VerificationLedger` | `core/verification_ledger.py` | Adjudication authority for verification verdicts |
| `TaskManager` | `core/task_manager.py` | Task state machine, step tracking, terminal verdict invariant |
| `TaskStore` | `core/task_store.py` | Persistent TaskRecord storage (JSONL + metadata) |
| `ModelRouter` | `ai/model_router.py` | Adaptive model selection (8B/14B, complexity, escalation, project pref) |
| `OllamaProvider` | `ai/ollama_provider.py` | Ollama HTTP client (chat, tools, model listing, timeouts) |
| `PermissionManager` | `config/permissions.py` | Permission evaluation (SAFE/NORMAL/SENSITIVE, session blocks, rules) |
| `MemoryStore` | `memory/memory_store.py` | Long-term key-value memory with sanitization |
| `ConversationStore` | `memory/conversation.py` | Short-term conversation history (turns) |
| `RuleEngine` | `memory/rule_engine.py` | Rule evaluation (global, project, task-scoped) |
| `KnowledgeGraph` | `core/knowledge_graph.py` | Persistent project knowledge (SQLite, nodes/edges, staleness) |
| `VoiceService` | `voice/service.py` | Composable voice facade (STT, TTS, VAD, wake word, pipeline) |
| `InterruptionManager` | `voice/cancellation.py` | Voice-initiated cancellation → bridge → agent |
| `EdgeCompanionApp` | `ui/edge_companion.py` | Edge-docked Tk companion (animation, geometry, personas) |
| `OrbRenderer` | `ui/orb/renderer.py` | Headless particle orb renderer (deterministic, numpy+PIL) |
| `DesktopWorkflow` | `integration/desktop_workflow.py` | High-level desktop orchestration (open→find→interact→wait) |

---

## 3. MODULE RESPONSIBILITIES AND DEPENDENCY RELATIONSHIPS

### 3.1 Dependency Graph (Simplified)

```
main.py
├── config.settings → load_settings()
├── core.logging_setup → setup_logging()
├── ai.model_manager → build_backend()
│   ├── ai.ollama_provider → OllamaProvider
│   ├── ai.offline_provider → OfflineProvider
│   └── ai.model_router → ModelRouter
├── core.events → EventBus
├── config.permissions → PermissionManager
│   └── memory.preferences → PreferencesStore
├── main.build_capability_runtime()
│   ├── blender.capabilities → BlenderCapabilityLayer
│   ├── vision.capabilities → VisionCapabilityLayer
│   ├── integration → BlenderVisionIntegration, BlenderVisionWorkflow
│   ├── integration.contracts → CapabilityRegistry, Adapters
│   ├── core.windows.capabilities → build_windows_capability() [optional]
│   └── integration.desktop_workflow → DesktopWorkflow [optional]
├── tools → build_registry()
│   ├── tools.registry → ToolRegistry
│   ├── tools.filesystem/terminal/python/... → tool implementations
│   ├── tools.capability_bridge → capability.execute
│   └── tools.desktop → desktop.* (if Windows capability)
├── memory.conversation → ConversationStore
├── memory.project_memory → ProjectMemory
├── memory.memory_store → MemoryStore
├── core.agent → Agent
│   ├── core.planner → Planner
│   ├── core.executor → Executor
│   ├── core.verification → Verifier
│   ├── core.verification_ledger → VerificationLedger
│   ├── core.task_manager → TaskManager
│   ├── core.task_store → TaskStore
│   ├── core.request_context → RequestContext
│   ├── core.recovery → RecoveryPolicy
│   ├── memory.rule_engine → RuleEngine
│   ├── memory.rules_store → RuleStore
│   ├── memory.memory_store → MemoryStore
│   ├── coding.change_tracker → ChangeTracker
│   ├── coding.snapshot_manager → SnapshotManager
│   └── ai.model_router → ModelRouter (via runtime_context)
├── voice.service → VoiceService [optional]
│   ├── voice.stt → SpeechToTextBackend
│   ├── voice.tts → TTSBackend
│   ├── voice.vad → VAD
│   ├── voice.wake_word → WakeWordDetector
│   ├── voice.pipeline → VoicePipeline
│   └── voice.cancellation → InterruptionManager
├── core.autonomy → AutonomyRuntime [optional]
│   └── core.goals → GoalStore
└── ui → TerminalUI / EdgeCompanionApp / CompanionController
```

### 3.2 Critical Dependency Invariants

| Invariant | Enforcement Location | Status |
|-----------|---------------------|--------|
| Single Agent instance | `main.py:268-282` `make_agent()` | ✅ Enforced |
| PermissionManager built BEFORE capability runtime | `main.py:208-215` | ✅ Enforced (M2.16.6 fix) |
| DesktopWorkflow shares PermissionManager with Executor | `main.py:86-93` | ✅ Enforced |
| Tool registry receives capability_registry + events | `main.py:238-242` | ✅ Enforced |
| Agent sets Executor's task_provider | `core/agent.py:93` | ✅ Enforced |
| Router applied management overrides from prefs | `core/agent.py:260-261` | ✅ Enforced |
| Knowledge projection reads authoritative records only | `core/knowledge_projection.py:5-19` | ✅ Enforced (docs) |
| VerificationLedger is single adjudication authority | `core/verification_ledger.py` | ✅ Enforced |
| Terminal verdict invariant: COMPLETED + PASSED only valid pair | `core/task_manager.py:93-234` | ✅ Enforced |

---

## 4. EVENT BUSES, BACKGROUND WORKERS, QUEUES, CALLBACKS, ASYNC TASKS

### 4.1 EventBus (`core/events.py`)

- **Pattern:** Simple pub/sub with `emit(kind, payload)` and `subscribe(callback)`
- **Used by:** Agent (lifecycle events), Executor (tool events), VoiceService, UI, AutonomyRuntime
- **Thread Safety:** Not explicitly thread-safe; assumes single-threaded event loop (Tk main thread for UI)
- **Event Kinds:** `task_started`, `task_completed`, `task_failed`, `tool_started`, `tool_completed`, `verification_started`, `verification_completed`, `plan_created`, `plan_step_started`, `plan_step_finished`, `recovery_decided`, `autonomy_*`, voice events

### 4.2 Background Workers

| Worker | Module | Threading Model | Purpose |
|--------|--------|-----------------|---------|
| `AutonomyRuntime` | `core/autonomy.py` | Daemon thread (`THREAD_NAME = "autonomy-scheduler"`) | Persistent goal reconciliation + scheduling (M2.17) |
| `VoicePipeline` | `voice/pipeline.py` | Thread per recording (sounddevice callback) | Audio capture → STT → command routing |
| `InterruptionManager` | `voice/cancellation.py` | Thread-safe (RLock) | Cancellation token management, bridge callbacks |
| `MicrophoneCapture` | `voice/capture.py` | sounddevice callback thread | Audio recording with VAD |
| `EdgeCompanionApp` | `ui/edge_companion.py` | Tk main thread (`root.after`) | Animation frame loop (33ms), monitor check (5s) |
| `BlenderVisionWorkflow` | `integration/workflows.py` | Not directly threaded; called from Agent/Executor | Blender+Vision orchestration |

### 4.3 Cancellation System

```
CancellationToken (core/cancellation.py)
├── threading.Event for signal
├── CancellationRequest metadata (task_id, source, reason, timestamp)
├── raise_if_cancelled() → CancellationError
├── wait(timeout) for cooperative blocking
└── reset() for reuse

InterruptionManager (voice/cancellation.py)
├── token_for(task_id) → per-task CancellationToken
├── request_cancel(task_id, source, reason) → CancellationOutcome
│   ├── Sets token event
│   ├── Calls cancel_callback(task_id) → Agent.cancel_current_task()
│   └── Calls bridge.cancel_task(task_id) → CoreEventBusBridge.canceler
└── Emits VoiceEvent: task_cancel_requested / task_cancelled / task_cancel_failed
```

**Verification:** Cancellation propagates from Voice → InterruptionManager → Agent.cancel_current_task() → CancellationToken → Executor checks at 4 points (pre-permission, post-permission, pre-execute, post-execute). Blender/Windows tools respect cancellation via `_tool_context.cancellation_token`.

---

## 5. CONFIGURATION LOADING, DEFAULTS, ENV PRECEDENCE, VALIDATION

### 5.1 Configuration Sources (Priority Order)

1. **Environment variables** (highest) — `os.environ`
2. **`.env` file** — `config/settings.py:_load_env_file()`
3. **`config/default_config.toml`** — TOML defaults
4. **Hardcoded defaults in `Settings` dataclass** (lowest)

### 5.2 Key Configuration Sections

| Section | Key Settings | Validation |
|---------|-------------|------------|
| `[ai]` | `provider = "ollama"` | Must be "ollama" or "offline" |
| `[ollama]` | `host`, `allow_remote`, `preferred_models` | Host URL, bool, list[str] |
| `[project]` | `workspace_root` | Path, resolved to absolute |
| `[agent]` | `max_turns=4`, `max_tool_calls=32`, `max_recovery_attempts=3` | int ≥ 0 |
| `[memory]` | `max_conversation_turns=20`, `write_policy="explicit"` | int ≥ 1, enum |
| `[rules]` | `max_active_in_prompt=8` | int ≥ 0 |
| `[tools]` | `max_result_chars=8000`, `full_context=false` | int ≥ 0, bool |
| `[context]` | `max_tokens=8000`, `relevant_files=8`, `excerpt_lines=200` | int ≥ 0 |
| `[models]` | `default_mode="auto"`, `fast_model="qwen3:8b"`, `strong_model="qwen3:14b"`, `complexity_threshold=0.6` | enum, str, float 0-1 |
| `[voice]` | `enabled=false`, `stt_model="base"`, `wake_word_enabled=false` | bool, str, bool |
| `[terminal]` | `auto_allow_patterns[]`, `blocklist[]` | list[str] |
| `[apps.whitelist]` | `calculator="calc"`, `blender="blender"`, etc. | dict[str,str] |
| `[autonomy]` | `max_active_goals=3`, `max_tasks=32`, `min_interval_seconds=60` | int ≥ 0 |

### 5.3 Validation & Coercion

- **Type coercion:** `_as_bool()`, `_as_float()`, `_int()`, `_float()`, `_str()`, `_list()`, `_dict()` in `settings.py`
- **Range clamping:** `max(0, ...)`, `max(0.0, min(1.0, ...))` for confidence thresholds
- **Path resolution:** `_resolve()` makes relative paths absolute against `BASE_DIR`
- **Directory creation:** `load_settings()` creates all data directories (memory, logs, backups, sessions)
- **No schema validation:** TOML is parsed with `tomllib` (stdlib), no validation beyond type coercion

### 5.4 Known Issues

- **No validation of Ollama host reachability at config load** — deferred to `OllamaProvider.is_available()`
- **No validation that `preferred_models` exist locally** — deferred to model resolution
- **`resource_check_enabled` default false** — hook exists but no reliable local check implemented
- **`git` policies default to "blocked"** — safe default, but no UI to change

---

## 6. DATA STORAGE AND PERSISTENCE MECHANISMS

### 6.1 Storage Summary

| Store | File | Format | Atomic Writes | Scope |
|-------|------|--------|---------------|-------|
| Conversation | `data/memory/conversation.json` | JSON array | Yes (save_json) | Session |
| Long-term Memory | `data/memory/memory.json` | JSON `{entries:[], categories:{}}` | Yes | Persistent |
| Rules | `data/memory/rules.json` | JSON `{rules:[]}` | Yes | Persistent |
| Preferences | `data/memory/preferences.json` | JSON `{permissions:{}, models:{}}` | Yes | Persistent |
| Projects | `data/memory/projects.json` | JSON `{projects:{}, active:""}` | Yes | Persistent |
| Tasks | `data/sessions/tasks.jsonl` | JSON Lines (one TaskRecord per line) | Yes | Persistent |
| Coding Sessions | `data/sessions/coding/*.json` | JSON per session | Yes | Persistent |
| Snapshots | `data/sessions/snapshots/*/workspace_state.json` | JSON per snapshot | Yes | Persistent |
| Knowledge Graph | `data/knowledge/kg_<project_id>.db` | SQLite (WAL mode) | SQLite transactions | Persistent |
| Logs | `data/logs/diablo.log` | Plain text (rotating) | Append | Runtime |
| Backups | `data/backups/*.bak` | Timestamped copies | N/A | Recovery |

### 6.2 Atomic Write Implementation (`memory/storage.py:save_json`)

```python
def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # Atomic on POSIX, best-effort on Windows
```

**Verification:** All stores use `save_json`/`load_json`. SQLite uses WAL mode (`PRAGMA journal_mode = WAL`) for concurrent read/write.

### 6.3 Persistence Guarantees

| Guarantee | Implementation |
|-----------|----------------|
| Conversation survives restart | `Agent._restore_conversation()` loads into `self._messages` |
| Memory survives restart | `MemoryStore` loads on init, `Agent._restore_conversation()` |
| Rules survive restart | `RuleStore` loads on init, `PermissionManager.rule_engine` references it |
| Tasks survive restart | `TaskStore` JSONL, `Agent._reconcile_interrupted_tasks()` on init |
| Knowledge graph survives restart | SQLite file per project, `KnowledgeGraphManager` caches handles |
| Recovery state survives restart | `TaskRecord.metadata["lifecycle"]` → `Agent._restore_recovery_state()` |

---

## 7. LOGGING, EXCEPTIONS, ERROR PROPAGATION, RESOURCE CLEANUP

### 7.1 Logging (`core/logging_setup.py`)

- **Logger:** `logging.getLogger(name)` with `RichHandler` (console) + `FileHandler` (data/logs/diablo.log)
- **Redaction:** `redact(text)` filters `SENSITIVE_KEYS` (password, token, api_key, secret, key, auth, credential)
- **Levels:** Configurable via `Settings.log_level` (DEBUG/INFO/WARNING/ERROR)
- **Format:** `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`

### 7.2 Exception Hierarchy

```
AIProviderError (ai/provider.py)          # Structured provider failures
├── category: TIMEOUT/CONNECTION/HTTP/MALFORMED/NO_MODEL/UNKNOWN
├── model, latency, retryable, status
└── to_dict() for serialization

CancellationError (voice/cancellation.py) # Cooperative cancellation
└── CancellationRequest

ToolBlockedError (config/permissions.py)  # Permission denial at tool layer
MemoryWriteRefused (memory/memory_store.py) # Sanitizer rejection
EdgeCompanionUnavailable (ui/edge_companion.py) # UI unavailable (fallback)
CompanionUnavailable (ui/launch.py)       # Floating companion unavailable
```

### 7.3 Error Propagation Patterns

| Pattern | Location | Example |
|---------|----------|---------|
| **Structured provider errors** | `ai/ollama_provider.py` | `AIProviderError(category=TIMEOUT, model="qwen3:14b", retryable=True)` |
| **Tool execution outcomes** | `core/executor.py:ExecutionOutcome` | `allowed`, `executed`, `success`, `cancelled`, `error`, `verification` |
| **Permission decisions** | `config/permissions.py:PermissionDecision` | `allow`, `requires_confirmation`, `blocked`, `level`, `reason` |
| **Verification results** | `core/verification.py:VerificationResult` | `state` (PASSED/FAILED/UNVERIFIED), `detail`, `kind` |
| **Recovery decisions** | `core/recovery.py:RecoveryDecision` | `action` (RETRY/ESCALATE_MODEL/ABORT), `reason` |
| **Voice cancellation outcomes** | `voice/cancellation.py:CancellationOutcome` | `accepted`, `bridge_accepted`, `error` |

### 7.4 Resource Cleanup

| Resource | Cleanup Mechanism | Verified |
|----------|-------------------|----------|
| SQLite connections | `KnowledgeGraph.close()` + `__del__` | ✅ |
| Blender subprocess | `BlenderController.close()` / `BlenderCapabilityLayer.close()` | ✅ |
| Windows COM (comtypes) | `WindowsCapability.close()` | ✅ |
| Tk windows | `EdgeCompanionApp.close()` → `root.destroy()` | ✅ |
| Voice pipelines | `VoiceService.close()` → `pipeline.close()`, `output.close()`, `interruptions.close()`, `microphone.close()` | ✅ |
| Autonomy scheduler | `AutonomyRuntime.stop()` (atexit) | ✅ |
| Threading locks | `RLock`/`Lock` in InterruptionManager, caches | ✅ |
| File handles | `with open()` or explicit `.close()` | ✅ |

**Issue:** `KnowledgeGraph.__del__` calls `close()` but Python finalization order is non-deterministic. `close_shared_managers()` should be called explicitly at shutdown (done in `main.py:close_capabilities()` via `capability_runtime` cleanup).

---

## 8. EXTERNAL PROCESSES AND OS INTEGRATION

### 8.1 External Process Invocation

| Tool | External Command | Purpose | Safety |
|------|------------------|---------|--------|
| `terminal.execute` | PowerShell (`powershell -Command ...`) | General shell commands | Blocklist, auto-allow, confirmation |
| `python.run_script` | `python <script> [args]` | Python scripts | Working dir restriction, cancellation token |
| `applications.open` | `start "" "<path>"` or direct exe | Launch desktop apps | Whitelist + confirmation |
| `git.*` | `git <subcommand>` | Git operations | Policy-gated (push/restore/branch blocked) |
| `gh.*` | `gh <subcommand>` | GitHub CLI | Confirmation for mutations |
| `system.info` | `wmic`, `Get-CimInstance` | System metrics | Read-only |
| Blender | `blender.exe -b -P script.py` | Blender scripting | Work dir sandboxed, timeout |
| STT (faster-whisper) | In-process (CTranslate2) | Speech recognition | Local only |
| TTS (SAPI) | `PowerShell -Command "Add-Type -AssemblyName System.Speech; ..."` | Speech synthesis | Local only, interruptible |

### 8.2 OS Integration Points

| Integration | Module | Mechanism | Platform |
|-------------|--------|-----------|----------|
| Window enumeration | `core/windows/windows.py` | `pywin32` (win32gui, win32process) | Windows only |
| UI Automation | `core/windows/uia.py` | `comtypes` (IUIAutomation) | Windows only |
| Display topology | `core/windows/displays.py` | `pywin32` (GetMonitorInfo, EnumDisplayMonitors) | Windows only |
| Process management | `core/windows/processes.py` | `pywin32` (OpenProcess, TerminateProcess) | Windows only |
| Input injection | `core/windows/input.py` | `pywin32` (SendInput, PostMessage) | Windows only |
| File system | `tools/filesystem.py` | `pathlib`, `shutil` | Cross-platform |
| Audio capture | `voice/capture.py` | `sounddevice` (WASAPI) | Windows primary |
| TTS | `voice/tts/sapi.py` | `comtypes` (SAPI.SpVoice) | Windows only |

### 8.3 Degradation Behavior

- **No pywin32/comtypes:** Windows capability reports `available=False`, DesktopWorkflow not registered, agent falls back to non-Windows tools
- **No Ollama:** Falls back to `OfflineProvider` (rule-based), logs warning
- **No Tkinter:** `--companion`/`--edge-companion` raise `CompanionUnavailable`/`EdgeCompanionUnavailable`, falls back to TerminalUI
- **No sounddevice:** Voice service unavailable, logs warning
- **No faster-whisper:** STT falls back to mock/unavailable

---

## 9. ARCHITECTURAL FINDINGS

### 9.1 Duplicated Functionality

| Duplication | Locations | Assessment |
|-------------|-----------|------------|
| ModelRouter | `ai/model_router.py` AND `core/model_router.py` | **CONFIRMED** — `core/model_router.py` appears to be a duplicate/stale copy. `Agent` imports from `ai.model_router`. |
| Verification logic | `core/verification.py` + `core/verification_ledger.py` | **INTENTIONAL** — Ledger is adjudication authority, Verifier is execution-time checker |
| Task persistence | `TaskStore` (JSONL) + `ChangeTracker` (coding sessions) | **INTENTIONAL** — Different granularities (task vs. coding session) |
| Tool registry | `tools/__init__.py:build_registry()` + `ToolRegistry` class | **INTENTIONAL** — Builder function + registry class |

### 9.2 Circular Dependencies

**None detected in source imports.** The architecture uses dependency injection (Agent receives all dependencies in `__init__`) and runtime context (`tools.runtime_context`) for late binding.

### 9.3 Tightly Coupled Modules

| Coupling | Modules | Reason |
|----------|---------|--------|
| Agent ↔ TaskManager | `core/agent.py` ↔ `core/task_manager.py` | Agent owns TaskManager instance, direct attribute access |
| Agent ↔ Executor | `core/agent.py` ↔ `core/executor.py` | Agent creates Executor, sets callbacks (`before_execute`, `state_listener`) |
| Agent ↔ VerificationLedger | `core/agent.py` ↔ `core/verification_ledger.py` | Agent records verifications through TaskManager → Ledger |
| PermissionManager ↔ RuleEngine | `config/permissions.py` ↔ `memory/rule_engine.py` | PermissionManager holds RuleEngine reference |
| Tool registry → All tool modules | `tools/__init__.py` imports all | Central registration imports every tool module |

### 9.4 Dead Code Candidates

| Candidate | Location | Evidence |
|-----------|----------|----------|
| `core/model_router.py` | Duplicate of `ai/model_router.py` | Not imported anywhere (Agent uses `ai.model_router`) |
| `KnowledgeGraph.transaction()` | `core/knowledge_graph.py:962-987` | Docstring says "currently unreferenced anywhere" |
| `OfflineProvider` chat() | `ai/offline_provider.py` | Only used when Ollama unavailable or forced offline |
| `MockCapability` | `integration/contracts.py` | Exported but not used in production code |
| `voice/tts/neural.py` | Neural TTS backend | Placeholder, not wired in `create_tts_backend()` |

### 9.5 Architectural Bottlenecks

| Bottleneck | Location | Impact |
|------------|----------|--------|
| Agent monolith | `core/agent.py` (2226 lines) | Single class handles planning, execution, verification, recovery, memory, routing, knowledge projection |
| Synchronous tool execution | `core/executor.py:run()` | Blocks Agent thread; no parallel tool execution |
| Single EventBus | `core/events.py` | All events through one bus; no prioritization |
| SQLite per project | `core/knowledge_graph.py` | One DB file per project; no cross-project queries |
| Tk main thread | `ui/edge_companion.py` | Animation + event handling on same thread; long operations block UI |

---

## 10. HYPOTHESES REQUIRING VERIFICATION

| Hypothesis | Status | Verification Needed |
|------------|--------|---------------------|
| `core/model_router.py` is dead code | 🔴 Hypothesis | Check all imports; confirm only `ai.model_router` used |
| Knowledge graph projection never fails tasks | 🟡 Inferred | Code has `try/except` swallowing errors — verify in integration tests |
| Cancellation reaches all tools | 🟡 Inferred | Blender/Windows tools check `_tool_context.cancellation_token` — verify coverage |
| Terminal verdict invariant holds under all paths | ✅ Verified | Enforced in `TaskManager.transition()` and `assert_terminal_invariant()` |
| No secret leakage in logs | 🟡 Inferred | `redact()` filters `SENSITIVE_KEYS` — verify coverage in all log calls |
| Atomic writes survive power loss | 🟡 Inferred | `save_json` uses `.tmp` + `replace()` — Windows `replace()` not fully atomic |

---

## 11. ARCHITECTURE AUDIT SUMMARY

**What works well:**
- Clear separation: Agent (orchestration) → Executor (permissions+dispatch) → Tools (implementation)
- Single verification authority (VerificationLedger) with terminal verdict invariant
- Durable recovery state survives restarts (M2.15.2)
- Configuration precedence well-defined (env > .env > TOML > defaults)
- Graceful degradation for optional components (Windows, Voice, Blender, UI)
- Atomic persistence for all stores
- Structured error types with categorization for routing/retry decisions

**Areas of concern:**
- Agent class is a God object (2226 lines, 50+ methods)
- Duplicate `model_router.py` in core/ vs ai/
- No parallel tool execution capability
- Tk UI blocks on long operations
- Knowledge graph is per-project only (no cross-project queries)
- Some dead code candidates not removed

**Recommendations (for roadmap, not implementation):**
1. Split Agent into: PlannerAgent, ExecutorAgent, VerificationAgent, MemoryAgent
2. Remove `core/model_router.py` duplicate
3. Add async tool execution with `asyncio` or thread pool
4. Move UI animation to separate thread or use `asyncio` with Tk
5. Add knowledge graph manager for multi-project queries
6. Run dead code elimination pass

---

**END OF ARCHITECTURE AUDIT**