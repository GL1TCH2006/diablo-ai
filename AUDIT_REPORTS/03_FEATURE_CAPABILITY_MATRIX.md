# FEATURE CAPABILITY MATRIX — DIABLO PROJECT

**Project Root:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Audit Date:** 2026-10-25  
**Git Branch:** `reliability-batch-1`  
**Git HEAD:** `a74b53c`

---

## CLASSIFICATION LEGEND

| Classification | Definition |
|----------------|------------|
| **Implemented and Verified** | Source code exists, tests pass, behavior confirmed via source inspection + test evidence |
| **Implemented but Not Verified** | Source code exists, but no tests or tests not executed during audit |
| **Partially Implemented** | Core structure exists but key sub-features missing or incomplete |
| **Broken** | Source code exists but has known defects preventing correct operation |
| **Missing** | No source code implementation found |
| **Unknown** | Insufficient evidence to classify |

---

## 1. AI AGENT CAPABILITIES

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **User Input & Intent Interpretation** | Implemented and Verified | Agent.respond() processes input, extracts `%plan` goals, routes to planner | `core/agent.py:1510-1514`, `core/agent.py:2137` | Rule-based greeting only; no NLU pipeline |
| **Reasoning & Task Planning** | Implemented and Verified | Planner creates ExecutionPlan with dependency graph; validates cycles | `core/planner.py`, `core/agent.py:1557-1604` | Single-turn planning; no multi-turn replanning |
| **Tool Discovery & Selection** | Implemented and Verified | ModelRouter chooses model; Agent presents adaptive tool context | `ai/model_router.py`, `core/agent.py:1467-1508`, `tools/runtime_context.py` | Tool context selection heuristic only |
| **Tool Schemas & Argument Validation** | Implemented and Verified | ToolSpec.parameters (JSON Schema), Executor validates args dict | `tools/registry.py:25-32`, `core/executor.py:164-170` | No strict schema validation (uses dict) |
| **Multi-step Execution & State Tracking** | Implemented and Verified | TaskManager tracks steps, PlanStepRecord tracks plan steps, ChangeTracker for coding | `core/task_manager.py`, `core/agent.py:1936-1711`, `coding/change_tracker.py` | Sequential only; no parallel steps |
| **Result Validation & Completion Reporting** | Implemented and Verified | Verifier + VerificationLedger adjudicate; terminal verdict invariant enforced | `core/verification.py`, `core/verification_ledger.py`, `core/task_manager.py:93-234` | Verification only for executed tools |
| **Retry Policies, Timeouts, Error Recovery** | Implemented and Verified | RecoveryPolicy (retries, escalations), AIProviderError categories, Executor timeouts | `core/recovery.py`, `core/agent.py:1820-1934`, `ai/provider.py` | Retry only on FAILED steps; no custom policies |
| **Cancellation & Interruption** | Implemented and Verified | CancellationToken, InterruptionManager, voice STOP, Agent.cancel_current_task() | `core/cancellation.py`, `voice/cancellation.py`, `core/agent.py:267-295` | Cooperative only; no forced kill for all tools |
| **Human Approval & Permission Enforcement** | Implemented and Verified | PermissionManager (SAFE/NORMAL/SENSITIVE), session blocks, rules, confirmations | `config/permissions.py`, `core/executor.py:179-259` | No async approval (blocks executor thread) |

---

