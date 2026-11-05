import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


class OnsetLeadTests(unittest.TestCase):
    def test_lead_never_overlaps_previous_cue_or_goes_negative(self) -> None:
        subs = [
            app.Subtitle(1, 0.02, 2.5, "a"),
            app.Subtitle(2, 2.55, 4.0, "b"),
            app.Subtitle(3, 4.5, 6.0, "c"),
        ]
        result, changed = app.apply_onset_lead(subs, 0.1)
        self.assertEqual([round(s.start, 3) for s in result], [0.0, 2.52, 4.4])
        self.assertEqual(changed, 3)
        self.assertEqual([s.end for s in result], [2.5, 4.0, 6.0])

    def test_zero_lead_is_noop(self) -> None:
        subs = [app.Subtitle(1, 1.0, 2.0, "a")]
        self.assertEqual(app.apply_onset_lead(subs, 0.0), (subs, 0))

    def test_config_value_is_clamped(self) -> None:
        self.assertEqual(app.onset_lead_from_config({}), app.DEFAULT_ONSET_LEAD)
        self.assertEqual(app.onset_lead_from_config({"onset_lead": "abc"}), app.DEFAULT_ONSET_LEAD)
        self.assertEqual(app.onset_lead_from_config({"onset_lead": 5}), 1.0)
        self.assertEqual(app.onset_lead_from_config({"onset_lead": -1}), 0.0)


class WhisperXOnsetPullbackTests(unittest.TestCase):
    def run_alignment(self, piece_start: float, sub_start: float) -> float:
        subtitles = [app.Subtitle(1, sub_start, sub_start + 2.0, "悪性")]

        def fake_align(transcript, *_args, **_kwargs):
            return {"segments": [{
                "id": 1, "start": piece_start, "end": sub_start + 1.9,
                "words": [{"word": "悪性", "start": piece_start, "end": sub_start + 1.9, "score": 0.9}],
            }]}

        fake_whisperx = SimpleNamespace(
            load_audio=lambda _path: [0.0] * 16000,
            load_align_model=lambda **_kwargs: (object(), {"language": "ja"}),
            align=fake_align,
        )
        with (
            patch.dict(sys.modules, {"whisperx": fake_whisperx}),
            patch.object(app, "add_bundled_ffmpeg_path"),
            patch.object(app, "resolve_torch_device", return_value="cpu"),
        ):
            aligned, _words = app.run_whisperx_alignment(
                "audio.wav", subtitles, "ja", "cpu", lambda _line: None, align_model_cache={}
            )
        return aligned[0].start

    def test_late_whisperx_onset_falls_back_to_earlier_asr_start(self) -> None:
        self.assertAlmostEqual(self.run_alignment(piece_start=4.34, sub_start=4.06), 4.06)

    def test_far_later_onset_is_trusted(self) -> None:
        self.assertAlmostEqual(self.run_alignment(piece_start=5.0, sub_start=4.0), 5.0)

    def test_earlier_whisperx_onset_is_kept(self) -> None:
        self.assertAlmostEqual(self.run_alignment(piece_start=3.9, sub_start=4.0), 3.9)


if __name__ == "__main__":
    unittest.main()
