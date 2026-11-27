# BASELINE AND ENVIRONMENT

**Mission:** M3.0 Engineering Mission  
**Date:** 2026-10-25  
**Project:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`

## GIT STATE

```
Branch: reliability-batch-1
HEAD: a74b53c (docs: M2.17.x Task 1 verification pipeline map)
```

### Modified Files (39)
- `.gitignore`
- `README.md`
- `blender/capabilities.py`
- `blender/runner.py`
- `blender/scripts/diablo_blender_ops.py`
- `config/default_config.toml`
- `config/settings.py`
- `core/agent.py`
- `core/knowledge_graph.py`
- `core/knowledge_ingest.py`
- `core/verification.py`
- `core/verification_ledger.py`
- `docs/DIABLO_M2_16_UI_M2_17_COMPLETE_AUDIT.txt`
- `docs/M2_17_TASK0_BASELINE_AND_SKIP_INVENTORY.txt`
- `main.py`
- `memory/sanitizer.py`
- `requirements.txt`
- `tests/m217_restart_child.py`
- `tests/test_applications.py`
- `tests/test_knowledge_ingest.py`
- `tests/test_m213_coding_workflow.py`
- `tests/test_m2152_blender_cancellation.py`
- `tests/test_m216_ui_companion.py`
- `tests/test_m216_ui_integration.py`
- `tests/test_m217_autonomy_integration.py`
- `tests/test_memory_safety_and_rule_scope.py`
- `tests/test_python.py`
- `tests/test_recovery_p10_repair.py`
- `tests/test_s2_9_hardening.py`
- `tests/test_terminal.py`
- `tests/test_terminal_state_invariant.py`
- `tools/__init__.py`
- `tools/applications.py`
- `tools/filesystem.py`
- `tools/python.py`
- `tools/terminal.py`
- `ui/companion_state.py`
- `ui/edge_companion.py`
- `ui/launch.py`
- `ui/terminal.py`
- `voice/__init__.py`
- `voice/output.py`
- `voice/service.py`
- `voice/stt/__init__.py`
- `voice/stt/fallback.py`
- `voice/stt/local.py`
- `voice/text_to_speech.py`
- `voice/tts/sapi.py`
- `voice/wake_wording.py`

### Untracked Files (53)
- `AGENTS.md`
- `AUDIT_REPORTS/` (directory)
- 20+ historical audit reports (`DIABLO_*.txt`, `DIABLO_*.md`, `FINAL_AUDIT_REPORT.txt`)
- `audit_checks.py`
- `data/` (directory)
- `diablo-overnight-req-audit.txt`
- `personas/` (directory)
- `scripts/` (directory)
- `test_term_tree.py`
- 12 test files (`tests/test_*.py`)
- `ui/orb/` (directory)
- `voice/capture.py`
- `voice/vad.py`
- `voice_recordings/` (directory)

## ENVIRONMENT

### Python
```
Python 3.14.6 (tags/v3.14.6:c63aec6, Jun 10 2026, 10:26:10) [MSC v.1944 64 bit (AMD64)]
```

### Dependencies (from pyproject.toml)
- `rich>=13.7`
- `httpx>=0.28`
- `Pillow>=10.0`
- `numpy>=1.24`
- `matplotlib>=3.7`
- `pywin32>=306; sys_platform == 'win32'`
- `comtypes>=1.4.17; sys_platform == 'win32'`

### Additional Dependencies (from requirements.txt)
- `pytest>=8.0`
- `faster-whisper>=1.0.0`
- `sounddevice>=0.4.6`
- `av<12.0.0`

### External Services (Not Required for CI)
- Ollama (LLM provider) - optional, falls back to offline
- Blender - optional, capability not registered if missing
- GitHub CLI (gh) - optional
- Microphone/Display - optional for UI/voice tests

## TEST BASELINE RESULTS

### Passing Test Suites
| Test File | Tests | Duration | Status |
|-----------|-------|----------|--------|
| `test_agent.py` | 4 | 0.47s | ✅ PASS |
| `test_memory_safety_and_rule_scope.py` | 83 | 1.46s | ✅ PASS |
| `test_filesystem_containment.py` | 14 | 1.36s | ✅ PASS |
| `test_filesystem_delete_verification.py` | 14 | 1.36s | ✅ PASS |
| `test_terminal_state_invariant.py` | 32 | 28.90s | ✅ PASS |
| `test_verification.py` | 7 | 0.90s | ✅ PASS |
| `test_verification_ledger.py` | 77 | 0.90s | ✅ PASS |
| `test_recovery_p10_repair.py` | 51 | ~60s | ✅ PASS (1 timeout) |

### Known Test Issues
| Test | Issue | Impact |
|------|-------|--------|
| `test_terminal.py::test_terminate_to_kill_escalation` | Timeout (120s) | Pre-existing |
| `test_recovery_p10_repair.py::test_the_graph_knows_the_task_and_its_verification` | Timeout (120s) | Pre-existing |

## CONFIRMED AUDIT CONCERNS

| Concern | Status | Evidence |
|---------|--------|----------|
| No CI/CD pipeline | CONFIRMED | No `.github/workflows/` directory |
| No resource bounds enforcement | CONFIRMED | `resource_check_enabled=false` default; no timeout enforcement in tools |
| Duplicate `model_router.py` | CONFIRMED | `core/model_router.py` exists but unreferenced; `ai/model_router.py` is canonical |
| Dead code: `MockCapability` | CONFIRMED | Exported in `integration/__init__.py` but unused |
| Dead code: `KnowledgeGraph.transaction()` | CONFIRMED | Docstring states "currently unreferenced anywhere" |
| Flaky orb renderer tests | CONFIRMED | Timing assertions in `test_orb_renderer_batch6.py` |
| Agent monolith (2226 lines) | CONFIRMED | `core/agent.py` combines planning, execution, verification, recovery, memory, routing |
| Synchronous tool execution | CONFIRMED | `Executor.run()` blocks on tool dispatch |
| Tk UI blocking | CONFIRMED | `EdgeCompanionApp` runs on Tk main thread with `root.after` |

## UNCONFIRMED / REQUIRES FURTHER INVESTIGATION

| Concern | Status |
|---------|--------|
| Prompt injection via tool outputs | Architecture suggests risk; needs runtime verification |
| No sandbox for code/shell | Architecture confirms full privileges; no isolation layer |
| Cross-restart recovery | Tests pass but not fully exercised in this session |
| Voice cancellation propagation | Tests pass but not exercised with real voice |

## IMPLEMENTATION PRIORITY

1. **GitHub Actions CI** - High value, low risk, enables future validation
2. **Resource bounds enforcement** - High value, addresses audit finding directly
3. **Duplicate/dead code cleanup** - Low risk, reduces maintenance burden
4. **Agent decomposition preparation** - Document interfaces, no structural changes yet