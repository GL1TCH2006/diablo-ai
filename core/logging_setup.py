from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SENSITIVE_KEYS = (
    "password", "passwd", "secret", "token", "apikey",
    "api_key", "api-key", "authorization", "credential", "private_key",
)

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def redact(obj):
    if isinstance(obj, dict):
        return {k: (redact(v) if not _is_sensitive(k) else "<redacted>") for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and obj:
        return _redact_text(obj)
    return obj


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(s in lowered for s in SENSITIVE_KEYS)


def _redact_text(text: str) -> str:
    lowered = text.lower()
    if any(s in lowered for s in SENSITIVE_KEYS):
        return "<redacted>"
    return text


def setup_logging(level: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("diablo")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(file_handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"diablo.{name}")


def redact_payload(payload: dict) -> dict:
    return redact(payload) if isinstance(payload, dict) else {"data": redact(payload)}