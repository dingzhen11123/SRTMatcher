from __future__ import annotations

import os
import subprocess
import sys
import unittest

import app


@unittest.skipUnless(os.name == "nt", "Windows-only console-window behavior")
class HiddenSubprocessTests(unittest.TestCase):
    def test_global_policy_is_installed(self) -> None:
        self.assertTrue(getattr(subprocess.Popen, "_srtmatcher_hidden", False))

    def test_child_python_has_no_console_window(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        self.assertEqual(result.stdout.strip(), "0")

    def test_existing_create_new_console_request_is_suppressed(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