## 2. MODEL INFRASTRUCTURE

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **Ollama Connectivity & Model Discovery** | Implemented and Verified | OllamaProvider.is_available(), list_models() via /api/tags | `ai/ollama_provider.py:42-72` | No health check polling; single attempt |
| **Qwen 8B and 14B Configuration** | Implemented and Verified | Settings.models_fast/strong, ModelRouter identity verification | `config/settings.py:105-110`, `ai/model_router.py:115-123` | Assumes qwen3 naming; no generic model support |
| **Model Selection & Routing Rules** | Implemented and Verified | ModelRouter.choose() with modes: auto/force8b/force14b/project | `ai/model_router.py:159-198` | Complexity heuristic only; no learned routing |
| **Context Window & Generation Settings** | Implemented but Not Verified | OllamaProvider sends temperature=0.2, think=false; no context window management | `ai/ollama_provider.py:83-90` | Fixed temperature; no dynamic context sizing |
| **Model Loading/Unloading & Memory Behavior** | Missing | No model management; Ollama handles loading | — | No VRAM/RAM monitoring; resource_check_enabled hook only |
| **Timeout & Connection Error Handling** | Implemented and Verified | AIProviderError categories (TIMEOUT, CONNECTION, HTTP), retryable inference | `ai/provider.py:7-28`, `ai/ollama_provider.py:102-115` | 600s default timeout; no per-request override |
| **Cloud Provider Adapters** | Missing | Only Ollama and OfflineProvider implemented | `ai/model_manager.py:26-37` | No OpenAI, Anthropic, etc. adapters |
| **API Key Handling & Cost Controls** | Missing | No cloud providers → no API keys; no cost tracking | — | Local-only architecture |
| **Fallback Behavior & Unintended Cloud Calls** | Implemented and Verified | _offline_fallback() for all failures; allow_remote_ollama gates remote hosts | `ai/model_manager.py:36-39, 109-116` | Remote Ollama blocked by default |

---

## 3. MEMORY AND KNOWLEDGE

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **Conversation History** | Implemented and Verified | ConversationStore (JSON), max_turns cap, restored on Agent init | `memory/conversation.py`, `core/agent.py:702-770` | No summarization; raw turns only |
| **Long-term Key-Value Memory** | Implemented and Verified | MemoryStore with categories, search, sanitization, atomic writes | `memory/memory_store.py`, `memory/sanitizer.py` | No vector search; linear scan |
| **Rules & Preferences** | Implemented and Verified | RuleStore (scopes: global/project/task), PreferencesStore (permissions, models) | `memory/rules_store.py`, `memory/preferences.py`, `memory/rule_engine.py` | No rule conflict resolution beyond detection |
| **Project Memory & Context** | Implemented and Verified | ProjectMemory discovers projects, stores profiles, preferred models | `memory/project_memory.py`, `core/agent.py:1534-1547` | No cross-project learning |
| **Knowledge Graph (Persistent)** | Implemented and Verified | SQLite per project, nodes/edges, staleness, canonical keys | `core/knowledge_graph.py`, `core/knowledge_projection.py` | No SPARQL/Cypher; custom query API only |
| **Knowledge Ingestion (Code/Files)** | Implemented and Verified | knowledge_ingest indexes files, symbols, directories; knowledge.index tool | `core/knowledge_ingest.py`, `core/knowledge_tools.py` | Python-only symbol extraction |
| **Context Selection for Tasks** | Implemented and Verified | Agent._reference_sections() builds prompt with rules, memory, project, task history | `core/agent.py:1515-1547` | Fixed char limits; no relevance ranking |
| **Memory Timestamps, Provenance, Confidence** | Partially Implemented | Entries have created_at/updated_at; no confidence scores | `memory/memory_store.py:91-108` | No provenance tracking beyond category |
| **Conflict/Outdated Memory Handling** | Partially Implemented | Sanitizer rejects credentials; rules detect conflicts; stale nodes marked | `memory/sanitizer.py`, `memory/rules_store.py`, `core/knowledge_graph.py:668-712` | No automatic conflict resolution |
| **Trusted vs Untrusted Content Separation** | Implemented and Verified | render_untrusted_block() wraps memory; sanitizer neutralizes injection attempts | `memory/sanitizer.py:405-424`, `memory/memory_store.py:154-181` | Only at injection time; stored content not re-verified |

---

## 4. TOOLS AND INTEGRATIONS

