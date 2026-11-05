from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import ai_transport
from ai_transport import AIRequestError, AITransport
from model_runtime import ModelRuntime
from task_runtime import CancellationToken, TaskCancelled, sleep_cancellable, task_context

S = app.Subtitle


def fake_whisperx(align):
    return SimpleNamespace(
        load_align_model=lambda language_code, device: (object(), {"language": language_code}),
        load_audio=lambda _path: object(),
        align=align,
    )


def run_alignment(align, subtitles, strict=False, logs=None):
    logs = [] if logs is None else logs
    with (
        patch.dict(sys.modules, {"whisperx": fake_whisperx(align)}),
        patch.object(app, "add_bundled_ffmpeg_path"),
        patch.object(app, "resolve_torch_device", return_value="cpu"),
    ):
        return app.run_whisperx_alignment(
            "a.wav", subtitles, "en", "cpu", logs.append, align_model_cache={}, strict=strict
        )


class WhisperxFailedPieceTests(unittest.TestCase):
    """#1 padded window without timed words is an alignment failure."""

    @staticmethod
    def padded_failure(transcript, *_args, **_kwargs):
        return {"segments": [dict(item, words=[]) for item in transcript]}

    def test_untimed_piece_keeps_original_timing(self) -> None:
        subs = [S(1, 1.0, 2.0, "2024"), S(2, 2.05, 3.0, "hello")]

        def align(transcript, *_args, **_kwargs):
            first, second = transcript
            return {
                "segments": [
                    dict(first, words=[{"word": "2024"}]),
                    dict(second, start=2.1, end=2.9, words=[{"word": "hello", "start": 2.1, "end": 2.9}]),
                ]
            }

        aligned, words = run_alignment(align, subs)
        self.assertEqual((aligned[0].start, aligned[0].end), (1.0, 2.0))
        self.assertIsNone(words[1])
        # 对齐起点比原起点晚不到 0.5s 时保留更早的原起点（防止切掉句首）。
        self.assertEqual((aligned[1].start, aligned[1].end), (2.05, 2.9))
        self.assertEqual(words[2][0].word, "hello")

    def test_strict_rejects_all_padded_failures(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "没有更新任何字幕时间轴"):
            run_alignment(self.padded_failure, [S(1, 1.0, 2.0, "2024")], strict=True)


class FakeWhisperModel:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def transcribe(self, media_path: str, **kwargs):
        segments = [
            SimpleNamespace(start=0.0, end=1.0, text="Hello there"),
            SimpleNamespace(start=1.0, end=2.0, text="Second line"),
        ]
        return iter(segments), SimpleNamespace(language="en")


