"""提交前代码审查发现的问题的回归测试。"""

import os
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from PySide6.QtWidgets import QApplication

import ai_transport
import app
import qt_app


class TailCompressionTests(unittest.TestCase):
    def assert_floors(self, subtitles, audio_end, min_duration=0.12, min_gap=0.02):
        for sub in subtitles:
            self.assertGreaterEqual(round(sub.end - sub.start, 6), min_duration)
            self.assertLessEqual(sub.end, audio_end)
        for prev, cur in zip(subtitles, subtitles[1:]):
            self.assertGreaterEqual(round(cur.start - prev.end, 6), min_gap)

    def test_keeps_minimum_duration_and_gap(self) -> None:
        subs = [app.Subtitle(1, 8.0, 9.95, "a"), app.Subtitle(2, 9.97, 10.09, "b")]
        fixed, _ = app.repair_subtitle_timings(subs, audio_end=10.0)
        self.assert_floors(fixed, 10.0)

    def test_many_cues_past_audio_end(self) -> None:
        subs = [app.Subtitle(i, 9.0 + i * 0.3, 9.25 + i * 0.3, "x") for i in range(1, 6)]
        fixed, _ = app.repair_subtitle_timings(subs, audio_end=10.0)
        self.assert_floors(fixed, 10.0)
        self.assertEqual([s.index for s in fixed], [1, 2, 3, 4, 5])

    def test_no_room_at_all_still_stays_inside_audio(self) -> None:
        subs = [app.Subtitle(i, float(i), float(i) + 0.5, "x") for i in range(1, 11)]
        fixed = app.compress_subtitle_tail(subs, audio_end=0.5)
        self.assertLessEqual(fixed[-1].end, 0.5)
        self.assertEqual(sorted(s.start for s in fixed), [s.start for s in fixed])


class ParserInvisibleCharTests(unittest.TestCase):
    def test_body_joiners_are_preserved(self) -> None:
        persian = "می\u200cخواهم"
        family = "👨\u200d👩\u200d👧"
        parsed = app.parse_correction_response(f"1|{persian}\n2|{family}", {1, 2})
        self.assertEqual(parsed, {1: persian, 2: family})

    def test_noise_around_number_and_edges_is_removed(self) -> None:
        parsed = app.parse_correction_response("\ufeff\u200b1\u200c|\u200chello\u200d\n2|a\u200bb", {1, 2})
        self.assertEqual(parsed, {1: "hello", 2: "ab"})


class SecretHandlingTests(unittest.TestCase):
    SECRET = "FAKEKEY1234567890"

    def test_illegal_key_is_rejected_before_sending(self) -> None:
        transport = ai_transport.AITransport()
        for key in (self.SECRET + "\nabc", self.SECRET + "中文", self.SECRET + "\t"):
            with patch.object(transport, "_client_for") as client_for:
                with self.assertRaises(ai_transport.AIRequestError) as ctx:
                    transport.post_json("https://example.test/v1", {}, key, 5)
            client_for.assert_not_called()
            self.assertFalse(ctx.exception.retryable)
            self.assertNotIn("FAKEKEY", str(ctx.exception))

    def test_transport_error_chain_does_not_expose_key(self) -> None:
        transport = ai_transport.AITransport()

        class FakeClient:
            def post(self, *_args, **_kwargs):
                raise httpx.ConnectError(f"boom Bearer {SecretHandlingTests.SECRET}")

        with patch.object(transport, "_client_for", return_value=FakeClient()):
            try:
                transport.post_json("https://example.test/v1", {}, self.SECRET, 5)
            except ai_transport.AIRequestError:
                formatted = traceback.format_exc()
            else:
                self.fail("应当抛出 AIRequestError")
        self.assertNotIn(self.SECRET, formatted)


class MediaPathEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qapp = QApplication.instance() or QApplication([])

    def test_typing_media_path_regenerates_output_path(self) -> None:
        with patch.object(app, "load_settings", return_value={}), patch.object(app, "save_settings"):
            window = qt_app.MainWindow()
        self.addCleanup(window.deleteLater)
        window.audio_path.setText("E:/a/first.mp4")
        window.update_output_path()
        window.audio_path.setText("E:/b/second.mp4")
        window.audio_path.textEdited.emit("E:/b/second.mp4")
        output = Path(window.output_path.text())
        self.assertEqual(output.parent, Path("E:/b"))
        self.assertTrue(output.name.startswith("second."))
        self.assertFalse(window.output_path_custom)


if __name__ == "__main__":
    unittest.main()
