from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


class CudaRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        app._TORCH_CUDA_DISABLED_FOR_PROCESS = False
        app._TORCH_CUDA_PROBE_RESULT = None

    def tearDown(self) -> None:
        app._TORCH_CUDA_DISABLED_FOR_PROCESS = False
        app._TORCH_CUDA_PROBE_RESULT = None

    def test_cuda_12_8_uses_cu128_build(self) -> None:
        plan = app.select_torch_cuda_build("13.2")
        self.assertEqual(plan["build"], "cu128")
        self.assertIn("2.8.0+cu128", plan["torch"])

    def test_kernel_architecture_error_is_recognized(self) -> None:
        exc = RuntimeError("CUDA error: no kernel image is available for execution on the device")
        self.assertTrue(app.is_torch_cuda_runtime_error(exc))
        self.assertFalse(app.is_torch_cuda_runtime_error(RuntimeError("missing subtitle file")))

    def test_real_kernel_probe_failure_selects_cpu_for_process(self) -> None:
        fake_cuda = SimpleNamespace(
            is_available=lambda: True,
            synchronize=lambda: None,
            empty_cache=lambda: None,
        )
        fake_torch = SimpleNamespace(
            cuda=fake_cuda,
            float32="float32",
            ones=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("no kernel image is available for execution on the device")
            ),
            nn=SimpleNamespace(functional=SimpleNamespace(group_norm=lambda value, _groups: value)),
        )
        logs: list[str] = []
        with patch.dict(sys.modules, {"torch": fake_torch}):
            device = app.resolve_torch_device("cuda", logs.append)

        self.assertEqual(device, "cpu")
        self.assertTrue(app.torch_cuda_disabled_for_process())
        self.assertTrue(any("实际运算失败" in line for line in logs))

    def test_whisperx_cuda_error_retries_on_cpu_even_in_strict_mode(self) -> None:
        calls: list[str] = []

        def align(transcript, _model, _metadata, _audio, device, **_kwargs):
            calls.append(device)
            if device == "cuda":
                raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
            return {"segments": transcript}

        fake_whisperx = SimpleNamespace(
            load_align_model=lambda language_code, device: (object(), {"language": language_code}),
            load_audio=lambda _path: object(),
            align=align,
        )
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None))
        subtitles = [app.Subtitle(1, 0.0, 1.0, "hello")]
        logs: list[str] = []
        cache: dict = {}

        with (
            patch.dict(sys.modules, {"whisperx": fake_whisperx, "torch": fake_torch}),
            patch.object(app, "add_bundled_ffmpeg_path", return_value="ffmpeg.exe"),
            patch.object(
                app,
                "resolve_torch_device",
                side_effect=lambda choice, _log: "cuda" if choice == "cuda" else "cpu",
            ),
        ):
            aligned, _words = app.run_whisperx_alignment(
                "audio.wav",
                subtitles,
                "en",
                "cuda",
                logs.append,
                align_model_cache=cache,
                strict=True,
            )

        self.assertEqual(calls, ["cuda", "cpu"])
        self.assertEqual(len(aligned), 1)
        self.assertTrue(app.torch_cuda_disabled_for_process())
        self.assertNotIn(("en", "cuda"), cache)
        self.assertIn(("en", "cpu"), cache)
        self.assertTrue(any("自动切换 CPU 重试" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
