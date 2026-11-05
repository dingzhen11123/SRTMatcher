"""Offscreen regression tests for GUI fixes in qt_app.py."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app as core
import qt_app
from PySide6.QtCore import QEventLoop
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from task_runtime import emit_task_event


QT_APP = QApplication.instance() or QApplication(sys.argv)


def pump(condition=None, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QT_APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if condition is not None and condition():
            return
        time.sleep(0.01)


class GuiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Never read or write the real %APPDATA% settings.
        for target, value in (
            ("load_settings", lambda: {}),
            ("save_settings", lambda data: Path("settings.json")),
        ):
            patcher = patch.object(core, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dialogs: list[tuple[str, str]] = []
        for name in ("information", "warning", "critical"):
            patcher = patch.object(
                QMessageBox, name,
                side_effect=lambda *args, _n=name, **kwargs: (
                    self.dialogs.append((_n, str(args[1]))) or QMessageBox.StandardButton.Ok
                ),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.tmp = Path(temp_dir.name)
        self.window = qt_app.MainWindow()
        self.addCleanup(self._dispose_window)

    def _dispose_window(self) -> None:
        window = self.window
        pump(lambda: not (window.worker_thread and window.worker_thread.isRunning()), 5.0)
        window.background_tasks.clear()
        window.close()
        window.deleteLater()
        pump(timeout=0.05)

    def dialog_kinds(self) -> list[str]:
        return [kind for kind, _title in self.dialogs]


class WorkerTests(unittest.TestCase):
    def test_base_exception_still_emits_terminal_signal(self) -> None:
        worker = qt_app.JobWorker({"job_type": "single"})
        failed: list[str] = []
        worker.failed.connect(failed.append)

        def boom(*_args, **_kwargs):
            raise SystemExit("stop")

        with patch.object(core, "run_srt_job", boom):
            worker.run()
        self.assertEqual(failed, ["stop"])


class ThreadingTests(GuiTestCase):
    def test_many_short_jobs_do_not_destroy_running_thread(self) -> None:
        # 回归：线程还没退出就释放 QThread 会让进程直接中止，所以这里不保留任何线程引用。
        window = self.window
        finished: list[str] = []
        with patch.object(core, "run_srt_job", lambda config, log, progress=None: "out.srt"):
            for _ in range(40):
                window.launch_worker({"job_type": "single"}, finished.append)
                pump(lambda: window.worker_thread is None, 5.0)
                self.assertIsNone(window.worker_thread)
                self.assertIsNone(window.worker)
        self.assertEqual(finished, ["out.srt"] * 40)

    def test_check_runtime_reads_widgets_on_main_thread_and_logs_via_signal(self) -> None:
        window = self.window
        window.set_combo_text(window.device, "cpu")
        calls: list[tuple[bool, bool]] = []

        def fake_report(wants_cuda: bool):
            calls.append((wants_cuda, threading.current_thread() is threading.main_thread()))
            return "REPORT", True, {"driver": "https://example.invalid/driver"}

        log_threads: list[bool] = []
        original_log = window.log

        def spy_log(message: str) -> None:
            log_threads.append(threading.current_thread() is threading.main_thread())
            original_log(message)

        window.build_runtime_report = fake_report
        window.ui_events.log.disconnect()
        window.ui_events.log.connect(spy_log)
        window.check_runtime()
        pump(lambda: window.runtime_status.text() == "REPORT")
        self.assertEqual(calls, [(False, False)])
        self.assertEqual(window.runtime_status.text(), "REPORT")
        self.assertTrue(log_threads and all(log_threads))
        self.assertEqual(window.cuda_links, {"driver": "https://example.invalid/driver"})

    def test_background_task_hides_cancel_and_confirms_close(self) -> None:
        window = self.window
        release = threading.Event()
        window.start_background_task("model-download", lambda: release.wait(5))
        self.assertTrue(window.task_dock.isVisibleTo(window))
        self.assertFalse(window.cancel_btn.isVisibleTo(window))
        self.assertFalse(window.cancel_btn.isEnabled())
        self.assertFalse(window.start_btn.isEnabled())

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as question:
            self.assertFalse(window.close())
            question.assert_called_once()

        release.set()
        pump(lambda: not window.background_tasks)
        self.assertEqual(window.background_tasks, set())
        self.assertTrue(window.start_btn.isEnabled())
        self.assertFalse(window.task_dock.isVisibleTo(window))

    def test_exception_hook_logs_and_shows_dialog(self) -> None:
        window = self.window
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook
        self.addCleanup(setattr, sys, "excepthook", old_hook)
        self.addCleanup(setattr, threading, "excepthook", old_thread_hook)
        qt_app.install_exception_hooks(lambda: window)
        try:
            raise ValueError("slot exploded")
        except ValueError:
            with patch.object(sys, "stderr", None):
                sys.excepthook(*sys.exc_info())
        self.assertIn("slot exploded", window.log_text.toPlainText())
        self.assertIn("critical", self.dialog_kinds())


class BatchTests(GuiTestCase):
    def make_media(self, names: list[str]) -> Path:
        root = self.tmp / "media"
        root.mkdir()
        for name in names:
            (root / name).write_bytes(b"x")
        return root

    def test_stale_preview_scan_is_ignored_after_job_start(self) -> None:
        window = self.window
        root = self.make_media(["a.mp3", "b.mp3"])
        window.batch_input_dir.setText(str(root))
        stale_generation = window.preview_generation
        release = threading.Event()

        def fake_batch(config, log, progress=None):
            emit_task_event("batch_discovered", total=2, files=["a.mp3", "b.mp3"])
            emit_task_event("file_status", path="a.mp3", status="failed", output="")
            release.wait(5)
            return str(root)

        with patch.object(core, "run_batch_txt_job", fake_batch):
            window.start_batch_job()
            pump(lambda: window.batch_table.rowCount() == 2 and window.failed_batch_paths)
            # While the batch runs, controls that rescan are disabled and rescans are no-ops.
            self.assertFalse(window.batch_recursive.isEnabled())
            self.assertFalse(window.batch_input_dir.isEnabled())
            self.assertFalse(window.batch_input_btn.isEnabled())
            window.refresh_batch_preview()
            window.apply_batch_preview(stale_generation, ["a.mp3", "b.mp3"], "")
            self.assertEqual(window.batch_table.item(0, 1).text(), "失败")
            self.assertEqual(window.failed_batch_paths, {"a.mp3"})
            release.set()
            pump(lambda: window.worker_thread is None, 5.0)
        self.assertTrue(window.batch_retry_btn.isEnabled())

    def test_retry_subset_keeps_full_table(self) -> None:
        window = self.window
        for name in ["a.mp3", "b.mp3", "c.mp3"]:
            window.ensure_batch_row(name, "已完成", "")
        window.batch_total_metric.setText("3")
        window.failed_batch_paths = {"b.mp3"}
        window.batch_subset_run = True
        window.handle_task_event({"type": "batch_discovered", "files": ["b.mp3"]})
        self.assertEqual(window.batch_table.rowCount(), 3)
        self.assertEqual(window.batch_total_metric.text(), "3")
        window.handle_task_event(
            {"type": "file_status", "path": "b.mp3", "status": "completed", "output": "o"}
        )
        self.assertEqual(window.batch_table.rowCount(), 3)
        self.assertEqual(window.batch_table.item(1, 1).text(), "已完成")
        self.assertEqual(window.failed_batch_paths, set())

    def test_refresh_disables_retry_and_manual_dir_edit_rescans_only_on_change(self) -> None:
        window = self.window
        window.failed_batch_paths = {"a.mp3"}
        window.batch_retry_btn.setEnabled(True)
        window.refresh_batch_preview()
        self.assertFalse(window.batch_retry_btn.isEnabled())

        generation = window.preview_generation
        window.batch_input_dir.editingFinished.emit()
        self.assertEqual(window.preview_generation, generation)
        window.batch_input_dir.setText(str(self.tmp))
        window.batch_input_dir.editingFinished.emit()
        self.assertEqual(window.preview_generation, generation + 1)


class OutputPathTests(GuiTestCase):
    def test_typed_output_path_is_custom_and_survives_edits(self) -> None:
        window = self.window
        window.audio_path.setText(r"C:\a\clip.mp4")
        window.output_path.clear()
        custom = str(self.tmp / "mine" / "custom.srt")
        QTest.keyClicks(window.output_path, custom)
        self.assertTrue(window.output_path_custom)
        window.script_text.setPlainText("hello")
        window.transcribe_mode_btn.click()
        self.assertEqual(window.output_path.text(), custom)

    def test_output_dir_editing_finished_without_change_keeps_custom_path(self) -> None:
        window = self.window
        custom = str(self.tmp / "custom.srt")
        window.output_path_custom = True
        window.output_path.setText(custom)
        window.output_dir.editingFinished.emit()
        self.assertTrue(window.output_path_custom)
        self.assertEqual(window.output_path.text(), custom)

        window.output_dir.setText(str(self.tmp / "elsewhere"))
        window.output_dir.editingFinished.emit()
        self.assertFalse(window.output_path_custom)
        self.assertTrue(window.output_path.text().startswith(str(self.tmp / "elsewhere")))

    def test_save_as_does_not_change_default_output_dir(self) -> None:
        window = self.window
        default_dir = window.output_dir.text()
        target = str(self.tmp / "one-off" / "x.srt")
        with patch.object(qt_app.QFileDialog, "getSaveFileName", return_value=(target, "")):
            window.pick_output_file()
        self.assertEqual(window.output_dir.text(), default_dir)
        self.assertTrue(window.output_path_custom)
        self.assertEqual(window.output_path.text(), target)


class SpeakerTests(GuiTestCase):
    def load_srt(self) -> Path:
        srt = self.tmp / "x.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n[张三] 你好\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n[李四] 再见\n",
            encoding="utf-8-sig",
        )
        self.assertTrue(self.window.load_srt_to_speaker_table(str(srt)))
        return srt

    def test_safe_speaker_filename_keeps_unicode(self) -> None:
        self.assertEqual(qt_app.safe_speaker_filename("张三"), "张三")
        self.assertEqual(qt_app.safe_speaker_filename('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(qt_app.safe_speaker_filename("name. "), "name")
        self.assertEqual(qt_app.safe_speaker_filename(" .. "), "speaker")

    def test_chinese_speakers_export_to_distinct_files(self) -> None:
        window = self.window
        self.load_srt()
        for speaker in ["张三", "李四"]:
            window.speaker_combo.setCurrentText(speaker)
            window.export_selected_speaker("txt")
        self.assertEqual((self.tmp / "x.张三.edited.txt").read_text(encoding="utf-8-sig"), "你好\n")
        self.assertEqual((self.tmp / "x.李四.edited.txt").read_text(encoding="utf-8-sig"), "再见\n")

    def test_renamed_speaker_refreshes_combo_before_export(self) -> None:
        window = self.window
        self.load_srt()
        window.speaker_table.item(0, 3).setText("王五")
        items = [window.speaker_combo.itemText(i) for i in range(window.speaker_combo.count())]
        self.assertEqual(items, ["李四", "王五"])
        window.speaker_combo.setCurrentText("王五")
        window.export_selected_speaker("srt")
        exported = (self.tmp / "x.王五.edited.srt").read_text(encoding="utf-8-sig")
        self.assertIn("[王五] 你好", exported)

    def test_export_with_no_matching_rows_warns_instead_of_writing(self) -> None:
        window = self.window
        self.load_srt()
        window.speaker_combo.blockSignals(True)
        window.speaker_combo.addItem("幽灵")
        window.speaker_combo.setCurrentText("幽灵")
        window.speaker_combo.blockSignals(False)
        with patch.object(window, "refresh_speaker_combo"):
            window.export_selected_speaker("txt")
        self.assertIn("warning", self.dialog_kinds())
        self.assertFalse(list(self.tmp.glob("*幽灵*")))

    def test_save_failure_shows_error_dialog(self) -> None:
        window = self.window
        self.load_srt()
        locked = self.tmp / "locked_dir"
        locked.mkdir()
        window.last_srt_path = str(locked)  # writing to a directory raises OSError
        window.save_edited_srt()
        self.assertIn("critical", self.dialog_kinds())


class ScriptEncodingTests(GuiTestCase):
    def test_decode_script_bytes(self) -> None:
        text = "中文文稿"
        self.assertEqual(qt_app.decode_script_bytes(text.encode("utf-16")), text)
        self.assertEqual(qt_app.decode_script_bytes(b"\xfe\xff" + text.encode("utf-16-be")), text)
        self.assertEqual(qt_app.decode_script_bytes(b"\xef\xbb\xbf" + text.encode("utf-8")), text)
        self.assertEqual(qt_app.decode_script_bytes(text.encode("utf-8")), text)
        self.assertEqual(qt_app.decode_script_bytes(text.encode("gbk")), text)
        self.assertIsNone(qt_app.decode_script_bytes(b"\x81\xff\xfe\x80\x80"))

    def test_undecodable_file_warns_and_is_not_loaded(self) -> None:
        window = self.window
        bad = self.tmp / "bad.txt"
        bad.write_bytes(b"\x81\xff\xfe\x80\x80")
        window.script_text.setPlainText("keep me")
        window.load_text_file(str(bad))
        self.assertIn("warning", self.dialog_kinds())
        self.assertEqual(window.script_text.toPlainText(), "keep me")

        good = self.tmp / "utf16.txt"
        good.write_bytes("中文文稿".encode("utf-16"))
        window.load_text_file(str(good))
        self.assertEqual(window.script_text.toPlainText(), "中文文稿")


if __name__ == "__main__":
    unittest.main()
