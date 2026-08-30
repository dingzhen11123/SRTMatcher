from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import bootstrap


class OverwriteUpgradeTests(unittest.TestCase):
    def test_marker_and_source_hash_detect_and_repair_old_files(self) -> None:
        original_home = bootstrap.APP_HOME
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir)
                bootstrap.set_install_home(home)
                pythonw = bootstrap.VENV_DIR / "Scripts" / "pythonw.exe"
                pythonw.parent.mkdir(parents=True)
                pythonw.touch()

                bootstrap.install_sources()
                bootstrap.write_runtime_marker()
                self.assertTrue(bootstrap.runtime_marker_ready())

                installed_app = bootstrap.APP_DIR / "app.py"
                installed_app.write_text("# stale application\n", encoding="utf-8")
                self.assertFalse(bootstrap.runtime_marker_ready())

                bootstrap.install_sources()
                bootstrap.write_runtime_marker()
                self.assertTrue(bootstrap.runtime_marker_ready())
                self.assertEqual(
                    installed_app.read_bytes(),
                    (bootstrap.source_root() / "app.py").read_bytes(),
                )
                self.assertFalse(any(bootstrap.APP_DIR.glob(".*.upgrade.tmp")))

                bootstrap.READY_FILE.write_text(
                    f"{bootstrap.APP_BUILD_ID}\n0\n", encoding="utf-8"
                )
                self.assertFalse(bootstrap.runtime_marker_ready())
        finally:
            bootstrap.set_install_home(original_home)


if __name__ == "__main__":
    unittest.main()
