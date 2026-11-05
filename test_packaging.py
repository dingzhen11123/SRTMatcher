from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bootstrap

ROOT = Path(__file__).resolve().parent
REAL_POPEN = subprocess.Popen


def reachable_local_modules(entry_points: list[str]) -> set[str]:
    local = {path.stem for path in ROOT.glob("*.py")}
    seen: set[str] = set()
    pending = list(entry_points)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            else:
                continue
            pending.extend(n for n in names if n in local and n not in seen)
    return seen


class PackagingTests(unittest.TestCase):
    def test_every_imported_local_module_is_shipped(self) -> None:
        shipped = {Path(name).stem for name in bootstrap.SOURCE_FILES if name.endswith(".py")}
        missing = reachable_local_modules(["app", "qt_app"]) - shipped
        self.assertEqual(missing, set(), f"SOURCE_FILES 缺少模块: {sorted(missing)}")

    def test_source_files_exist(self) -> None:
        for name in bootstrap.SOURCE_FILES:
            self.assertTrue((ROOT / name).is_file(), name)


class LauncherRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_home = bootstrap.APP_HOME
        self.temp = tempfile.TemporaryDirectory()
        bootstrap.set_install_home(Path(self.temp.name))
        bootstrap.APP_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        bootstrap.set_install_home(self.original_home)
        self.temp.cleanup()

    def test_requirements_change_forces_dependency_install(self) -> None:
        requirements = bootstrap.APP_DIR / "requirements.txt"
        requirements.write_text("PySide6>=6\n", encoding="utf-8")
        self.assertFalse(bootstrap.requirements_installed())
        bootstrap.REQUIREMENTS_FILE.write_text(bootstrap.requirements_hash() + "\n", encoding="utf-8")
        self.assertTrue(bootstrap.requirements_installed())
        requirements.write_text("PySide6>=6\nhttpx>=0.27\n", encoding="utf-8")
        self.assertFalse(bootstrap.requirements_installed())

    def test_launch_app_reports_immediate_crash_and_clears_marker(self) -> None:
        bootstrap.READY_FILE.write_text("marker\n", encoding="utf-8")

        def fake_popen(args, cwd=None, stdout=None, stderr=None, creationflags=0):
            stderr.write("Traceback ...\nModuleNotFoundError: No module named 'httpx'\n")
            return REAL_POPEN([sys.executable, "-c", "raise SystemExit(1)"])

        with patch.object(bootstrap.subprocess, "Popen", side_effect=fake_popen):
            with self.assertRaises(RuntimeError) as ctx:
                bootstrap.launch_app(show_status=False)
        self.assertIn("No module named 'httpx'", str(ctx.exception))
        self.assertFalse(bootstrap.READY_FILE.exists())

    def test_launch_app_accepts_long_running_gui(self) -> None:
        started: list[subprocess.Popen] = []

        def fake_popen(*_args, **_kwargs):
            proc = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])
            started.append(proc)
            return proc

        try:
            with (
                patch.object(bootstrap.subprocess, "Popen", side_effect=fake_popen),
                patch.object(bootstrap, "APP_START_GRACE_SECONDS", 0.5),
            ):
                bootstrap.launch_app(show_status=False)
        finally:
            for proc in started:
                proc.kill()
                proc.wait()
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