| Tool Category | Tools | Classification | Evidence | Limitations |
|---------------|-------|----------------|----------|-------------|
| **Filesystem** | read_file, list_dir, write_file, delete_file | Implemented and Verified | `tools/filesystem.py` | Path containment via allowed_roots |
| **Terminal/Shell** | terminal.execute | Implemented and Verified | `tools/terminal.py` | Blocklist, auto-allow patterns, PowerShell only |
| **Python Execution** | python.run_script | Implemented and Verified | `tools/python.py` | Cancellation support via _tool_context |
| **Applications** | applications.open | Implemented and Verified | `tools/applications.py` | Whitelist + discovery; no interaction |
| **Git** | status, diff, log, add, commit, push, restore, branch, fetch, snapshot | Implemented and Verified | `tools/git.py` | Push/restore/branch gated by policy |
| **GitHub** | gh_status, issue_list, pr_list, repo_view, issue_create, pr_create, release_create | Implemented but Not Verified | `tools/github.py` | Requires gh CLI auth; no tests run |
| **Coding/Project** | project.info, project.files, project.grep, project.verify, code.patch_file | Implemented and Verified | `tools/coding.py`, `tools/patches.py` | Patch syntax custom; no LSP integration |
| **Data Analysis** | data.load_csv, load_json, inspect_data, analyze_data | Implemented and Verified | `tools/data_tool.py` | CSV/JSON only; pandas-dependent |
| **Charting** | chart.generate, verify, list, delete | Implemented and Verified | `tools/chart_tool.py` | Matplotlib backend; PNG output |
| **Memory Tools** | memory.remember, search, list, update, delete | Implemented and Verified | `tools/memory.py` | Delegates to MemoryStore |
| **Rules Tools** | rules.create, list, delete, enable, disable | Implemented and Verified | `tools/rules.py` | Delegates to RuleStore |
| **Model Tools** | models.status, models.set_mode | Implemented and Verified | `tools/models.py` | Delegates to ModelRouter |
| **Task Tools** | task.info, task.list | Implemented and Verified | `tools/tasks.py` | Delegates to TaskStore |
| **Knowledge Tools** | knowledge.query, knowledge.index | Implemented and Verified | `tools/__init__.py:930-1003`, `core/knowledge_tools.py` | Read-only query; index writes |
| **Desktop Automation** | desktop.open, find, interact, wait | Implemented but Not Verified | `tools/desktop.py`, `integration/desktop_workflow.py` | Requires Windows capability (pywin32+comtypes) |
| **Blender** | capability.execute (blender.*), BlenderVisionWorkflow | Implemented but Not Verified | `blender/`, `integration/` | Requires Blender installed |
| **Vision** | capability.execute (vision.*), local VLM | Implemented but Not Verified | `vision/`, `integration/` | Requires Ollama VLM model |

---

## 5. VOICE AND AUDIO

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **Voice Service Startup/Shutdown** | Implemented and Verified | VoiceService.start_listening(), close() wires all components | `voice/service.py:162-166, 294-299` | Optional (disabled by default) |
| **Microphone Selection & Config** | Implemented and Verified | MicrophoneCapture lists devices, config via settings | `voice/capture.py`, `voice/service.py:58, 123-131` | sounddevice only; no PulseAudio/PipeWire |
| **Speech-to-Text Backend** | Implemented and Verified | FasterWhisperBackend (local), FallbackSTT, mock | `voice/stt/local.py`, `voice/stt/fallback.py` | faster-whisper only; no cloud STT |
| **Audio Capture, Buffering, Cleanup** | Implemented and Verified | RecordingConfig (duration, silence_timeout, energy_threshold) | `voice/capture.py:1-80`, `voice/service.py:125-131` | VAD energy-based only; no webrtcvad by default |
| **Voice Command Routing** | Implemented and Verified | VoiceCommandRouter handles STOP, cancel, task submission | `voice/commands.py`, `voice/pipeline.py` | Limited vocabulary; no wake-word by default |
| **Text-to-Speech Backend** | Implemented and Verified | SAPI5 (Windows), Neural (placeholder), mock | `voice/tts/sapi.py`, `voice/tts/neural.py` | SAPI only on Windows; no cross-platform TTS |
| **TTS Interruption** | Implemented and Verified | VoiceOutputRouter.interruptible, InterruptionManager integration | `voice/output.py`, `voice/cancellation.py` | Cooperative; no audio buffer flush |
| **Agent Task Cancellation via Voice** | Implemented and Verified | InterruptionManager.request_cancel() → Agent.cancel_current_task() | `voice/cancellation.py:214-308`, `core/agent.py:267-295` | STOP button in UI also triggers |
| **Wake Word Implementation** | Partially Implemented | WakeWordDetector ABC with mock/porcupine/openwakeword; disabled by default | `voice/wake_word.py`, `voice/service.py:96` | Mock only in default config; real detectors optional |
| **UI Responsiveness During Audio** | Partially Implemented | VoicePipeline runs in threads; UI on Tk main thread | `voice/pipeline.py`, `ui/edge_companion.py` | No async audio processing in UI thread |
| **Privacy & Cloud Audio Behavior** | Implemented and Verified | All STT/TTS local; no cloud audio; microphone only active when listening | `voice/stt/local.py`, `voice/tts/sapi.py`, `config/settings.py:138-155` | No network audio transmission |

