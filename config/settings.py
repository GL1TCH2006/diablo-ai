from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "default_config.toml"
ENV_FILE = BASE_DIR / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: str | None, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    # A non-positive timeout would mean "give up immediately", which is never
    # what someone setting a timeout intends.
    return parsed if parsed > 0 else default


@dataclass
class Settings:
    base_dir: Path = BASE_DIR
    ai_provider: str = "ollama"
    ollama_host: str = "http://localhost:11434"
    allow_remote_ollama: bool = False
    ai_model: str = ""
    workspace_root: Path = Path("C:/PROJECTS")
    data_dir: Path = BASE_DIR / "data"
    memory_dir: Path = BASE_DIR / "data" / "memory"
    logs_dir: Path = BASE_DIR / "data" / "logs"
    backups_dir: Path = BASE_DIR / "data" / "backups"
    scratch_dir: Path = BASE_DIR / "workspace"
    sessions_dir: Path = BASE_DIR / "data" / "sessions"
    voice_enabled: bool = False
    log_level: str = "INFO"
    max_turns: int = 4
    max_tool_calls: int = 32
    max_recovery_attempts: int = 3
    # M2.16.UI.1: bounded wrong-tool corrections per request. Zero disables the
    # correction entirely and leaves only the telemetry.
    desktop_mismatch_corrections: int = 1
    # M2.16.UI.1: minimum classifier confidence before the presented tool set is
    # narrowed to the desktop family. Below this the normal widened set is used.
    desktop_focus_min_confidence: float = 0.6
    max_conversation_turns: int = 20
    allowed_roots: list[Path] = field(default_factory=list)
    terminal_auto_allow: list[str] = field(default_factory=list)
    terminal_blocklist: list[str] = field(default_factory=list)
    app_whitelist: dict[str, str] = field(default_factory=dict)
    app_known_paths: dict[str, str] = field(default_factory=dict)
    preferred_models: list[str] = field(default_factory=list)
    max_result_chars: int = 8000
    context_max_tokens: int = 8000
    context_relevant_files: int = 8
    context_excerpt_lines: int = 200
    debug_max_attempts: int = 3
    debug_max_steps: int = 12
    memory_write_policy: str = "explicit"
    rules_max_active_in_prompt: int = 8
    models_mode: str = "auto"
    models_fast: str = "qwen3:8b"
    models_strong: str = "qwen3:14b"
    models_complexity_threshold: float = 0.6
    models_max_attempts_before_escalate: int = 1
    models_max_escalations: int = 1
    resource_check_enabled: bool = False
    #: M2.13.0: per-request model timeout in seconds. The default is the
    #: historical 600s, chosen because a cold large model on a loaded machine can
    #: legitimately think for minutes; making it explicit lets a user on known
    #: slow hardware shorten it deliberately instead of it being unreachable.
    ai_request_timeout: float = 600.0
    #: M2.13.0: when true, every model turn sees the complete registry instead
    #: of the adaptively selected subset. The subset only changes what the model
    #: is *shown* - permissions, dispatch and verification are unaffected - so
    #: this exists as an escape hatch and a way to A/B the two behaviours.
    tools_full_context: bool = False
    git_push_policy: str = "blocked"
    git_restore_policy: str = "blocked"
    git_branch_policy: str = "blocked"
    raw: dict = field(default_factory=dict)


