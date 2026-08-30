from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

import app


class BundledFfmpegTests(unittest.TestCase):
    def test_bundled_ffmpeg_is_found_without_system_path(self) -> None:
        ffmpeg = Path("C:/ffmpeg/ffmpeg.exe")
        if not ffmpeg.exists():
            self.skipTest("local FFmpeg fixture is unavailable")

        original_app_dir = app.APP_DIR
        original_path = os.environ.get("PATH", "")
        logs: list[str] = []
        try:
            app.APP_DIR = ffmpeg.parent
            os.environ["PATH"] = ""
            resolved = app.add_bundled_ffmpeg_path(logs.append)
            self.assertEqual(Path(resolved), ffmpeg.resolve())
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("ffmpeg version", result.stdout)
            self.assertTrue(any("使用内置 FFmpeg" in line for line in logs))
        finally:
            app.APP_DIR = original_app_dir
            os.environ["PATH"] = original_path


if __name__ == "__main__":
    unittest.main()