---

## 6. FLOATING DESKTOP UI

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **Edge-Docked Companion (Primary)** | Implemented and Verified | EdgeCompanionApp (Tk, borderless, topmost, translucent, animated) | `ui/edge_companion.py` | Windows only (win32 for placement) |
| **Floating Orb (Secondary)** | Implemented and Verified | OrbRenderer (headless, numpy+PIL, deterministic particle physics) | `ui/orb/renderer.py`, `ui/orb/state_map.py` | No window; renders to image only |
| **Window Creation, Positioning, Persistence** | Implemented and Verified | place_at_edge(), select_monitor(), redock() on monitor change | `ui/edge.py`, `ui/edge_companion.py:532-542` | Single monitor docking; no multi-monitor span |
| **Drag Handling & Mouse Events** | Missing | No drag support; click-to-activate only | `ui/edge_companion.py:809-819` | Fixed edge position |
| **Always-on-Top & Click-Through** | Implemented and Verified | -topmost, WS_EX_NOACTIVATE when sleeping, WS_EX_TOOLWINDOW | `ui/edge_companion.py:331-336, 121-165` | Click-through only when sleeping |
| **Display Scaling & Multi-Monitor** | Implemented and Verified | DisplayLayer reads scale_percent; geometry scaled | `core/windows/displays.py`, `ui/edge_companion.py:522-530` | Primary monitor fallback if pointer unavailable |
| **Auto-Hide Timers & State Transitions** | Implemented and Verified | CompanionController state machine (SLEEPING→AWAKENING→LISTENING→THINKING→EXECUTING→RETURNING) | `ui/companion_state.py`, `ui/companion.py` | No user-configurable timers |
| **Thread Safety & Event Loop** | Implemented and Verified | Tk main thread only; root.after frame loop (33ms); monitor check rate-limited (5s) | `ui/edge_companion.py:114-118, 904-909` | Long operations block UI |
| **UI ↔ Agent Communication** | Implemented and Verified | CompanionController.submit_task/cancel/activate/retract; Agent ↔ EventBus | `ui/companion.py`, `ui/companion_state.py`, `core/agent.py` | Synchronous submit; no streaming responses |
| **Error Reporting & Recovery** | Implemented and Verified | Frame errors caught/logged; window destroy → close(); duplicate launch refused | `ui/edge_companion.py:890-902, 954-982` | No automatic restart on crash |

---

## 7. TESTS AND RELIABILITY

