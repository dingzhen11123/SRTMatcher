from __future__ import annotations

import gc
import sys
import threading
from pathlib import Path
from typing import Callable


class ModelRuntime:
    """Process-local model cache shared by consecutive desktop jobs.

    The application runs one media job at a time. Keeping the most recently used
    ASR model and language-specific alignment models avoids repeated GPU uploads,
    while an explicit release action lets the user reclaim memory at any time.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._asr_key: tuple[str, str, str] | None = None
        self._asr_model: object | None = None
        self.align_models: dict[tuple[str, str], tuple[object, object]] = {}
        self._diarization: dict[tuple[str, str], object] = {}

    @staticmethod
    def _model_key(model_path: str, device: str, compute_type: str) -> tuple[str, str, str]:
        path = str(Path(model_path).expanduser().resolve())
        return path.casefold(), device.casefold(), compute_type.casefold()

    def get_asr_model(
        self,
        model_path: str,
        device: str,
        compute_type: str,
        factory: Callable[..., object],
    ) -> tuple[object, bool]:
        key = self._model_key(model_path, device, compute_type)
        with self._lock:
            if self._asr_key == key and self._asr_model is not None:
                return self._asr_model, True
            had_model = self._asr_model is not None
            self._asr_model = None
            self._asr_key = None
            if had_model:
                self._collect()
            model = factory(
                model_path,
                device=device,
                compute_type=compute_type,
                local_files_only=Path(model_path).exists(),
            )
            self._asr_key = key
            self._asr_model = model
            return model, False

    def get_diarization_pipeline(
        self,
        repository: str,
        device: str,
        factory: Callable[[], object],
    ) -> tuple[object, bool]:
        key = repository, device.casefold()
        with self._lock:
            if key in self._diarization:
                return self._diarization[key], True
            pipeline = factory()
            self._diarization[key] = pipeline
            return pipeline, False

    def stats(self) -> dict:
        with self._lock:
            return {
                "asr_loaded": self._asr_model is not None,
                "alignment_models": len(self.align_models),
                "diarization_models": len(self._diarization),
            }

    def release_device(self, device: str) -> dict:
        """Drop every cached alignment/diarization model that lives on ``device``."""
        target = (device or "").casefold()
        with self._lock:
            align_keys = [key for key in self.align_models if str(key[1]).casefold() == target]
            diarization_keys = [key for key in self._diarization if str(key[1]).casefold() == target]
            for key in align_keys:
                self.align_models.pop(key, None)
            for key in diarization_keys:
                self._diarization.pop(key, None)
            self._collect()
            return {"alignment_models": len(align_keys), "diarization_models": len(diarization_keys)}

    def release_all(self) -> dict:
        with self._lock:
            before = self.stats()
            self._asr_key = None
            self._asr_model = None
            self.align_models.clear()
            self._diarization.clear()
            self._collect()
            return before

    @staticmethod
    def _collect() -> None:
        gc.collect()
        if "torch" not in sys.modules:
            return
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


MODEL_RUNTIME = ModelRuntime()
