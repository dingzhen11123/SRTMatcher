from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app


class FakeWhisperModel:
    init_count = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).init_count += 1

    def transcribe(self, media_path: str, **kwargs):
        path = Path(media_path)
        if path.stem == "bad":
            raise RuntimeError("broken media")
        language = "fr" if path.stem == "bonjour" else "en"
        segments = [
            SimpleNamespace(start=0.0, end=1.0, text=f"raw {path.stem}"),
            SimpleNamespace(start=1.0, end=2.0, text="second line"),
        ]
        return iter(segments), SimpleNamespace(language=language)


def fake_corrector(subtitles, base_url, api_key, model, system_prompt, log):
    return [
        app.Subtitle(sub.index, sub.start, sub.end, f"{sub.text} [corrected]", sub.speaker)
        for sub in subtitles
    ]


class BatchTxtTests(unittest.TestCase):
    def test_mixed_language_batch_reuses_model_and_continues_after_failure(self) -> None:
        FakeWhisperModel.init_count = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "media"
            output_dir = root / "text"
            nested = input_dir / "nested"
            nested.mkdir(parents=True)
            (input_dir / "hello.mp4").touch()
            (nested / "bonjour.mp3").touch()
            (nested / "bad.mkv").touch()
            (input_dir / "ignore.txt").touch()
            model_dir = root / "model"
            model_dir.mkdir()
            logs: list[str] = []
            progress: list[tuple[int, str]] = []
            config = {
                "batch_input_dir": str(input_dir),
                "batch_output_dir": str(output_dir),
                "batch_recursive": True,
                "batch_skip_existing": True,
                "model_path": str(model_dir),
                "device": "cpu",
                "compute_type": "int8",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "ai_model": "test-model",
            }

            with (
                patch.dict(
                    sys.modules,
                    {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel)},
                ),
                patch.object(app, "prepare_model_files", return_value=str(model_dir)),
                patch.object(app, "add_cuda_dll_paths"),
                patch.object(app, "correct_subtitles_with_ai", side_effect=fake_corrector),
            ):
                result = app.run_batch_txt_job(
                    config, logs.append, lambda value, message: progress.append((value, message))
                )

            self.assertEqual(Path(result), output_dir.resolve())
            self.assertEqual(FakeWhisperModel.init_count, 1)
            self.assertIn("raw hello [corrected]", (output_dir / "hello.txt").read_text(encoding="utf-8-sig"))
            self.assertIn(
                "raw bonjour [corrected]",
                (output_dir / "nested" / "bonjour.txt").read_text(encoding="utf-8-sig"),
            )
            report = (output_dir / app.BATCH_ERROR_FILENAME).read_text(encoding="utf-8-sig")
            self.assertIn("nested\\bad.mkv", report)
            self.assertTrue(any("检测语言: fr" in line for line in logs))
            self.assertTrue(any("失败但继续处理" in line for line in logs))
            self.assertEqual(progress[-1][0], 100)

    def test_discovery_respects_recursive_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "top.mp4").touch()
            nested = root / "nested"
            nested.mkdir()
            (nested / "inside.mp4").touch()
            self.assertEqual(len(app.discover_batch_media(str(root), recursive=False)), 1)
            self.assertEqual(len(app.discover_batch_media(str(root), recursive=True)), 2)

    def test_same_stem_uses_extension_to_avoid_txt_collision(self) -> None:
        root = Path("C:/media")
        output = Path("C:/text")
        first = root / "clip.mp4"
        second = root / "clip.wav"
        paths = app.batch_txt_output_paths([first, second], root, output)
        self.assertEqual(paths[first].name, "clip.mp4.txt")
        self.assertEqual(paths[second].name, "clip.wav.txt")


if __name__ == "__main__":
    unittest.main()