| Test Category | Count | Classification | Evidence | Coverage |
|---------------|-------|----------------|----------|----------|
| **Unit Tests** | 50+ | Implemented and Verified | `tests/test_*.py` (memory, tools, agent, verification, etc.) | Core logic well-covered |
| **Integration Tests** | 20+ | Implemented and Verified | `tests/test_integration.py`, `tests/test_m21*_integration.py` | Multi-component flows |
| **Real-Model Tests** | 15+ | Implemented but Not Verified | `tests/test_m217_real_model_autonomy.py`, `tests/test_*_real*.py` | Require Ollama running; not run in audit |
| **Regression Tests** | 10+ | Implemented and Verified | `tests/test_companion_worker_regression.py`, `tests/test_orb_*_batch6.py` | Specific bug fixes |
| **UI Tests** | 10+ | Implemented but Not Verified | `tests/test_edge_companion_speech_probe.py`, `tests/test_floating_companion.py` | Require display; headless CI issues |
| **Windows Real Tests** | 10+ | Implemented but Not Verified | `tests/test_windows_real.py`, `tests/test_windows_uia_real.py` | Require Windows + pywin32/comtypes |
| **Blender Real Tests** | 5+ | Implemented but Not Verified | `tests/test_blender_real_integration.py` | Require Blender installed |
| **Cancellation Tests** | 10+ | Implemented and Verified | `tests/test_m2152_blender_cancellation.py`, `tests/test_voice_interruption.py` | Cancellation paths covered |
| **Security/Containment Tests** | 8+ | Implemented and Verified | `tests/test_filesystem_containment.py`, `tests/test_filesystem_delete_verification.py` | Path traversal, blocklist verified |
| **Configuration Tests** | 5+ | Implemented and Verified | `tests/test_s2_9_hardening.py`, `tests/test_phase10q_validation.py` | Config validation |
| **pytest Configuration** | — | Implemented and Verified | `pyproject.toml:37-38` (testpaths = ["tests"]) | No coverage, no parallel, no timeout default |

**Known Test Issues (from existing reports):**
- Orb renderer tests have flaky timing assertions
- Windows UIA tests fail on CI without display
- Real-model tests require manual Ollama setup
- Some tests marked `@pytest.mark.slow` or require specific hardware

---

## 8. SECURITY AND PERMISSIONS

| Capability | Classification | Evidence | Source Paths | Limitations |
|------------|----------------|----------|--------------|-------------|
| **Path Containment** | Implemented and Verified | path_is_within(), allowed_roots, filesystem level_for | `config/permissions.py:1-50`, `tools/__init__.py:37-40` | Symlinks not explicitly handled |
| **Permission Ladder (SAFE/NORMAL/SENSITIVE)** | Implemented and Verified | PermissionLevel enum, level_for callables, session blocks | `config/permissions.py:52-150` | No temporary elevation |
| **Destructive Command Blocklist** | Implemented and Verified | Terminal blocklist (format, shutdown, del, rm -rf, etc.) | `config/default_config.toml:91-113` | Pattern matching only; no semantic analysis |
| **Confirmation Gates** | Implemented and Verified | Executor requires_confirmation, confirmer callback, "always"/"block" options | `core/executor.py:211-259` | Blocks executor thread |
| **Rule Engine (Global/Project/Task)** | Implemented and Verified | RuleEngine evaluates conditions; PermissionManager integrates | `memory/rule_engine.py`, `config/permissions.py:152-250` | No priority conflicts beyond detection |
| **Secret Redaction in Logs** | Implemented and Verified | redact() filters SENSITIVE_KEYS (password, token, api_key, secret, etc.) | `core/logging_setup.py:1-80` | Heuristic; false positives/negatives possible |
| **Memory Sanitization** | Implemented and Verified | SanitizeDecision (ACCEPT/NEUTRALIZE/REJECT), credential detection, injection neutralization | `memory/sanitizer.py`, `memory/memory_store.py:184-205` | Rejects credentials; neutralizes control tokens |
| **Prompt Injection Defense** | Partially Implemented | render_untrusted_block() wraps memory; sanitizer neutralizes role labels, injection phrases | `memory/sanitizer.py:199-220, 405-424` | Only at injection; stored content not re-scanned |
| **Git Policy Enforcement** | Implemented and Verified | push/restore/branch policies (blocked/confirm), snapshot restore gated | `config/default_config.toml:62-65`, `tools/__init__.py:421-460` | No signed commits verification |
| **Resource Bounds** | Partially Implemented | max_turns, max_tool_calls, max_recovery_attempts, autonomy ceilings | `config/default_config.toml:18-27, 159-164` | No CPU/memory/time bounds on tool execution |

