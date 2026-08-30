from __future__ import annotations

import unittest
from types import SimpleNamespace

import app


def segment(
    text: str,
    *,
    no_speech_prob: float = 0.05,
    avg_logprob: float = -0.15,
    compression_ratio: float = 1.1,
):
    return SimpleNamespace(
        text=text,
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
        compression_ratio=compression_ratio,
    )


class AsrHallucinationTests(unittest.TestCase):
    def test_removes_consecutive_zdf_hallucinations_from_end(self) -> None:
        logs: list[str] = []
        source = [
            segment("Das Produkt verschwand langsam wieder vom Markt."),
            segment("Untertitelung des ZDF für heute."),
            segment("Untertitelung des ZDF für zdf."),
        ]
        filtered = app.filter_trailing_asr_hallucinations(source, logs.append)
        self.assertEqual([item.text for item in filtered], [source[0].text])
        self.assertTrue(any("共移除 2" in line for line in logs))
        self.assertTrue(any("Untertitelung des ZDF" in line for line in logs))

    def test_removes_low_confidence_silence_hallucination_at_end(self) -> None:
        logs: list[str] = []
        source = [
            segment("A real sentence."),
            segment("Random tail.", no_speech_prob=0.82, avg_logprob=-1.2),
        ]
        filtered = app.filter_trailing_asr_hallucinations(source, logs.append)
        self.assertEqual(len(filtered), 1)
        self.assertIn("静音概率高", logs[0])

    def test_does_not_remove_normal_final_speech(self) -> None:
        logs: list[str] = []
        source = [
            segment("Untertitelung des ZDF für heute."),
            segment("This is genuinely spoken at the end."),
        ]
        filtered = app.filter_trailing_asr_hallucinations(source, logs.append)
        self.assertEqual(filtered, source)
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
