from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from asr_runtime import transcribe_options
from batch_runtime import select_requested_media
from model_runtime import ModelRuntime
from task_runtime import CancellationToken, TaskCancelled, check_cancelled, emit_task_event, task_context


class RuntimeServiceTests(unittest.TestCase):
    def test_task_context_carries_events_and_cancellation(self) -> None:
        events: list[dict] = []
        token = CancellationToken()
        with task_context(token, events.append):
            emit_task_event("stage", stage="asr")
            token.cancel()
            with self.assertRaises(TaskCancelled):
                check_cancelled()
        self.assertEqual(events, [{"type": "stage", "stage": "asr"}])

    def test_model_runtime_reuses_matching_asr_model(self) -> None:
        runtime = ModelRuntime()
        created: list[object] = []

        def factory(*_args, **_kwargs):
            model = object()
            created.append(model)
            return model

        with tempfile.TemporaryDirectory() as temp_dir:
            first, first_reused = runtime.get_asr_model(temp_dir, "cpu", "int8", factory)
            second, second_reused = runtime.get_asr_model(temp_dir, "cpu", "int8", factory)
        self.assertIs(first, second)
        self.assertFalse(first_reused)
        self.assertTrue(second_reused)
        self.assertEqual(len(created), 1)

    def test_batch_retry_filter_uses_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "a.mp4"
            second = root / "nested" / "b.wav"
            selected = select_requested_media(
                [first, second], root, ["nested/b.wav"]
            )
        self.assertEqual(selected, [second])

    def test_performance_presets_keep_recommended_default_quality(self) -> None:
        self.assertEqual(transcribe_options("fast")["beam_size"], 1)
        self.assertEqual(transcribe_options("recommended")["beam_size"], 5)
        self.assertEqual(transcribe_options("quality")["beam_size"], 8)

    def test_ai_corrections_preserve_order_with_bounded_parallelism(self) -> None:
        subtitles = [
            app.Subtitle(index, float(index), float(index + 1), f"line {index}")
            for index in range(1, 46)
        ]

        def correct(batch, *_args, **_kwargs):
            return {item.index: item.text + " corrected" for item in batch}

        with patch.object(app, "correct_ai_batch_strict", side_effect=correct) as mocked:
            corrected = app.correct_subtitles_with_ai(
                subtitles, "url", "key", "model", "prompt", lambda _line: None
            )
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual([item.index for item in corrected], list(range(1, 46)))
        self.assertTrue(all(item.text.endswith(" corrected") for item in corrected))

    def test_openai_compatible_uses_reusable_transport(self) -> None:
        response = {
            "choices": [
                {"message": {"content": "1|hello"}, "finish_reason": "stop"}
            ]
        }
        with patch.object(app.AI_TRANSPORT, "post_json", return_value=response) as post:
            result = app.call_openai_compatible(
                "https://example.test/v1", "key", "model", "prompt", "1|hello"
            )
        self.assertEqual(result.content, "1|hello")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
