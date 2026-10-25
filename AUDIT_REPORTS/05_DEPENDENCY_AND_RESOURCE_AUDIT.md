# DEPENDENCY AND RESOURCE AUDIT — DIABLO PROJECT

**Project Root:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Audit Date:** 2026-10-25  
**Git Branch:** `reliability-batch-1`  
**Git HEAD:** `a74b53c`

---

## 1. DEPENDENCY MANIFESTS

### 1.1 pyproject.toml (Authoritative)

```toml
[project]
name = "diablo-ai"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "rich>=13.7",
    "httpx>=0.28",
    "Pillow>=10.0",
    "numpy>=1.24",
    "matplotlib>=3.7",
    "pywin32>=306; sys_platform == 'win32'",
    "comtypes>=1.4.17; sys_platform == 'win32'",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

### 1.2 requirements.txt (Full Install Set)

```
rich>=13.7
httpx>=0.28
pytest>=8.0
Pillow>=10.0
numpy>=1.24
matplotlib>=3.7
pywin32>=306; sys_platform == 'win32'
comtypes>=1.4.17; sys_platform == 'win32'
faster-whisper>=1.0.0
sounddevice>=0.4.6
av<12.0.0
```

**Difference:** requirements.txt adds test dependency (pytest) and voice dependencies (faster-whisper, sounddevice, av) not in pyproject.toml. This is documented in pyproject.toml comments.

---

## 2. DEPENDENCY ANALYSIS

### 2.1 Required vs Optional Classification

| Package | Required? | Platform | Purpose | Import Location |
|---------|-----------|----------|---------|-----------------|
| Python | YES | All | Runtime | — |
| rich | YES | All | Terminal UI, formatting | `ui/terminal.py`, `main.py` |
| httpx | YES | All | Ollama HTTP client | `ai/ollama_provider.py` |
| Pillow | YES | All | Orb renderer, image handling | `ui/orb/renderer.py`, `vision/` |
| numpy | YES | All | Orb renderer, knowledge graph | `ui/orb/renderer.py`, `core/knowledge_graph.py` |
| matplotlib | YES | All | Chart generation | `tools/chart_tool.py` |
| pytest | TEST ONLY | All | Test runner | `tests/` |
| pywin32 | CONDITIONAL | Windows | Windows capability layer | `core/windows/*.py` |
| comtypes | CONDITIONAL | Windows | UI Automation (UIA) | `core/windows/uia.py`, `voice/tts/sapi.py` |
| faster-whisper | CONDITIONAL | All | Local STT | `voice/stt/local.py` |
| sounddevice | CONDITIONAL | All | Microphone capture | `voice/capture.py` |
| av | CONDITIONAL | All | Audio decoding | `voice/stt/local.py` (faster-whisper dep) |

### 2.2 Conditional Dependencies Detail

| Feature | Dependencies | Fallback Behavior |
|---------|--------------|-------------------|
| **Windows Desktop Automation** | pywin32 + comtypes | Capability reports `available=False`; DesktopWorkflow not registered; agent works without desktop tools |
| **Voice (STT)** | faster-whisper + sounddevice + av | VoiceService unavailable; logs warning; text input still works |
| **Voice (TTS - SAPI)** | comtypes (pywin32) | SAPI backend unavailable; neural/mock fallback |
| **Blender Integration** | Blender executable (external) | BlenderCapabilityLayer reports not installed; capability not registered |
| **GitHub CLI** | gh (external) | gh tools return auth error; graceful degradation |
| **Ollama** | Ollama service (external) | Falls back to OfflineProvider (rule-based) |

### 2.3 Version Constraints & Compatibility Risks

| Package | Constraint | Risk | Notes |
|---------|------------|------|-------|
| av | `<12.0.0` | HIGH | faster-whisper pins av<12; av 12+ breaks API compatibility |
| pywin32 | `>=306` | LOW | Mature; Windows version specific but backward compatible |
| comtypes | `>=1.4.17` | LOW | Required for UIA (no IDispatch); SAPI also needs it |
| faster-whisper | `>=1.0.0` | MEDIUM | CTranslate2 backend; model format changes possible |
| numpy | `>=1.24` | LOW | Used for orb renderer; stable API |
| matplotlib | `>=3.7` | LOW | Chart generation only; stable |
| httpx | `>=0.28` | LOW | Ollama client; async not used (sync API) |

### 2.4 Unused / Potentially Unused Dependencies

| Package | Status | Evidence |
|---------|--------|----------|
| pytest | TEST ONLY | Only in requirements.txt; not in pyproject.toml dependencies |
| av | TRANSITIVE | Only via faster-whisper; not directly imported |
| sounddevice | CONDITIONAL | Only used if voice enabled (default: false) |

**No unused required dependencies found.** All pyproject.toml dependencies have clear import sites.

---

## 3. IMPORTS WITHOUT DECLARED DEPENDENCIES

### 3.1 Standard Library Only (No Risk)

All imports in source code resolve to either:
- Standard library modules (os, sys, json, pathlib, sqlite3, threading, etc.)
- Declared dependencies above
- Local modules

### 3.2 Verified Import Coverage

| Module | Imported By | Declared In |
|--------|-------------|-------------|
| rich | `ui/terminal.py`, `main.py` | pyproject.toml |
| httpx | `ai/ollama_provider.py` | pyproject.toml |
| PIL (Pillow) | `ui/orb/renderer.py`, `vision/inspect.py` | pyproject.toml |
| numpy | `ui/orb/renderer.py`, `core/knowledge_graph.py` | pyproject.toml |
| matplotlib | `tools/chart_tool.py` | pyproject.toml |
| pywin32 (win32*) | `core/windows/*.py` | pyproject.toml (conditional) |
| comtypes | `core/windows/uia.py`, `voice/tts/sapi.py` | pyproject.toml (conditional) |
| faster_whisper | `voice/stt/local.py` | requirements.txt (conditional) |
| sounddevice | `voice/capture.py` | requirements.txt (conditional) |
| av | `voice/stt/local.py` (transitive) | requirements.txt (conditional) |

**No missing declarations found.**

---

## 4. EXTERNAL TOOLS AND EXECUTABLES

| Tool | Required? | Detection Method | Fallback |
|------|-----------|------------------|----------|
| **Ollama** | YES (for LLM) | HTTP `/api/tags` check | OfflineProvider (rule-based) |
| **Blender** | NO | Filesystem probe (`C:/Program Files/Blender Foundation/Blender*/blender.exe`) | Capability not registered |
| **Git** | YES | `git --version` (assumed on PATH) | Git tools fail gracefully |
| **GitHub CLI (gh)** | NO | `gh auth status` | gh tools return error |
| **PowerShell** | YES (Windows) | `powershell -Command` (assumed) | Terminal tool fails |
| **Python** | YES | `sys.executable` (self) | Python tool fails |

### 4.1 External Tool Version Requirements

| Tool | Minimum Version | Checked? |
|------|----------------|----------|
| Ollama | Any with `/api/chat` + tools support | No (runtime capability check) |
| Blender | 5.2+ (configured path) | No (only path existence) |
| Git | 2.0+ | No |
| gh | 2.0+ | No |
| Python | 3.11+ | Enforced by `requires-python` |

---

## 5. MODEL REQUIREMENTS

### 5.1 Configured Models

| Model | Role | Source | Size Estimate |
|-------|------|--------|---------------|
| qwen3:8b | Fast (routine tasks) | Ollama | ~4.7 GB |
| qwen3:14b | Strong (complex/escalation) | Ollama | ~8.2 GB |
| qwen3:latest | Fallback preference | Ollama | Variable |

### 5.2 Model Capabilities Required

- **Tool calling** (function calling) — required for all agent operations
- **Context window** — minimum 8192 tokens (configured 8000)
- **Structured output** — JSON schema adherence for tool calls

### 5.3 Hardware for Models

| Model | VRAM (int8) | RAM (int8) | CPU Only Viable? |
|-------|-------------|------------|------------------|
| qwen3:8b | ~5 GB | ~8 GB | YES (CTranslate2 int8) |
| qwen3:14b | ~9 GB | ~14 GB | YES (slower) |

**No GPU required** — faster-whisper and Ollama (via llama.cpp) support CPU inference.

---

## 6. RESOURCE CONSUMPTION ESTIMATES

### 6.1 Static Resource Footprint

| Component | Disk Space | Notes |
|-----------|------------|-------|
| Source code | ~1.5 MB | 485 Python files |
| Virtual environment | ~200-500 MB | All deps + pip cache |
| Downloaded models (faster-whisper base) | ~150 MB | `data/models/faster-whisper/` |
| Knowledge graphs | ~10-50 MB/project | SQLite DBs in `data/knowledge/` |
| Session data | Variable | `data/sessions/` (can grow large) |
| Logs | ~1-10 MB | `data/logs/diablo.log` |

### 6.2 Runtime Resource Profile (Estimated)

| Scenario | CPU | RAM | GPU | Disk I/O |
|----------|-----|-----|-----|----------|
| Idle (Terminal UI) | <1% | ~50 MB | None | Minimal |
| Agent reasoning (Ollama 8B) | 1-2 cores | +4-8 GB | Optional | Model load |
| Agent reasoning (Ollama 14B) | 2-4 cores | +8-14 GB | Optional | Model load |
| Voice active (STT) | 1-2 cores | +1-2 GB | Optional | Audio buffer |
| Blender operation | 2-8 cores | +2-4 GB | Recommended | Scene I/O |
| Chart generation | 1 core | +200 MB | None | PNG write |
| Knowledge indexing | 1-2 cores | +500 MB | None | File reads |

### 6.3 Resource Risk: No Bounds Enforcement

**CRITICAL GAP:** No runtime resource limits enforced:
- No CPU time limits on tool execution
- No memory limits on subprocesses
- No disk space quotas
- No concurrent operation limits (max_tool_calls is count only)
- `resource_check_enabled` config exists but defaults to `false` with "placeholder until reliable local check exists"

---

## 7. HARDWARE ASSUMPTIONS

| Assumption | Validated? | Risk if False |
|------------|------------|---------------|
| Windows 10/11 | Primary target | Desktop/Voice/Windows capability unavailable on Linux/macOS |
| x64 Architecture | Implicit (Blender, Ollama builds) | ARM64 may lack Blender/Ollama builds |
| Microphone | Optional (voice disabled by default) | Voice features unavailable |
| Display | Optional (UI companions) | Headless operation supported via Terminal UI |
| GPU (NVIDIA/AMD) | Optional | CPU-only slower but functional |
| 16+ GB RAM | Recommended for 14B model | 8B model works on 8 GB; 14B needs 16+ GB |
| 10+ GB Free Disk | For models + sessions | Session data can grow unbounded |

---

## 8. DEPENDENCY GRAPH (CRITICAL PATH)

```
Python 3.11+
├── rich, httpx, Pillow, numpy, matplotlib (ALWAYS REQUIRED)
│   └── Core agent, UI, tools, knowledge graph functional
├── pywin32 + comtypes (WINDOWS ONLY)
│   └── Windows capability → DesktopWorkflow → desktop.* tools
├── faster-whisper + sounddevice + av (VOICE ONLY)
│   └── VoiceService → STT/TTS/VAD/WakeWord
└── External Services (RUNTIME)
    ├── Ollama (LLM) → Agent reasoning
    ├── Blender (3D) → Blender capability
    └── gh CLI (GitHub) → gh.* tools
