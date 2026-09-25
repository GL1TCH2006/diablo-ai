import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pytest

from config.settings import Settings


def make_settings(tmp: Path) -> Settings:
    root = tmp.resolve()
    data = root / "data"
    return Settings(
        base_dir=root,
        ai_provider="ollama",
        ollama_host="http://localhost:11434",
        ai_model="",
        workspace_root=root / "projects",
        data_dir=data,
        memory_dir=data / "memory",
        logs_dir=data / "logs",
        backups_dir=data / "backups",
        scratch_dir=root / "workspace",
        sessions_dir=data / "sessions",
        voice_enabled=False,
        log_level="INFO",
        max_turns=4,
        max_conversation_turns=20,
        allowed_roots=[root],
        terminal_auto_allow=["git status", "dir", "python --version", "where", "ls"],
        terminal_blocklist=["format", "shutdown", "reboot", "diskpart", "bcdedit", "del /s", "rm -rf", "rmdir /s", "rd /s", "erase", "deltree"],
        app_whitelist={"calculator": "calc", "notepad": "notepad", "blender": "blender"},
        app_known_paths={"notepad": "C:\\Windows\\System32\\notepad.exe", "blender": "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe"},
        preferred_models=["qwen3:latest", "qwen3:8b", "qwen3:14b"],
        raw={},
    )


@pytest.fixture
def tmp_settings(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.memory_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root).mkdir(parents=True, exist_ok=True)
    return settings