def _resolve(value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


def load_settings() -> Settings:
    env = {**_load_env_file(ENV_FILE), **os.environ}
    toml: dict = {}
    if CONFIG_FILE.exists():
        toml = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    data_dir = _resolve(env.get("DATA_DIR", _str(toml, ["paths", "data_dir"], "data")))
    workspace_root = _resolve(env.get("WORKSPACE_ROOT", _str(toml, ["project", "workspace_root"], "C:/PROJECTS")))
    sessions_dir = _resolve(env.get("SESSIONS_DIR", _str(toml, ["paths", "sessions_dir"], "data/sessions")))

    allowed_roots = [_resolve(p) for p in _list(toml, ["filesystem", "allowed_roots"])]
    allowed_roots.append(BASE_DIR)

    settings = Settings(
        ai_provider=env.get("AI_PROVIDER", _str(toml, ["ai", "provider"], "ollama")),
        ollama_host=env.get("OLLAMA_HOST", _str(toml, ["ollama", "host"], "http://localhost:11434")),
        allow_remote_ollama=_as_bool(env.get("ALLOW_REMOTE_OLLAMA"), _bool(toml, ["ollama", "allow_remote"], False)),
        ai_model=env.get("AI_MODEL", ""),
        ai_request_timeout=_as_float(
            env.get("AI_REQUEST_TIMEOUT"), _as_float(_str(toml, ["ai", "request_timeout"], ""), 600.0)
        ),
        workspace_root=workspace_root,
        data_dir=data_dir,
        memory_dir=data_dir / "memory",
        logs_dir=data_dir / "logs",
        backups_dir=data_dir / "backups",
        scratch_dir=BASE_DIR / "workspace",
        sessions_dir=sessions_dir,
        voice_enabled=_as_bool(env.get("VOICE_ENABLED"), _bool(toml, ["voice", "enabled"], False)),
        log_level=env.get("LOG_LEVEL", _str(toml, ["logging", "level"], "INFO")).upper(),
        max_turns=int(_int(toml, ["agent", "max_turns"], 4)),
        max_tool_calls=int(_int(toml, ["agent", "max_tool_calls"], 32)),
        max_recovery_attempts=int(_int(toml, ["agent", "max_recovery_attempts"], 3)),
        desktop_mismatch_corrections=max(0, int(_int(toml, ["agent", "desktop_mismatch_corrections"], 1))),
        desktop_focus_min_confidence=max(0.0, min(1.0, _float(toml, ["agent", "desktop_focus_min_confidence"], 0.6))),
        max_conversation_turns=int(_int(toml, ["memory", "max_conversation_turns"], 20)),
        allowed_roots=allowed_roots,
        terminal_auto_allow=_list(toml, ["terminal", "auto_allow_patterns"]),
        terminal_blocklist=_list(toml, ["terminal", "blocklist"]),
        app_whitelist=_dict(toml, ["apps", "whitelist"]),
        app_known_paths=_dict(toml, ["apps", "known_paths"]),
        preferred_models=_list(toml, ["ollama", "preferred_models"]),
        max_result_chars=int(_int(toml, ["tools", "max_result_chars"], 8000)),
        context_max_tokens=int(_int(toml, ["context", "max_tokens"], 8000)),
        context_relevant_files=int(_int(toml, ["context", "relevant_files"], 8)),
        context_excerpt_lines=int(_int(toml, ["context", "excerpt_lines"], 200)),
        debug_max_attempts=int(_int(toml, ["debug", "max_attempts"], 3)),
        debug_max_steps=int(_int(toml, ["debug", "max_steps"], 12)),
        memory_write_policy=_str(toml, ["memory", "write_policy"], "explicit"),
        rules_max_active_in_prompt=int(_int(toml, ["rules", "max_active_in_prompt"], 8)),
        models_mode=_str(toml, ["models", "default_mode"], "auto"),
        models_fast=_str(toml, ["models", "fast_model"], "qwen3:8b"),
        models_strong=_str(toml, ["models", "strong_model"], "qwen3:14b"),
        models_complexity_threshold=_float(toml, ["models", "complexity_threshold"], 0.6),
        models_max_attempts_before_escalate=int(_int(toml, ["models", "max_attempts_before_escalate"], 1)),
        models_max_escalations=int(_int(toml, ["models", "max_escalations"], 1)),
        resource_check_enabled=_as_bool(env.get("RESOURCE_CHECK"), _bool(toml, ["resources", "check_enabled"], False)),
        # M2.16.UI.1 fix. These two previously passed a *string* default through
        # ``_str(...)``, and ``_as_bool`` returns its default unchanged when the env
        # var is absent - so both settings were the truthy string "false".
        #
        # The consequence was not cosmetic: ``tools_full_context`` truthy made
        # Agent._tool_context call ``select(full=True)`` on every turn, so the whole
        # M2.13.0 tool-context selector was dead in production and the model was
        # handed all 74 tools every turn. ``resource_check_enabled`` truthy turned
        # the model resource guard permanently on. Both now take a real bool default,
        # matching allow_remote_ollama and voice_enabled above.
        tools_full_context=_as_bool(env.get("TOOLS_FULL_CONTEXT"), _bool(toml, ["tools", "full_context"], False)),
        git_push_policy=_str(toml, ["git", "push"], "blocked"),
        git_restore_policy=_str(toml, ["git", "restore"], "blocked"),
        git_branch_policy=_str(toml, ["git", "branch"], "blocked"),
        raw=toml,
    )
    for path_attr in ("data_dir", "memory_dir", "logs_dir", "backups_dir", "scratch_dir", "sessions_dir"):
        getattr(settings, path_attr).mkdir(parents=True, exist_ok=True)
    return settings


def _str(data: dict, keys: list[str], default: str) -> str:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        return str(node.get(keys[-1], default))
    return default


def _int(data: dict, keys: list[str], default: int) -> int:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        return int(node.get(keys[-1], default))
    return default


def _bool(data: dict, keys: list[str], default: bool) -> bool:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        return bool(node.get(keys[-1], default))
    return default


def _float(data: dict, keys: list[str], default: float) -> float:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        return float(node.get(keys[-1], default))
    return default


def _list(data: dict, keys: list[str]) -> list[str]:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        value = node.get(keys[-1], [])
        return list(value) if isinstance(value, list) else []
    return []


def _dict(data: dict, keys: list[str]) -> dict:
    node = _get(data, keys[:-1])
    if isinstance(node, dict):
        value = node.get(keys[-1], {})
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _get(data: dict, keys: list[str]):
    node = data
    for key in keys:
        if isinstance(node, dict):
            node = node.get(key)
        else:
            return None
    return node