class RunSrtJobDiarizationTests(unittest.TestCase):
    """#2 validate diarization inputs early; keep the main output on failure."""

    def make_config(self, **overrides) -> tuple[dict, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        media = root / "talk.mp4"
        media.touch()
        output = root / "talk.srt"
        config = {
            "audio_path": str(media),
            "output_path": str(output),
            "model_path": str(root),
            "mode": "transcribe",
            "device": "cpu",
            "compute_type": "int8",
            "language": "en",
            "ai_enabled": False,
            "diarization_enabled": True,
            "hf_token": "hf_test",
            "min_speakers": "",
            "max_speakers": "",
        }
        config.update(overrides)
        return config, output

    def run_job(self, config: dict, logs: list[str], diarization=None):
        with (
            patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel)}),
            patch.object(app, "prepare_model_files", return_value=config["model_path"]) as prepare,
            patch.object(app, "add_cuda_dll_paths"),
            patch.object(app, "run_speaker_diarization", side_effect=diarization),
        ):
            try:
                return app.run_srt_job(config, logs.append)
            finally:
                self.prepare_calls = prepare.call_count

    def test_invalid_settings_fail_before_asr(self) -> None:
        cases = [
            ({"hf_token": "  "}, "Hugging Face Token"),
            ({"min_speakers": "two"}, "最少说话人数必须是整数"),
            ({"max_speakers": "1.5"}, "最多说话人数必须是整数"),
            ({"min_speakers": "4", "max_speakers": "2"}, "不能大于"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                config, output = self.make_config(**overrides)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_job(config, [])
                self.assertEqual(self.prepare_calls, 0)
                self.assertFalse(output.exists())

    def test_diarization_failure_keeps_main_output(self) -> None:
        config, output = self.make_config(min_speakers="1", max_speakers="3")
        with self.assertRaises(RuntimeError) as caught:
            self.run_job(config, [], diarization=RuntimeError("pyannote exploded"))
        self.assertIn("pyannote exploded", str(caught.exception))
        self.assertIn(str(output), str(caught.exception))
        text = output.read_text(encoding="utf-8-sig")
        self.assertIn("Hello there", text)
        self.assertNotIn("[", text)

    def test_diarization_success_rewrites_output_with_speakers(self) -> None:
        config, output = self.make_config()
        seen: list[tuple] = []

        def diarize(audio, token, device, min_speakers, max_speakers, log):
            seen.append((token, min_speakers, max_speakers))
            return [(0.0, 5.0, "SPEAKER_00")]

        self.run_job(config, [], diarization=diarize)
        self.assertEqual(seen, [("hf_test", None, None)])
        self.assertIn("[SPEAKER_00] Hello there", output.read_text(encoding="utf-8-sig"))


class TextBoundaryTests(unittest.TestCase):
    """#3 punctuation-only cues, first-cue punctuation, contiguous numbering."""

    def test_punctuation_only_cue_is_dropped_and_numbering_is_contiguous(self) -> None:
        subs = [S(1, 0.0, 1.0, "你好世界"), S(2, 1.0, 1.5, "！！"), S(3, 1.5, 2.5, "再见朋友")]
        repaired = app.repair_subtitle_text_boundaries(subs)
        self.assertEqual([sub.text for sub in repaired], ["你好世界！！", "再见朋友"])
        srt = app.srt_from_subtitles(repaired)
        self.assertEqual(re.findall(r"(?m)^(\d+)$", srt), ["1", "2"])
        self.assertNotIn("\n\n\n", srt)

    def test_first_cue_keeps_leading_punctuation(self) -> None:
        repaired = app.repair_subtitle_text_boundaries([S(1, 0, 1, "...Hi"), S(2, 1, 2, ", there")])
        self.assertEqual([sub.text for sub in repaired], ["...Hi,", "there"])

    def test_word_export_sentence_ids_follow_srt_numbering(self) -> None:
        subs = app.repair_subtitle_text_boundaries(
            [S(1, 0.0, 1.0, "a"), S(2, 1.0, 1.2, "。"), S(3, 1.2, 2.0, "b")]
        )
        words = {
            1: [app.WordTimestamp(1, "a", 0.1, 0.5)],
            2: [app.WordTimestamp(2, "。", 1.0, 1.1)],
            3: [app.WordTimestamp(3, "b", 1.3, 1.8)],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = app.export_word_timestamps(
                subs, words, str(Path(temp_dir) / "x.srt"), "json", lambda _line: None
            )
            rows = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual([(row["sentence_id"], row["word"]) for row in rows], [(1, "a"), (2, "b")])

    def test_empty_original_returned_empty_is_not_an_issue(self) -> None:
        self.assertEqual(app.correction_text_issue("", ""), "")
        self.assertEqual(app.correction_text_issue("text", ""), "返回文本为空")


class AudioEndTimingTests(unittest.TestCase):
    """#4 no cue ends after audio_end; trailing script tokens get a duration."""

    def assert_sane(self, subs, audio_end) -> None:
        previous_end = 0.0
        for sub in subs:
            self.assertGreaterEqual(sub.start, previous_end - 1e-9)
            self.assertGreater(sub.end, sub.start)
            self.assertLessEqual(sub.end, audio_end + 1e-9)
            previous_end = sub.end

    def test_tail_is_compressed_within_audio_end(self) -> None:
        subs = [S(i + 1, 9.0, 9.4, f"s{i}") for i in range(5)]
        repaired, changes = app.repair_subtitle_timings(subs, audio_end=10.0)
        self.assertGreater(changes, 0)
        self.assert_sane(repaired, 10.0)

    def test_everything_after_audio_end_is_squeezed_in(self) -> None:
        subs = [S(1, 11.0, 12.0, "a"), S(2, 12.0, 13.0, "b")]
        repaired, _ = app.repair_subtitle_timings(subs, audio_end=10.0)
        self.assert_sane(repaired, 10.0)

    def test_untouched_when_inside_audio(self) -> None:
        subs = [S(1, 0.0, 1.0, "a"), S(2, 1.5, 2.0, "b")]
        repaired, changes = app.repair_subtitle_timings(subs, audio_end=10.0)
        self.assertEqual(changes, 0)
        self.assertEqual([(s.start, s.end) for s in repaired], [(0.0, 1.0), (1.5, 2.0)])

    def test_trailing_unmatched_script_tokens_get_duration(self) -> None:
        asr = [app.Token(ch, i, i + 1, i * 0.4, i * 0.4 + 0.3) for i, ch in enumerate("你好世界")]
        tokens = app.assign_script_times("你好世界甲乙丙丁", asr, audio_end=3.0)
        trailing = tokens[4:]
        self.assertTrue(all(t.end_time > t.start_time for t in trailing))
        self.assertLessEqual(trailing[-1].end_time, 3.0)
        bounded = app.assign_script_times("你好世界甲乙丙丁", asr, audio_end=1.8)
        self.assertTrue(all(t.end_time > t.start_time for t in bounded[4:]))
        self.assertLessEqual(bounded[-1].end_time, 1.8 + 1e-9)


class WordOrderTests(unittest.TestCase):
    """#5 word order is preserved."""

    def test_untimed_and_clamped_words_stay_in_place(self) -> None:
        sub = [S(1, 0.0, 2.0, "a b c d")]
        words = {
            1: [
                app.WordTimestamp(1, "a", 0.1, 0.3),
                app.WordTimestamp(1, "b", None, None),
                app.WordTimestamp(1, "c", 2.5, 2.9),
                app.WordTimestamp(1, "d", 1.5, 1.9),
            ]
        }
        repaired = app.repair_word_timestamps(sub, words, lambda _line: None)[1]
        self.assertEqual([w.word for w in repaired], ["a", "b", "c", "d"])
        self.assertIsNone(repaired[1].start)
        self.assertIsNone(repaired[2].start)
        self.assertEqual((repaired[3].start, repaired[3].end), (1.5, 1.9))


class AsrTokenTests(unittest.TestCase):
    """#6 None word times fall back to the segment, not 0.0."""

    def test_none_word_times_use_segment_bounds(self) -> None:
        segment = SimpleNamespace(
            start=100.0, end=102.0, text=" hello world",
            words=[SimpleNamespace(word=" hello", start=None, end=None),
                   SimpleNamespace(word=" world", start=101.0, end=101.5)],
        )
        _, tokens = app.extract_asr_tokens([segment])
        self.assertEqual(tokens[0].start_time, 100.0)
        self.assertEqual(tokens[0].end_time, 102.0)
        self.assertEqual(tokens[1].start_time, 101.0)


class CudaFallbackTests(unittest.TestCase):
    """#7 CPU fallback drops every CUDA model."""

    def tearDown(self) -> None:
        app._TORCH_CUDA_DISABLED_FOR_PROCESS = False
        app._TORCH_CUDA_PROBE_RESULT = None

    def test_all_cuda_models_are_released(self) -> None:
        runtime = ModelRuntime()
        runtime.align_models.update({("en", "cuda"): 1, ("de", "cuda"): 2, ("fr", "cpu"): 3})
        runtime._diarization.update({("repo", "cuda"): 4, ("repo", "cpu"): 5})
        emptied: list[bool] = []
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True, empty_cache=lambda: emptied.append(True))
        )
        with patch.object(app, "MODEL_RUNTIME", runtime), patch.dict(sys.modules, {"torch": fake_torch}):
            app.disable_torch_cuda_for_process()
        self.assertEqual(list(runtime.align_models), [("fr", "cpu")])
        self.assertEqual(list(runtime._diarization), [("repo", "cpu")])
        self.assertTrue(emptied)


class WordWarningTests(unittest.TestCase):
    """#8 warn when AI changed text after word timestamps were computed."""

    def test_warns_once_for_changed_sentences(self) -> None:
        subs = [S(1, 0, 1, "fixed"), S(2, 1, 2, "same")]
        words = {1: [app.WordTimestamp(1, "fixd", 0, 1)], 2: [app.WordTimestamp(2, "same", 1, 2)]}
        logs: list[str] = []
        app.warn_words_predate_ai_correction(subs, {1: "fixd", 2: "same"}, words, logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn("1 条", logs[0])
        logs.clear()
        app.warn_words_predate_ai_correction(subs, {1: "fixed", 2: "same"}, words, logs.append)
        self.assertEqual(logs, [])


class HallucinationPrefixTests(unittest.TestCase):
    """#9 generic phrases only match at the start."""

    def test_captions_by_only_at_start(self) -> None:
        normal = SimpleNamespace(text="We read the captions by the river bank.")
        fake = SimpleNamespace(text="Captions by Example Media")
        self.assertEqual(app.asr_hallucination_reason(normal), "")
        self.assertTrue(app.asr_hallucination_reason(fake))


class AiRetryTests(unittest.TestCase):
    """#10 transient errors retry; permanent ones raise immediately."""

    def setUp(self) -> None:
        self.batch = [S(1, 0.0, 1.0, "hello world")]
        self.ok = app.AICompletion("1|hello world\n__SRTMATCHER_COMPLETE_1_1__", "stop")

    def call(self, side_effect, attempts=3):
        with (
            patch.object(app, "call_openai_compatible", side_effect=side_effect) as request,
            patch.object(app.time, "sleep") as sleep,
        ):
            try:
                return app.correct_ai_batch_strict(
                    self.batch, "u", "k", "m", "p", lambda _line: None, attempts=attempts
                )
            finally:
                self.request_count = request.call_count
                self.sleep_count = sleep.call_count

    def test_transient_errors_are_retried(self) -> None:
        errors = [
            AIRequestError("timeout", retryable=True),
            AIRequestError("HTTP 429", status_code=429, retryable=True),
            self.ok,
        ]
        self.assertEqual(self.call(errors), {1: "hello world"})
        self.assertEqual(self.request_count, 3)
        self.assertEqual(self.sleep_count, 2)

    def test_transient_errors_exhaust_attempts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "连续 2 次临时失败"):
            self.call([AIRequestError("HTTP 503", status_code=503, retryable=True)] * 2, attempts=2)

    def test_permanent_error_is_not_retried(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "API Key 无效"):
            self.call([AIRequestError("HTTP 401", status_code=401), self.ok])
        self.assertEqual(self.request_count, 1)

    def test_cancel_is_not_retried(self) -> None:
        with self.assertRaises(TaskCancelled):
            self.call([TaskCancelled("stop"), self.ok])
        self.assertEqual(self.request_count, 1)


class AiParallelFailureTests(unittest.TestCase):
    """#11 one failing batch stops the pool promptly."""

    def test_failure_cancels_pending_and_stops_in_flight(self) -> None:
        subs = [S(i, float(i), i + 1.0, f"line {i} text") for i in range(1, 101)]
        calls: dict[int, int] = {}
        second_started = threading.Event()
        lock = threading.Lock()

        def fake_call(_url, _key, _model, _prompt, content):
            first = int(re.search(r"__SRTMATCHER_COMPLETE_(\d+)_", content).group(1))
            with lock:
                calls[first] = calls.get(first, 0) + 1
            if first == 1:
                second_started.wait(2)
                raise RuntimeError("boom")
            second_started.set()
            return app.AICompletion("garbage", "stop")  # forces a retry wait

        started = time.perf_counter()
        with patch.object(app, "call_openai_compatible", side_effect=fake_call):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                app.correct_subtitles_with_ai(subs, "u", "k", "m", "p", lambda _line: None)
        self.assertLess(time.perf_counter() - started, 1.5)
        time.sleep(0.3)
        self.assertEqual(calls.get(21), 1)
        self.assertTrue(all(count <= 1 for count in calls.values()))
        self.assertLessEqual(len(calls), 3)


class CorrectionTextIssueTests(unittest.TestCase):
    """#12 CJK spacing changes are not flagged."""

    def test_cjk_space_removal_is_ok(self) -> None:
        self.assertEqual(app.correction_text_issue("我今天 去了 北京 看了 故宫", "我今天去了北京看了故宫"), "")

    def test_english_truncation_still_flagged(self) -> None:
        self.assertTrue(app.correction_text_issue("Greek fire could burn even on water.", "Greek"))


class ParseCorrectionTests(unittest.TestCase):
    """#13 robust response parsing."""

    def test_variants(self) -> None:
        reply = "\n".join([
            "\ufeff1|first",
            "2｜second",
            "**3|third**",
            "`4|fourth`",
            "\u200b5 | fifth",
            "①|circled",
            "#6 context",
            "99|unexpected",
            "__DONE__",
        ])
        self.assertEqual(
            app.parse_correction_response(reply, {1, 2, 3, 4, 5, 6}),
            {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"},
        )


class CancellableSleepTests(unittest.TestCase):
    """#14 retry sleep wakes on cancel."""

    def test_cancel_wakes_sleep(self) -> None:
        token = CancellationToken()
        threading.Timer(0.1, token.cancel).start()
        started = time.perf_counter()
        with task_context(token), self.assertRaises(TaskCancelled):
            app.wait_before_ai_retry(5)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_stop_event_wakes_sleep(self) -> None:
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        started = time.perf_counter()
        self.assertTrue(sleep_cancellable(5, stop))
        self.assertLess(time.perf_counter() - started, 1.0)


class OpenAiResponseShapeTests(unittest.TestCase):
    """#15 null message and content-part lists."""

    def call(self, response: dict) -> app.AICompletion:
        with patch.object(app.AI_TRANSPORT, "post_json", return_value=response):
            return app.call_openai_compatible("https://example.test/v1", "k", "m", "p", "u")

    def test_null_message(self) -> None:
        result = self.call({"choices": [{"message": None, "finish_reason": "stop"}]})
        self.assertEqual(result.content, "")

    def test_content_parts(self) -> None:
        parts = [{"type": "text", "text": "1|a"}, {"type": "text", "text": "\n__M__"}, {"type": "image_url"}]
        result = self.call({"choices": [{"message": {"content": parts}, "finish_reason": "stop"}]})
        self.assertEqual(result.content, "1|a\n__M__")


class SettingsFileTests(unittest.TestCase):
    """#16 tolerant reads and atomic writes."""

    def test_read_bom_gbk_and_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "s.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"language": "zh"}).encode())
            self.assertEqual(app.read_json_file(path), {"language": "zh"})
            path.write_bytes(json.dumps({"ai_glossary": "中文"}, ensure_ascii=False).encode("gbk"))
            self.assertEqual(app.read_json_file(path), {"ai_glossary": "中文"})
            path.write_bytes(b"\xff\xfe\x00garbage")
            self.assertEqual(app.read_json_file(path), {})

    def test_save_settings_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main, portable = root / "appdata" / "settings.json", root / "settings.json"
            with patch.object(app, "SETTINGS_PATH", main), patch.object(app, "PORTABLE_SETTINGS_PATH", portable):
                app.save_settings({"language": "en"})
                self.assertEqual(app.read_json_file(main), {"language": "en"})
                self.assertEqual(app.read_json_file(portable), {"language": "en"})
                with patch.object(app.os, "replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        app.save_settings({"language": "de"})
                self.assertEqual(app.read_json_file(main), {"language": "en"})
            self.assertEqual([p.name for p in main.parent.iterdir()], ["settings.json"])


class ModelFilesTests(unittest.TestCase):
    """#17 same completeness check as model_file_status; cancellable download hook."""

    def test_missing_vocabulary_triggers_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json"):
                (root / name).write_text("x")
            with patch.object(app, "download_faster_whisper_model") as download:
                app.prepare_model_files(str(root), lambda _line: None)
                self.assertTrue(download.called)
                (root / "vocabulary.txt").write_text("x")
                download.reset_mock()
                app.prepare_model_files(str(root), lambda _line: None)
                self.assertFalse(download.called)
            self.assertTrue(app.model_file_status(str(root))["ready"])

    def test_download_progress_hook_honours_cancel(self) -> None:
        token = CancellationToken()

        def snapshot_download(repo_id, tqdm_class, **_kwargs):
            bar = tqdm_class(total=10, disable=True)
            bar.update(1)
            token.cancel()
            bar.update(1)
            return "unreachable"

        modules = {
            "huggingface_hub": SimpleNamespace(snapshot_download=snapshot_download),
            "faster_whisper": SimpleNamespace(utils=SimpleNamespace(_MODELS={})),
        }
        with patch.dict(sys.modules, modules), task_context(token), self.assertRaises(TaskCancelled):
            app.download_faster_whisper_model("m", Path("."), lambda _line: None)


class BatchOutputPathTests(unittest.TestCase):
    """#18 de-duplicated names are re-checked case-insensitively."""

    def test_second_level_collision_gets_numeric_suffix(self) -> None:
        root, out = Path("C:/in"), Path("C:/out")
        files = [root / "a.mp4", root / "a.wav", root / "A.mp4.mp3"]
        paths = app.batch_output_paths(files, root, out, "srt")
        names = [paths[f].name for f in files]
        self.assertEqual(len({name.casefold() for name in names}), 3)
        self.assertEqual(paths[files[2]].name, "A.mp4.srt")
        self.assertEqual(paths[files[0]].name, "a.mp4_2.srt")
        self.assertEqual(paths[files[1]].name, "a.wav.srt")


class AiTransportErrorTests(unittest.TestCase):
    """#19 API key redaction, truncation and retry classification."""

    def post(self, response=None, exc=None):
        import httpx

        class Client:
            def post(self, url, **_kwargs):
                if exc is not None:
                    raise exc
                response.request = httpx.Request("POST", url)
                return response

        transport = AITransport()
        with patch.object(transport, "_client_for", return_value=Client()):
            return transport.post_json("https://example.test/v1/chat/completions", {}, "sk-secret-key", 5)

    def test_error_detail_is_redacted_and_truncated(self) -> None:
        import httpx

        body = "bad key sk-secret-key " + "x" * 2000
        with self.assertRaises(AIRequestError) as caught:
            self.post(httpx.Response(401, text=body))
        message = str(caught.exception)
        self.assertNotIn("sk-secret-key", message)
        self.assertIn("***", message)
        self.assertLess(len(message), ai_transport.ERROR_DETAIL_LIMIT + 100)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(caught.exception.retryable)

    def test_retryable_classification(self) -> None:
        import httpx

        for status in (429, 500, 503):
            with self.subTest(status=status), self.assertRaises(AIRequestError) as caught:
                self.post(httpx.Response(status, text="busy"))
            self.assertTrue(caught.exception.retryable)
        with self.assertRaises(AIRequestError) as caught:
            self.post(exc=httpx.ReadTimeout("timed out"))
        self.assertTrue(caught.exception.retryable)
        with self.assertRaises(AIRequestError) as caught:
            self.post(exc=httpx.ConnectError("refused"))
        self.assertTrue(caught.exception.retryable)


class RunCaptureTests(unittest.TestCase):
    """#20 any OSError means the command is unavailable."""

    def test_permission_error(self) -> None:
        with patch.object(app.subprocess, "run", side_effect=PermissionError("denied")):
            self.assertEqual(app.run_capture(["nvidia-smi"]), (127, ""))


class InstallTorchTests(unittest.TestCase):
    """#21 refuse to reinstall torch while it is loaded."""

    def test_refuses_when_torch_loaded(self) -> None:
        with (
            patch.dict(sys.modules, {"torch": SimpleNamespace()}),
            patch.object(app, "recommended_runtime_plan") as plan,
            patch.object(app.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "重启软件"):
                app.install_recommended_torch(lambda _line: None)
        plan.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
