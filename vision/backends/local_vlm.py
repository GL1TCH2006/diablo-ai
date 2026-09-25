from __future__ import annotations

"""Optional pluggable local vision-language backend (model-dependent).

This backend is OPTIONAL. It is never auto-loaded or auto-downloaded.

It only becomes available when ALL of the following are true:
    * ``torch`` and ``transformers`` are installed in the environment
    * a local model directory is configured via ``config["model_path"]``
    * that directory exists on disk with model weights

If any prerequisite is missing, ``check_availability()`` returns ``False``
with an explanatory reason and the capability layer simply does not select
this backend. No inference is attempted and nothing is downloaded.

Intended target models (user-installed, e.g. via Ollama/transformers.pipeline
or a HF cache): small VLMs like Moondream2, LLaVA 1.5 7B, or tiny-llava.
The hardware target is an RTX 4050 Laptop (~6GB VRAM), so a 7B int4/8-bit
model or a <1B VLM is realistic.
"""

import time
from pathlib import Path
from typing import Any

from vision.backends.base import VisionBackend
from vision.protocol import VisionRequest, VisionResult


class LocalVLMBackend(VisionBackend):
    """Generic huggingface ``transformers`` VLM backend (pluggable opt-in)."""

    name = "local_vlm"
    description = "Local vision-language model via huggingface transformers"
    supported_operations = frozenset({"describe", "analyze_ui", "extract_text", "detect_objects"})
    requires_gpu = True
    min_vram_mb = 6000

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._model = None
        self._processor = None

    # ------------------------------------------------------------------- deps

    def _load_deps(self) -> None:
        """Deferred import of heavyweight dependencies (never at module load)."""
        if self._model is not None:
            return
        import torch  # noqa: PLC0415 - deferred heavyweight import
        import transformers  # noqa: PLC0415

        model_path = self.config.get("model_path", "")
        if not model_path:
            raise RuntimeError("no model_path configured in backend config")
        model_dir = Path(model_path)
        if not model_dir.is_dir():
            raise RuntimeError(f"model directory not found: {model_dir}")
        if not any(model_dir.iterdir()):
            raise RuntimeError(f"model directory is empty: {model_dir}")

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.config.get("device"):
            device = str(self.config["device"])

        # Local-only loading; trust_remote_code is never enabled and no
        # download is ever triggered. If the model is not on disk this raises.
        self._processor = transformers.AutoProcessor.from_pretrained(
            str(model_dir), local_files_only=True
        )
        self._model = transformers.AutoModelForCausalLM.from_pretrained(
            str(model_dir),
            local_files_only=True,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            device_map="auto" if device == "cuda" else None,
        )
        self._device = device

    # ------------------------------------------------------------- availability

    def check_availability(self) -> tuple[bool, str]:
        try:
            import torch  # noqa: PLC0415, F401
            import transformers  # noqa: PLC0415, F401
        except ImportError as exc:
            return False, f"torch/transformers not installed ({exc})"

        model_path = self.config.get("model_path", "")
        if not model_path:
            return False, "no model_path configured"
        model_dir = Path(model_path)
        if not model_dir.is_dir():
            return False, f"model directory not found: {model_dir}"
        if not any(model_dir.iterdir()):
            return False, f"model directory is empty: {model_dir}"
        return True, "local model available"

    # --------------------------------------------------------------- inference

    def inference(self, request: VisionRequest) -> VisionResult:
        start = time.perf_counter()

        def duration() -> float:
            return (time.perf_counter() - start) * 1000

        available, reason = self.check_availability()
        if not available:
            return VisionResult.failure_result(
                operation=request.operation,
                error={"code": "vision_backend_not_found", "message": reason},
                request_id=request.request_id,
                backend=self.name,
                duration_ms=duration(),
            )

        try:
            self._load_deps()
        except Exception as exc:  # noqa: BLE001 - structured failure report
            return VisionResult.failure_result(
                operation=request.operation,
                error={"code": "vision_model_error", "message": str(exc)},
                request_id=request.request_id,
                backend=self.name,
                duration_ms=duration(),
            )

        try:
            image = self._open_image(request.image_path)
            operation = request.operation
            prompt = self._build_prompt(operation, request.params, request.context)
            inputs = self._processor(images=image, text=prompt, return_tensors="pt")
            if getattr(self, "_device", "cuda") == "cuda":
                inputs = {k: v.to(self._device) for k, v in inputs.items()}
            output = self._model.generate(**inputs, max_new_tokens=self.config.get("max_new_tokens", 128))
            text = self._processor.batch_decode(output, skip_special_tokens=True)[0]

            data: dict[str, Any] = {"text": text}
            if operation == "describe":
                data["description"] = text
            elif operation == "analyze_ui":
                data["ui_elements"] = [{"description": text}]
            elif operation == "extract_text":
                data["extracted_text"] = text
            elif operation == "detect_objects":
                data["objects"] = [{"label": text}]

            return VisionResult.success_result(
                operation=operation,
                data=data,
                request_id=request.request_id,
                backend=self.name,
                duration_ms=duration(),
            )
        except Exception as exc:  # noqa: BLE001 - structured inference failure
            import traceback

            return VisionResult.failure_result(
                operation=request.operation,
                error={
                    "code": "vision_backend_error",
                    "message": str(exc),
                    "details": {"traceback": traceback.format_exc()[:2000]},
                },
                request_id=request.request_id,
                backend=self.name,
                duration_ms=duration(),
            )

    def _open_image(self, image_path: str):
        from PIL import Image  # noqa: PLC0415 - deferred

        return Image.open(image_path).convert("RGB")

    def _build_prompt(self, operation: str, params: dict, context: dict) -> str:
        focus = params.get("focus") or ""
        if operation == "describe":
            base = "Describe this image in detail."
            if focus:
                return f"{base} Focus on {focus}."
            return base
        if operation == "analyze_ui":
            return "List the visible UI elements in this screenshot and their approximate locations."
        if operation == "extract_text":
            lang = params.get("language") or ""
            return f"Extract all visible text from this image{(' in ' + lang) if lang else ''}."
        if operation == "detect_objects":
            threshold = params.get("confidence_threshold", 0.5)
            return f"List all objects detected in this image with confidence above {threshold}."
        return "Describe this image in detail."


# Optional: register only if the caller opts in. Not registered by default.
def register_local_vlm(config: dict | None = None) -> LocalVLMBackend:
    """Factory to opt-in an installed local VLM backend into the registry."""
    from vision.backends import registry

    backend = LocalVLMBackend(config)
    registry.register(backend, priority=100)
    return backend