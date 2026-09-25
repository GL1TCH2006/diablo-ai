from __future__ import annotations

import subprocess
import threading

from core.logging_setup import get_logger

log = get_logger("voice")


def _ps_script(text: str) -> str:
    escaped = text.replace("'", "''")
    return (
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.SelectVoice('Microsoft David Desktop')\n"
        f"$s.Speak('{escaped}')\n"
        "$s.Dispose()"
    )


def is_sapi_available() -> bool:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().Count"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        return proc.returncode == 0 and str(proc.stdout or "").strip().isdigit()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _speak_blocking(text: str) -> bool:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _ps_script(text)],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            log.warning("TTS stderr: %s", (proc.stderr or "").strip()[:200])
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("TTS failed: %s", exc)
        return False


def speak(text: str, now: bool = False) -> bool:
    """Speak text via Windows SAPI. Runs in a background thread unless `now` is set."""
    result: dict[str, bool] = {}

    def _run() -> None:
        result["ok"] = _speak_blocking(text)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    if now:
        thread.join(timeout=35)
        return result.get("ok", False)
    return True