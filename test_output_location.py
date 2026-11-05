import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app
import qt_app


class OutputLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qapp = QApplication.instance() or QApplication([])

    def make_window(self, settings: dict) -> qt_app.MainWindow:
        with patch.object(app, "load_settings", return_value=dict(settings)), patch.object(app, "save_settings"):
            window = qt_app.MainWindow()
        self.addCleanup(window.deleteLater)
        return window

    def test_defaults_to_media_folder(self) -> None:
        window = self.make_window({"output_dir": "D:/desktop"})
        self.assertTrue(window.output_to_source.isChecked())
        self.assertFalse(window.output_dir.isEnabled())
        media = Path("E:/videos/show/ep1.mp4")
        window.audio_path.setText(str(media))
        window.output_path_custom = False
        window.update_output_path()
        self.assertEqual(Path(window.output_path.text()).parent, media.parent)
        self.assertEqual(window.gather_config()["output_location"], "source")

    def test_fixed_folder_when_unchecked(self) -> None:
        window = self.make_window({"output_dir": "D:/desktop", "output_location": "fixed"})
        self.assertFalse(window.output_to_source.isChecked())
        self.assertTrue(window.output_dir.isEnabled())
        window.audio_path.setText("E:/videos/show/ep1.mp4")
        window.update_output_path()
        self.assertEqual(Path(window.output_path.text()).parent, Path("D:/desktop"))

    def test_toggling_recomputes_auto_path(self) -> None:
        window = self.make_window({"output_dir": "D:/desktop", "output_location": "fixed"})
        window.audio_path.setText("E:/videos/show/ep1.mp4")
        window.update_output_path()
        window.output_to_source.setChecked(True)
        self.assertEqual(Path(window.output_path.text()).parent, Path("E:/videos/show"))

    def test_loading_settings_keeps_custom_path_flag(self) -> None:
        window = self.make_window({"output_path_custom": True, "output_path": "F:/x/custom.srt"})
        self.assertTrue(window.output_path_custom)
        self.assertEqual(window.output_path.text(), "F:/x/custom.srt")


if __name__ == "__main__":
    unittest.main()
