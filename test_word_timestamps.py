from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app


class WordTimestampExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subtitles = [
            app.Subtitle(1, 1.0, 1.6, "这是左边"),
            app.Subtitle(2, 2.0, 2.5, "缺失"),
        ]
        self.words = {
            1: [
                app.WordTimestamp(1, "这是", 0.9, 1.35, 0.95),
                app.WordTimestamp(1, "左边", 1.3, 1.7, 0.9),
            ],
            2: None,
        }
        self.logs: list[str] = []

    def test_repair_clamps_to_sentence_and_removes_overlap(self) -> None:
        repaired = app.repair_word_timestamps(self.subtitles, self.words, self.logs.append)
        first, second = repaired[1]
        self.assertGreaterEqual(first.start, self.subtitles[0].start)
        self.assertLessEqual(second.end, self.subtitles[0].end)
        self.assertLessEqual(first.end, second.start)

    def test_json_and_srt_exports(self) -> None:
        repaired = app.repair_word_timestamps(self.subtitles, self.words, self.logs.append)
        with tempfile.TemporaryDirectory() as temp_dir:
            sentence_path = str(Path(temp_dir) / "sample.aligned.srt")
            json_path = app.export_word_timestamps(
                self.subtitles, repaired, sentence_path, "json", self.logs.append
            )
            rows = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(json_path.name, "sample.aligned.words.json")
            self.assertEqual(rows[-1], {"sentence_id": 2, "words": None})
            self.assertEqual(rows[0]["sentence_id"], 1)
            self.assertIn("score", rows[0])

            srt_path = app.export_word_timestamps(
                self.subtitles, repaired, sentence_path, "srt", self.logs.append
            )
            text = srt_path.read_text(encoding="utf-8-sig")
            self.assertEqual(srt_path.name, "sample.aligned.words.srt")
            self.assertIn("1\n00:00:01,000 -->", text)
            self.assertIn("\n这是\n", text)
            self.assertNotIn("缺失", text)


if __name__ == "__main__":
    unittest.main()
