from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

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


class SapiSpeaker:
    """Manages a SAPI TTS subprocess with interruption support."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._speech_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _run_speech(self, text: str, result: dict) -> None:
        """Run the speech in a subprocess, checking for cancellation."""
        try:
            # Build the PowerShell script
            ps_script = _ps_script(text)
            
            # Start the subprocess
            with self._lock:
                if self._stop_event.is_set():
                    result["ok"] = False
                    result["cancelled"] = True
                    return
                self._process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", _ps_script(text)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            
            # Wait for completion or cancellation
            while True:
                if self._stop_event.is_set():
                    self._terminate_process()
                    result["ok"] = False
                    result["cancelled"] = True
                    return
                
                retcode = self._process.poll()
                if retcode is not None:
                    # Process completed
                    if retcode != 0:
                        stderr = self._process.stderr.read() if self._process.stderr else ""
                        log.warning("TTS stderr: %s", stderr.strip()[:200])
                    result["ok"] = (retcode == 0)
                    return
                
                time.sleep(0.05)  # Small sleep to avoid busy waiting
                
        except Exception as exc:
            log.warning("TTS failed: %s", exc)
            result["ok"] = False
        finally:
            self._cleanup()

    def _terminate_process(self) -> None:
        """Terminate the running subprocess if any."""
        with self._lock:
            if self._process and self._process.poll() is None:
                try:
                    self._process.terminate()
                    # Give it a moment to terminate gracefully
                    try:
                        self._process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=1.0)
                except Exception:
                    pass

    def _cleanup(self) -> None:
        """Clean up process references."""
        with self._lock:
            self._process = None

    def speak(self, text: str, now: bool = False) -> tuple[bool, bool]:
        """
        Speak text via Windows SAPI.
        
        Returns:
            tuple: (success, was_cancelled)
        """
        if not text.strip():
            return False, False
        
        if not is_sapi_available():
            return False, False
        
        self._stop_event.clear()
        result = {"ok": False, "cancelled": False}
        
        self._speech_thread = threading.Thread(target=self._run_speech, args=(text, result), daemon=True)
        self._speech_thread.start()
        
        if now:
            self._speech_thread.join(timeout=35)
            return result.get("ok", False), result.get("cancelled", False)
        
        return True, False

    def stop(self) -> bool:
        """Stop any ongoing speech. Returns True if speech was stopped, False if no speech was playing."""
        was_speaking = False
        with self._lock:
            was_speaking = self._process is not None and self._process.poll() is None
            self._stop_event.set()
        
        if self._speech_thread and self._speech_thread.is_alive():
            self._speech_thread.join(timeout=2.0)
        
        return was_speaking

    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        with self._lock:
            return self._process is not None and self._process.poll() is None


# Global instance for backward compatibility
_global_speaker = SapiSpeaker()


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
    """Legacy blocking speak function for backward compatibility."""
    return _global_speaker.speak(text, now=True)[0]


def speak(text: str, now: bool = False) -> bool:
    """Speak text via Windows SAPI. Runs in a background thread unless `now` is set.
    
    Returns True if speech started (or completed if now=True), False on failure.
    """
    success, _ = _global_speaker.speak(text, now)
    return success


def stop_speaking() -> bool:
    """Stop any ongoing speech. Returns True if speech was stopped."""
    return _global_speaker.stop()


def is_speaking() -> bool:
    """Check if currently speaking."""
    return _global_speaker.is_speaking()