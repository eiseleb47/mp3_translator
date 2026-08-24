"""Wrapper um faster-whisper: lädt das Modell einmalig und liefert Segmente mit Fortschritt."""

import os
from pathlib import Path


class Cancelled(Exception):
    pass


def _device_and_compute() -> tuple[str, str]:
    device = os.environ.get("AUDIO_TRANSKRIPT_DEVICE", "cpu").strip().lower()
    if device not in ("cpu", "cuda", "auto"):
        device = "cpu"
    if device == "auto":
        try:
            from ctranslate2 import get_cuda_device_count

            device = "cuda" if get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = os.environ.get("AUDIO_TRANSKRIPT_COMPUTE", "").strip()
    if not compute:
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._key: tuple | None = None

    def load(self, model_name: str):
        from faster_whisper import WhisperModel

        device, compute = _device_and_compute()
        key = (model_name, device, compute)
        if self._key != key:
            self._model = None
            threads = max(1, min(8, (os.cpu_count() or 4)))
            self._model = WhisperModel(
                model_name, device=device, compute_type=compute, cpu_threads=threads
            )
            self._key = key
        return self._model

    def is_loaded(self, model_name: str) -> bool:
        device, compute = _device_and_compute()
        return self._key == (model_name, device, compute)

    def transcribe(
        self,
        wav_path: Path,
        model_name: str,
        language: str,
        duration: float | None,
        on_progress=None,
        cancel=None,
    ) -> dict:
        model = self.load(model_name)
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        segments, info = model.transcribe(
            str(wav_path),
            language=None if language == "auto" else language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )

        total = duration or getattr(info, "duration", None) or 0.0
        collected = []
        for segment in segments:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            text = (segment.text or "").strip()
            if text:
                collected.append((float(segment.start), float(segment.end), text))
            if on_progress and total > 0:
                on_progress(min(1.0, float(segment.end) / total))
        if on_progress:
            on_progress(1.0)

        return {
            "segments": collected,
            "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "duration": total or None,
        }
