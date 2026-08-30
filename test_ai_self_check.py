from __future__ import annotations

import unittest
from unittest.mock import patch

import app


class AiSelfCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batch = [
            app.Subtitle(1, 0.0, 1.0, "The oldest technologies remain unexplained."),
            app.Subtitle(2, 1.0, 2.0, "Greek fire could burn even on water."),
        ]
        self.marker = "__SRTMATCHER_COMPLETE_1_2__"

    def valid_completion(self) -> app.AICompletion:
        return app.AICompletion(
            content=(
                "1|The oldest technologies remain unexplained.\n"
                "2|Greek fire could burn even on water.\n"
                f"{self.marker}"
            ),
            finish_reason="stop",
        )

    def test_short_last_line_is_rejected(self) -> None:
        mapping = {
            1: "The oldest technologies remain unexplained.",
            2: "The",
        }
        invalid = app.invalid_correction_indexes(self.batch, mapping)
        self.assertIn(2, invalid)
        self.assertIn("长度", invalid[2])

    def test_missing_marker_retries_then_accepts_complete_result(self) -> None:
        truncated = app.AICompletion(
            content=(
                "1|The oldest technologies remain unexplained.\n"
                "2|The"
            ),
            finish_reason="stop",
        )
        logs: list[str] = []
        with (
            patch.object(
                app,
                "call_openai_compatible",
                side_effect=[truncated, self.valid_completion()],
            ) as request,
            patch.object(app.time, "sleep"),
        ):
            mapping = app.correct_ai_batch_strict(
                self.batch, "url", "key", "model", "prompt", logs.append, attempts=2
            )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(mapping[2], "Greek fire could burn even on water.")
        self.assertTrue(any("缺少批次完成标记" in line for line in logs))
        self.assertTrue(any("疑似截断/改写" in line for line in logs))
        self.assertTrue(any("AI 自检通过" in line for line in logs))

    def test_length_finish_reason_retries_even_when_rows_and_marker_exist(self) -> None:
        length_limited = self.valid_completion()
        length_limited.finish_reason = "length"
        logs: list[str] = []
        with (
            patch.object(
                app,
                "call_openai_compatible",
                side_effect=[length_limited, self.valid_completion()],
            ) as request,
            patch.object(app.time, "sleep"),
        ):
            mapping = app.correct_ai_batch_strict(
                self.batch, "url", "key", "model", "prompt", logs.append, attempts=2
            )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(mapping), 2)
        self.assertTrue(any("finish_reason=length" in line for line in logs))

    def test_repeated_truncation_on_single_item_stops_without_result(self) -> None:
        single = [self.batch[0]]
        marker = "__SRTMATCHER_COMPLETE_1_1__"
        truncated = app.AICompletion(
            content=f"1|The\n{marker}",
            finish_reason="stop",
        )
        with (
            patch.object(
                app,
                "call_openai_compatible",
                side_effect=[truncated, truncated],
            ),
            patch.object(app.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "避免写入截断结果"):
                app.correct_ai_batch_strict(
                    single, "url", "key", "model", "prompt", lambda _line: None, attempts=2
                )


if __name__ == "__main__":
    unittest.main()
