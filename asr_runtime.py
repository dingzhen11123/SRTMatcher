from __future__ import annotations


PRESET_LABELS = {
    "fast": "快速",
    "recommended": "推荐",
    "quality": "高质量",
}


def normalize_performance_preset(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in PRESET_LABELS else "recommended"


def transcribe_options(preset: str) -> dict:
    preset = normalize_performance_preset(preset)
    beam_size = {"fast": 1, "recommended": 5, "quality": 8}[preset]
    return {
        "beam_size": beam_size,
        "word_timestamps": True,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 1.0,
    }
