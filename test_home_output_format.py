from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app


class FakeWhisperModel:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def transcribe(self, media_path: str, **kwargs):
        segments = [
            SimpleNamespace(start=0.0, end=1.0, text=" Erste Zeile "),
            SimpleNamespace(start=1.0, end=2.0, text="Zweite Zeile"),
        ]
        return iter(segments), SimpleNamespace(language="de")


class HomeOutputFormatTests(unittest.TestCase):
    def run_job(self, output_format: str | None) -> tuple[Path, list[str]]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        media_path = root / "podcast.mp4"
        media_path.touch()
        extension = output_format or "srt"
        output_path = root / f"podcast.transcribed.{extension}"
        model_dir = root / "model"
        model_dir.mkdir()
        config = {
            "audio_path": str(media_path),
            "output_path": str(output_path),
            "model_path": str(model_dir),
            "mode": "transcribe",
            "device": "cpu",
            "compute_type": "int8",
            "language": "de",
            "ai_enabled": False,
            "diarization_enabled": False,
        }
        if output_format is not None:
            config["output_format"] = output_format
        logs: list[str] = []

        with (
            patch.dict(
                sys.modules,
                {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel)},
            ),
            patch.object(app, "prepare_model_files", return_value=str(model_dir)),
            patch.object(app, "add_cuda_dll_paths"),
        ):
            result = app.run_srt_job(config, logs.append)

        self.assertEqual(Path(result), output_path)
        return output_path, logs

    def test_txt_choice_writes_plain_text_instead_of_srt(self) -> None:
        output_path, logs = self.run_job("txt")

        text = output_path.read_text(encoding="utf-8-sig")
        self.assertEqual(text, "Erste Zeile\nZweite Zeile\n")
        self.assertNotIn("-->", text)
        self.assertIn(f"TXT 已保存: {output_path}", logs)

    def test_missing_choice_remains_backward_compatible_with_srt(self) -> None:
        output_path, logs = self.run_job(None)

        text = output_path.read_text(encoding="utf-8-sig")
        self.assertIn("00:00:00,000 --> ", text)
        self.assertIn("Erste Zeile", text)
        self.assertIn(f"SRT 已保存: {output_path}", logs)

    def test_txt_renderer_preserves_speaker_labels(self) -> None:
        text = app.transcript_text_from_subtitles(
            [
                app.Subtitle(1, 0.0, 1.0, "Hallo", "主播"),
                app.Subtitle(2, 1.0, 1.5, "ja", "捧哏"),
            ]
        )

        self.assertEqual(text, "[主播] Hallo\n[捧哏] ja\n")


if __name__ == "__main__":
    unittest.main()
