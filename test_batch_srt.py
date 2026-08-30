from __future__ import annotations

import sys
import tempfile
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


class BatchSrtTests(unittest.TestCase):
    def test_mixed_language_batch_runs_alignment_and_writes_srt(self) -> None:
        FakeWhisperModel.init_count = 0
        alignment_calls: list[tuple[str, bool, int]] = []

        def fake_alignment(
            audio_path,
            subtitles,
            language_code,
            device_choice,
            log,
            **kwargs,
        ):
            alignment_calls.append(
                (language_code, bool(kwargs.get("strict")), id(kwargs["align_model_cache"]))
            )
            aligned = [
                app.Subtitle(sub.index, sub.start + 0.05, sub.end - 0.05, sub.text, sub.speaker)
                for sub in subtitles
            ]
            return aligned, {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "media"
            output_dir = root / "srt"
            nested = input_dir / "nested"
            nested.mkdir(parents=True)
            (input_dir / "hello.mp4").touch()
            (nested / "bonjour.mp3").touch()
            (nested / "bad.mkv").touch()
            model_dir = root / "model"
            model_dir.mkdir()
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
                patch.object(app, "run_whisperx_alignment", side_effect=fake_alignment),
                patch.object(app, "correct_subtitles_with_ai", side_effect=fake_corrector),
            ):
                result = app.run_batch_srt_job(config, lambda _message: None)

            self.assertEqual(Path(result), output_dir.resolve())
            self.assertEqual(FakeWhisperModel.init_count, 1)
            self.assertEqual([call[0] for call in alignment_calls], ["en", "fr"])
            self.assertTrue(all(call[1] for call in alignment_calls))
            self.assertEqual(len({call[2] for call in alignment_calls}), 1)
            english_srt = (output_dir / "hello.srt").read_text(encoding="utf-8-sig")
            french_srt = (output_dir / "nested" / "bonjour.srt").read_text(encoding="utf-8-sig")
            self.assertIn("00:00:00,050 --> 00:00:00,950", english_srt)
            self.assertIn("raw hello [corrected]", english_srt)
            self.assertIn("raw bonjour [corrected]", french_srt)
            report = (output_dir / app.BATCH_SRT_ERROR_FILENAME).read_text(encoding="utf-8-sig")
            self.assertIn("nested\\bad.mkv", report)

    def test_alignment_model_is_cached_for_same_language(self) -> None:
        load_calls: list[tuple[str, str]] = []

        def load_align_model(language_code, device):
            load_calls.append((language_code, device))
            return object(), {"language": language_code}

        fake_whisperx = SimpleNamespace(
            load_align_model=load_align_model,
            load_audio=lambda _path: object(),
            align=lambda transcript, *_args, **_kwargs: {"segments": transcript},
        )
        subtitles = [app.Subtitle(1, 0.0, 1.0, "hello")]
        cache: dict = {}

        with (
            patch.dict(sys.modules, {"whisperx": fake_whisperx}),
            patch.object(app, "add_bundled_ffmpeg_path"),
            patch.object(app, "resolve_torch_device", return_value="cpu"),
        ):
            app.run_whisperx_alignment(
                "one.wav", subtitles, "en", "cpu", lambda _message: None,
                align_model_cache=cache, strict=True,
            )
            app.run_whisperx_alignment(
                "two.wav", subtitles, "en", "cpu", lambda _message: None,
                align_model_cache=cache, strict=True,
            )

        self.assertEqual(load_calls, [("en", "cpu")])

    def test_same_stem_uses_extension_to_avoid_srt_collision(self) -> None:
        root = Path("C:/media")
        output = Path("C:/srt")
        first = root / "clip.mp4"
        second = root / "clip.wav"
        paths = app.batch_output_paths([first, second], root, output, "srt")
        self.assertEqual(paths[first].name, "clip.mp4.srt")
        self.assertEqual(paths[second].name, "clip.wav.srt")


if __name__ == "__main__":
    unittest.main()