```

---

## 9. COMPATIBILITY MATRIX

| Component | Windows | Linux | macOS | Notes |
|-----------|---------|-------|-------|-------|
| Core Agent | ✅ | ✅ | ✅ | Pure Python |
| Terminal UI | ✅ | ✅ | ✅ | rich-based |
| Edge Companion | ✅ | ❌ | ❌ | Tk + win32 for placement |
| Floating Orb | ✅ | ✅ | ✅ | Headless renderer (numpy+PIL) |
| Voice (STT) | ✅ | ✅ | ✅ | faster-whisper cross-platform |
| Voice (TTS) | ✅ | ⚠️ | ⚠️ | SAPI Windows only; neural placeholder |
| Windows Capability | ✅ | ❌ | ❌ | pywin32/comtypes Windows-only |
| Desktop Automation | ✅ | ❌ | ❌ | Requires Windows capability |
| Blender Integration | ✅ | ✅ | ✅ | Blender cross-platform |
| Git/GitHub Tools | ✅ | ✅ | ✅ | CLI tools cross-platform |
| Ollama | ✅ | ✅ | ✅ | Cross-platform |

---

## 10. UPGRADE / MAINTENANCE CONSIDERATIONS

### 10.1 Pinned Versions Requiring Attention

| Package | Current Pin | Upgrade Consideration |
|---------|-------------|----------------------|
| av | `<12.0.0` | Blocked by faster-whisper; monitor CTranslate2 releases |
| faster-whisper | `>=1.0.0` | Model format changes possible; test STT after upgrade |
| pywin32 | `>=306` | Windows SDK alignment; test Windows capability after upgrade |
| comtypes | `>=1.4.17` | UIA/SAPI stability; test voice + desktop after upgrade |

### 10.2 Python Version Support

- **Minimum:** 3.11 (enforced by `requires-python`)
- **Tested:** 3.11, 3.12 (implied by `>=3.11`)
- **3.13+:** Not yet tested; monitor for breaking changes

### 10.3 External Service Compatibility

| Service | Protocol | Version Tolerance |
|---------|----------|-------------------|
| Ollama | HTTP `/api/chat`, `/api/tags` | High (stable API) |
| Blender | CLI `-b -P script.py` | Medium (Python API changes) |
| gh CLI | Command output parsing | Low (output format changes) |

---

## 11. RESOURCE RISK SUMMARY

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Ollama unavailable | HIGH | Agent falls back to rule-based (severely limited) | Document offline mode; consider local llama.cpp fallback |
| Blender not installed | MEDIUM | Blender capability unavailable | Clear docs; graceful degradation |
| Insufficient RAM for 14B | MEDIUM | OOM or extreme swapping | Default to 8B; resource_check hook (disabled) |
| Disk full from sessions | LOW | Write failures | Add session rotation/cleanup policy |
| Microphone permission denied | LOW | Voice unavailable | Graceful degradation (already implemented) |
| Display unavailable (headless) | LOW | UI companions fail | Terminal UI fallback (already implemented) |

---

**END OF DEPENDENCY AND RESOURCE AUDIT**