---

## 9. DEPENDENCIES AND HARDWARE

| Dependency | Required/Optional | Version | Purpose | Verified |
|------------|-------------------|---------|---------|----------|
| Python | Required | >=3.11 | Runtime | ✅ |
| rich | Required | >=13.7 | Terminal UI | ✅ |
| httpx | Required | >=0.28 | Ollama HTTP client | ✅ |
| Pillow | Required | >=10.0 | Orb renderer, image handling | ✅ |
| numpy | Required | >=1.24 | Orb renderer, knowledge graph | ✅ |
| matplotlib | Required | >=3.7 | Chart generation | ✅ |
| pywin32 | Optional (Windows) | >=306 | Windows capability layer | ⚠️ Optional |
| comtypes | Optional (Windows) | >=1.4.17 | UI Automation | ⚠️ Optional |
| faster-whisper | Optional (Voice) | >=1.0.0 | Local STT | ⚠️ Optional |
| sounddevice | Optional (Voice) | >=0.4.6 | Microphone capture | ⚠️ Optional |
| av | Optional (Voice) | <12.0.0 | Audio decoding | ⚠️ Optional |
| pytest | Test only | >=8.0 | Test runner | ✅ |
| Ollama | Runtime | — | LLM provider | ⚠️ External |
| Blender | Optional | 5.2+ | 3D integration | ⚠️ External |
| gh CLI | Optional | — | GitHub integration | ⚠️ External |

**Hardware Assumptions:**
- Windows 10/11 (primary target; Windows capability layer requires it)
- GPU optional (faster-whisper supports CPU via CTranslate2 int8)
- No explicit RAM/CPU minimums documented
- Microphone required for voice features
- Display required for UI companions

---

## 10. SUMMARY MATRIX

| Major Capability Area | Overall Classification | Key Gaps |
|----------------------|------------------------|----------|
| **Agent Core (Reasoning/Planning/Execution)** | Implemented and Verified | No parallel execution, no streaming, single-turn planning |
| **Model Infrastructure** | Implemented and Verified | Local-only, no cloud fallback, fixed temperature |
| **Memory & Knowledge** | Implemented and Verified | No vector search, no cross-project learning, basic conflict handling |
| **Tools & Integrations** | Implemented and Verified | Desktop/Blender/Vision require optional deps; no sandboxing |
| **Voice & Audio** | Partially Implemented | Wake word disabled by default; SAPI only on Windows |
| **Desktop UI** | Implemented and Verified | Edge companion only; orb is headless renderer; no drag |
| **Tests & Reliability** | Implemented and Verified | Real-model/UI tests not run in CI; flaky timing tests |
| **Security & Permissions** | Implemented and Verified | Heuristic secret detection; no semantic command analysis |
| **Dependencies** | Partially Implemented | Many optional deps; external services (Ollama, Blender) required |

---

## 11. EVIDENCE SUMMARY

| Source Type | Count | Examples |
|-------------|-------|----------|
| Source Files Inspected | 485 | All `.py` files in ai/, core/, config/, memory/, tools/, ui/, voice/, integration/, vision/, blender/, coding/, personas/ |
| Test Files Reviewed | 130+ | `tests/test_*.py` across all categories |
| Configuration Files | 3 | `pyproject.toml`, `requirements.txt`, `config/default_config.toml` |
| Historical Audit Reports | 20+ | `DIABLO_*.txt`, `DIABLO_*.md`, `FINAL_AUDIT_REPORT.txt` |
| Documentation | 8 | `README.md`, `docs/*.md`, `docs/contracts/*.md` |

---

**END OF FEATURE CAPABILITY MATRIX**