from __future__ import annotations

import gc
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import copy
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from ai_transport import AI_TRANSPORT
from asr_runtime import normalize_performance_preset, transcribe_options
from batch_runtime import select_requested_media
from model_runtime import MODEL_RUNTIME
from task_runtime import (
    TaskCancelled,
    check_cancelled,
    current_cancellation_token,
    emit_task_event,
    task_context,
)
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
INSTALL_ROOT = APP_DIR.parent if APP_DIR.name.lower() == "app" else APP_DIR
APP_NAME = "SRTMatcher"
DISPLAY_NAME = "字幕多功能工具"
LEGACY_CONFIG_PATH = APP_DIR / "config.json"
PORTABLE_SETTINGS_PATH = APP_DIR / "settings.json"
SETTINGS_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME / "settings.json"
DEFAULT_MODEL_REPO = "large-v3-turbo"
DEFAULT_MODEL_DIRNAME = "faster-whisper-large-v3-turbo"
DEFAULT_MODEL_ROOT = INSTALL_ROOT / "models"
DEFAULT_MODEL_PATH = str(DEFAULT_MODEL_ROOT)
APP_SETTING_KEYS = {
    "model_path",
    "output_dir",
    "output_path",
    "output_path_custom",
    "output_format",
    "device",
    "compute_type",
    "performance_preset",
    "language",
    "mode",
    "max_chars",
    "ai_enabled",
    "whisperx_enabled",
    "word_timestamp_export",
    "diarization_enabled",
    "hf_token",
    "min_speakers",
    "max_speakers",
    "base_url",
    "api_key",
    "ai_model",
    "system_prompt",
    "batch_input_dir",
    "batch_output_dir",
    "batch_recursive",
    "batch_skip_existing",
    "batch_output_format",
}
MEDIA_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".wma",
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".mpeg", ".mpg", ".flv",
}
BATCH_ERROR_FILENAME = "_batch_errors.txt"
BATCH_SRT_ERROR_FILENAME = "_batch_srt_errors.txt"
_TORCH_CUDA_DISABLED_FOR_PROCESS = False
_TORCH_CUDA_PROBE_RESULT: bool | None = None


def install_hidden_subprocess_policy() -> bool:
    """Force every child process started by this Windows GUI to stay invisible.

    WhisperX launches FFmpeg through its own ``subprocess.run`` call.  Adding
    ``CREATE_NO_WINDOW`` only to our direct calls therefore still lets a console
    flash once per batch item.  Replacing ``subprocess.Popen`` at the shared
    module boundary also covers third-party calls imported later, while retaining
    the original Popen API and return type.
    """
    if os.name != "nt" or getattr(subprocess.Popen, "_srtmatcher_hidden", False):
        return False

    original_popen = subprocess.Popen
    no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    detached = int(getattr(subprocess, "DETACHED_PROCESS", 0))

    class HiddenWindowsPopen(original_popen):
        _srtmatcher_hidden = True
        _srtmatcher_original = original_popen

        def __init__(self, *popen_args, **kwargs):
            positional = list(popen_args)

            # Popen's positional slots 12/13 are startupinfo/creationflags.
            # They are normally passed by keyword, but supporting both keeps this
            # transparent to third-party libraries.
            if len(positional) > 13:
                flags = int(positional[13] or 0)
                if not flags & detached:
                    positional[13] = (flags & ~new_console) | no_window
            else:
                flags = int(kwargs.get("creationflags", 0) or 0)
                if not flags & detached:
                    kwargs["creationflags"] = (flags & ~new_console) | no_window

            if len(positional) > 12:
                startup = copy.copy(positional[12]) if positional[12] is not None else subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                positional[12] = startup
            else:
                supplied = kwargs.get("startupinfo")
                startup = copy.copy(supplied) if supplied is not None else subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = startup

            super().__init__(*positional, **kwargs)

    HiddenWindowsPopen.__name__ = original_popen.__name__
    HiddenWindowsPopen.__qualname__ = original_popen.__qualname__
    HiddenWindowsPopen.__module__ = original_popen.__module__
    subprocess.Popen = HiddenWindowsPopen
    return True


# Install before faster-whisper/WhisperX/pyannote are imported.  Since Python's
# subprocess module is shared, their later FFmpeg calls inherit this policy too.
install_hidden_subprocess_policy()


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings() -> dict:
    for path in (SETTINGS_PATH, PORTABLE_SETTINGS_PATH, LEGACY_CONFIG_PATH):
        if path.exists():
            data = read_json_file(path)
            if data and APP_SETTING_KEYS.intersection(data):
                return data
    return {}


def save_settings(data: dict) -> Path:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    SETTINGS_PATH.write_text(text, encoding="utf-8")

    try:
        PORTABLE_SETTINGS_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass

    return SETTINGS_PATH


def consume_transcription_segments(segments_iter) -> list[object]:
    """Materialize faster-whisper output with cooperative cancel checkpoints."""
    segments: list[object] = []
    for segment in segments_iter:
        check_cancelled()
        segments.append(segment)
    check_cancelled()
    return segments


def release_cached_models(log=None) -> dict:
    """Release process-local ASR/alignment/diarization models and GPU memory."""
    stats = MODEL_RUNTIME.release_all()
    AI_TRANSPORT.close()
    if log is not None:
        log(
            "已释放模型缓存："
            f"ASR {'1' if stats['asr_loaded'] else '0'}，"
            f"对齐 {stats['alignment_models']}，"
            f"说话人 {stats['diarization_models']}。"
        )
    return stats


def parse_version(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(part) for part in parts[:3])


def run_capture(args: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return proc.returncode, proc.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or ""


def detect_nvidia_smi() -> dict:
    code, output = run_capture(["nvidia-smi"], timeout=20)
    info = {
        "available": code == 0,
        "raw": output,
        "driver_version": "",
        "cuda_version": "",
        "gpu_name": "",
    }
    if code != 0:
        return info

    driver_match = re.search(r"Driver Version:\s*([0-9.]+)", output)
    cuda_match = re.search(r"CUDA Version:\s*([0-9.]+)", output)
    if driver_match:
        info["driver_version"] = driver_match.group(1)
    if cuda_match:
        info["cuda_version"] = cuda_match.group(1)

    q_code, q_output = run_capture(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        timeout=20,
    )
    if q_code == 0 and q_output.strip():
        first = q_output.strip().splitlines()[0]
        info["gpu_name"] = first.split(",")[0].strip()
        if not info["driver_version"] and "," in first:
            info["driver_version"] = first.split(",", 1)[1].strip()
    return info


def select_torch_cuda_build(cuda_version: str) -> dict:
    version = parse_version(cuda_version)
    if version >= (12, 8):
        build = "cu128"
    elif version >= (12, 6):
        build = "cu126"
    elif version >= (12, 4):
        build = "cu124"
    elif version >= (12, 1):
        build = "cu121"
    else:
        build = "cpu"

    if build == "cpu":
        return {
            "build": "cpu",
            "index_url": "https://download.pytorch.org/whl/cpu",
            "torch": "torch==2.8.0+cpu",
            "torchaudio": "torchaudio==2.8.0+cpu",
            "torchvision": "torchvision==0.23.0+cpu",
            "cuda_available": False,
            "reason": "未检测到足够新的 NVIDIA CUDA 驱动能力，将安装 CPU 版。",
        }

    return {
        "build": build,
        "index_url": f"https://download.pytorch.org/whl/{build}",
        "torch": f"torch==2.8.0+{build}",
        "torchaudio": f"torchaudio==2.8.0+{build}",
        "torchvision": f"torchvision==0.23.0+{build}",
        "cuda_available": True,
        "reason": f"nvidia-smi CUDA Version={cuda_version}，选择 PyTorch {build}。驱动可向后兼容该 CUDA runtime/cuDNN。",
    }


def recommended_runtime_plan() -> dict:
    nvidia = detect_nvidia_smi()
    torch_build = select_torch_cuda_build(str(nvidia.get("cuda_version", "")))
    return {"nvidia": nvidia, "torch": torch_build, "downloads": recommended_nvidia_downloads(str(nvidia.get("cuda_version", "")))}


CUDA_LOCAL_INSTALLERS = {
    "12.8.1": "https://developer.download.nvidia.com/compute/cuda/12.8.1/local_installers/cuda_12.8.1_572.61_windows.exe",
    "12.8.0": "https://developer.download.nvidia.com/compute/cuda/12.8.0/local_installers/cuda_12.8.0_571.96_windows.exe",
    "12.6.3": "https://developer.download.nvidia.com/compute/cuda/12.6.3/local_installers/cuda_12.6.3_561.17_windows.exe",
    "12.4.1": "https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_551.78_windows.exe",
    "12.1.1": "https://developer.download.nvidia.com/compute/cuda/12.1.1/local_installers/cuda_12.1.1_531.14_windows.exe",
}


def recommended_nvidia_downloads(cuda_version: str) -> dict:
    version = parse_version(cuda_version)
    if version >= (12, 8):
        cuda_key = "12.8.1"
    elif version >= (12, 6):
        cuda_key = "12.6.3"
    elif version >= (12, 4):
        cuda_key = "12.4.1"
    elif version >= (12, 1):
        cuda_key = "12.1.1"
    else:
        cuda_key = "12.6.3"
    return {
        "driver": "https://www.nvidia.com/Download/index.aspx",
        "cuda_version": cuda_key,
        "cuda_local": CUDA_LOCAL_INSTALLERS[cuda_key],
        "cuda_archive": "https://developer.nvidia.com/cuda-toolkit-archive",
        "cudnn": "https://developer.nvidia.com/cudnn-downloads",
    }


def install_recommended_torch(log, python_exe: str | None = None) -> None:
    python_exe = python_exe or sys.executable
    plan = recommended_runtime_plan()
    torch_plan = plan["torch"]
    log(torch_plan["reason"])
    command = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--index-url",
        torch_plan["index_url"],
        torch_plan["torch"],
        torch_plan["torchaudio"],
        torch_plan["torchvision"],
    ]
    log("执行: " + " ".join(command))
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            log(line.rstrip())
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"PyTorch CUDA 依赖安装失败，退出码: {code}")

DEFAULT_SYSTEM_PROMPT = """# 角色
你是一名中文/多语字幕校对员，服务于一个 AI 视频解说系统。上游的 ASR 语音识别结果常有同音错别字、错词、缺字多字、标点混乱等问题。你的任务是只做文本纠错，绝不改动时间轴与结构。

# 任务
对系统给出的字幕条目逐条修正文本错误，原样返回相同数量、相同编号的条目。

# 输入说明
- 输入是若干字幕条目，每行格式为：序号|原始文本
- 每个序号对应一条字幕，文本是 ASR 识别结果，可能有错。
- 你看不到也不需要时间轴；时间轴由系统保管，与你无关。

# 修正规则
1. 只修正：同音/近音错别字、明显错词、缺字或多字、明显的语音识别误判、必要的标点。
2. 必须保持每条原意不变，不得改写润色、不得增删信息、不得翻译、不得改变语言。
3. 不合并、不拆分、不增加、不删除任何条目；输入多少条，输出多少条。
4. 每条的序号必须原样保留、一一对应，顺序不变。
5. 拿不准的条目，原样输出该条文本，不要臆改。
6. 不要输出任何解释、说明、空行或多余字符；系统在请求中指定的批次完成标记除外。

# 输出格式
逐行输出，每行格式严格为：序号|修正后文本
- 行数与输入完全一致，序号与输入完全一致。
- 不要输出表头、不要输出 Markdown、不要加代码块、不要在前后添加任何文字。"""


@dataclass
class Token:
    text: str
    start_char: int
    end_char: int
    start_time: float | None = None
    end_time: float | None = None
    alignable: bool = True


@dataclass
class Subtitle:
    index: int
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass
class WordTimestamp:
    sentence_id: int
    word: str
    start: float | None
    end: float | None
    score: float | None = None


@dataclass
class AICompletion:
    content: str
    finish_reason: str = ""


def add_cuda_dll_paths() -> None:
    """Make common CUDA/cuDNN DLL locations visible before importing CTranslate2."""
    candidates: list[Path] = []

    for name, value in os.environ.items():
        if name.startswith("CUDA_PATH") and value:
            candidates.append(Path(value) / "bin")

    for base in [Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA")]:
        if base.exists():
            candidates.extend(sorted((p / "bin" for p in base.glob("v*")), reverse=True))

    candidates.extend(
        [
            APP_DIR / "_internal" / "torch" / "lib",
            APP_DIR / "_internal" / "ctranslate2",
            APP_DIR / "torch" / "lib",
            APP_DIR / "ctranslate2",
        ]
    )

    site_packages = [Path(p) for p in sys.path if "site-packages" in p.lower()]
    for sp in site_packages:
        candidates.extend(
            [
                sp / "nvidia" / "cublas" / "bin",
                sp / "nvidia" / "cudnn" / "bin",
                sp / "nvidia" / "cublas" / "lib",
                sp / "nvidia" / "cudnn" / "lib",
            ]
        )

    seen: set[str] = set()
    for path in candidates:
        if path.exists():
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(resolved)
                    except OSError:
                        pass


def check_torch_cuda_runtime(python_exe: str | None = None) -> dict:
    """Run a real PyTorch CUDA kernel in an isolated process and report the result."""
    add_cuda_dll_paths()
    result = {
        "available": False,
        "usable": False,
        "torch_version": "",
        "torch_cuda_version": "",
        "device_name": "",
        "compute_capability": "",
        "supported_architectures": [],
        "error": "",
    }
    script = r'''
import json

result = {
    "available": False,
    "usable": False,
    "torch_version": "",
    "torch_cuda_version": "",
    "device_name": "",
    "compute_capability": "",
    "supported_architectures": [],
    "error": "",
}
try:
    import torch
    result["torch_version"] = str(torch.__version__)
    result["torch_cuda_version"] = str(torch.version.cuda or "")
    result["available"] = bool(torch.cuda.is_available())
    if result["available"]:
        result["device_name"] = str(torch.cuda.get_device_name(0))
        capability = torch.cuda.get_device_capability(0)
        result["compute_capability"] = f"{capability[0]}.{capability[1]}"
        result["supported_architectures"] = list(torch.cuda.get_arch_list())
        # Device discovery and allocation can succeed even when this wheel has
        # no executable kernel for the GPU architecture. Force a real launch.
        sample = torch.ones((1, 8, 64), device="cuda", dtype=torch.float32)
        normalized = torch.nn.functional.group_norm(sample, 8)
        float(normalized.sum().item())
        torch.cuda.synchronize()
        result["usable"] = True
except Exception as exc:
    result["error"] = str(exc)
print(json.dumps(result, ensure_ascii=False))
'''
    env = os.environ.copy()
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    try:
        proc = subprocess.run(
            [python_exe or sys.executable, "-c", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        for line in reversed(proc.stdout.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                result.update(parsed)
                return result
        result["error"] = (proc.stderr or proc.stdout or f"CUDA 探针退出码 {proc.returncode}").strip()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def add_bundled_ffmpeg_path(log=None) -> str:
    bundled = APP_DIR / "ffmpeg.exe"
    if bundled.exists():
        directory = str(bundled.parent.resolve())
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if directory.lower() not in {entry.lower() for entry in path_entries if entry}:
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        resolved = str(bundled.resolve())
        if log:
            log(f"WhisperX 使用内置 FFmpeg: {resolved}")
        return resolved

    external = shutil.which("ffmpeg")
    if external:
        resolved = str(Path(external).resolve())
        if log:
            log(f"WhisperX 使用系统 FFmpeg: {resolved}")
        return resolved
    raise RuntimeError("WhisperX 需要 FFmpeg，但软件内置文件缺失且系统 PATH 中未找到 ffmpeg.exe。")


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().strip()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def is_punctuation(char: str) -> bool:
    return unicodedata.category(char).startswith("P") or char in "，。！？；：、,.!?;:"


def tokenize_text(text: str, *, keep_punctuation: bool = True) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if is_cjk(ch):
            tokens.append(Token(ch, i, i + 1, alignable=True))
            i += 1
            continue
        if ch.isalnum():
            j = i + 1
            while j < len(text) and text[j].isalnum() and not is_cjk(text[j]):
                j += 1
            tokens.append(Token(text[i:j], i, j, alignable=True))
            i = j
            continue
        if keep_punctuation:
            tokens.append(Token(ch, i, i + 1, alignable=False))
        i += 1
    return tokens


def distribute_times(tokens: list[Token], start: float, end: float) -> list[Token]:
    if not tokens:
        return []
    duration = max(0.02, end - start)
    timed: list[Token] = []
    for idx, token in enumerate(tokens):
        t0 = start + duration * idx / len(tokens)
        t1 = start + duration * (idx + 1) / len(tokens)
        timed.append(Token(token.text, token.start_char, token.end_char, t0, t1, token.alignable))
    return timed


def extract_asr_tokens(segments: list[object]) -> tuple[str, list[Token]]:
    full_text_parts: list[str] = []
    tokens: list[Token] = []
    char_cursor = 0

    for segment in segments:
        seg_text = getattr(segment, "text", "") or ""
        full_text_parts.append(seg_text)
        words = getattr(segment, "words", None) or []

        if words:
            for word in words:
                word_text = getattr(word, "word", "") or ""
                word_start = float(getattr(word, "start", getattr(segment, "start", 0.0)) or 0.0)
                word_end = float(getattr(word, "end", getattr(segment, "end", word_start + 0.1)) or word_start + 0.1)
                sub_tokens = tokenize_text(word_text, keep_punctuation=False)
                timed = distribute_times(sub_tokens, word_start, word_end)
                for token in timed:
                    token.start_char = char_cursor
                    token.end_char = char_cursor + len(token.text)
                    char_cursor = token.end_char
                    if normalize_for_match(token.text):
                        tokens.append(token)
        else:
            seg_start = float(getattr(segment, "start", 0.0) or 0.0)
            seg_end = float(getattr(segment, "end", seg_start + 0.1) or seg_start + 0.1)
            sub_tokens = tokenize_text(seg_text, keep_punctuation=False)
            timed = distribute_times(sub_tokens, seg_start, seg_end)
            for token in timed:
                if normalize_for_match(token.text):
                    tokens.append(token)

    return "".join(full_text_parts).strip(), tokens


def assign_script_times(script: str, asr_tokens: list[Token]) -> list[Token]:
    script_tokens = tokenize_text(script, keep_punctuation=True)
    script_alignable = [(idx, normalize_for_match(t.text)) for idx, t in enumerate(script_tokens) if t.alignable]
    asr_alignable = [(idx, normalize_for_match(t.text)) for idx, t in enumerate(asr_tokens) if normalize_for_match(t.text)]

    script_seq = [item[1] for item in script_alignable]
    asr_seq = [item[1] for item in asr_alignable]

    matcher = SequenceMatcher(None, script_seq, asr_seq, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size == 0:
            continue
        for offset in range(block.size):
            script_index = script_alignable[block.a + offset][0]
            asr_index = asr_alignable[block.b + offset][0]
            script_tokens[script_index].start_time = asr_tokens[asr_index].start_time
            script_tokens[script_index].end_time = asr_tokens[asr_index].end_time

    timed_indexes = [i for i, t in enumerate(script_tokens) if t.start_time is not None and t.end_time is not None]
    if not script_tokens or not asr_tokens:
        return script_tokens

    first_time = asr_tokens[0].start_time or 0.0
    last_time = asr_tokens[-1].end_time or max(first_time + 1.0, first_time)

    if not timed_indexes:
        duration = max(1.0, last_time - first_time)
        for idx, token in enumerate(script_tokens):
            token.start_time = first_time + duration * idx / len(script_tokens)
            token.end_time = first_time + duration * (idx + 1) / len(script_tokens)
        return script_tokens

    for idx, token in enumerate(script_tokens):
        if token.start_time is not None:
            continue
        left = next((j for j in reversed(timed_indexes) if j < idx), None)
        right = next((j for j in timed_indexes if j > idx), None)
        if left is None and right is None:
            token.start_time = first_time
            token.end_time = min(last_time, first_time + 0.2)
        elif left is None:
            right_time = script_tokens[right].start_time or first_time
            span = right + 1
            token.start_time = first_time + (right_time - first_time) * idx / span
            token.end_time = first_time + (right_time - first_time) * (idx + 1) / span
        elif right is None:
            left_time = script_tokens[left].end_time or first_time
            span = len(script_tokens) - left
            token.start_time = left_time + (last_time - left_time) * (idx - left - 1) / span
            token.end_time = left_time + (last_time - left_time) * (idx - left) / span
        else:
            left_time = script_tokens[left].end_time or first_time
            right_time = script_tokens[right].start_time or last_time
            span = right - left
            token.start_time = left_time + (right_time - left_time) * (idx - left - 1) / span
            token.end_time = left_time + (right_time - left_time) * (idx - left) / span

    return script_tokens


def visual_len(text: str) -> int:
    count = 0
    for ch in text:
        if ch.isspace():
            continue
        count += 1 if is_cjk(ch) else 1
    return count


def split_script(script: str, max_chars: int) -> list[tuple[int, int, str]]:
    hard_punc = set("。！？!?；;\n")
    soft_punc = set("，,、：:")
    chunks: list[tuple[int, int, str]] = []
    start = 0
    last_soft: int | None = None
    i = 0

    while i < len(script):
        ch = script[i]
        if ch in soft_punc:
            last_soft = i + 1
        current = script[start : i + 1]
        should_split = False
        split_at = i + 1

        if ch in hard_punc and visual_len(current) >= 6:
            should_split = True
        elif visual_len(current) >= max_chars:
            should_split = True
            if last_soft and last_soft > start:
                split_at = last_soft

        if should_split:
            raw = script[start:split_at].strip()
            if raw:
                chunks.append((start, split_at, raw))
            start = split_at
            last_soft = None
            while start < len(script) and script[start].isspace():
                start += 1
            i = start
            continue
        i += 1

    tail = script[start:].strip()
    if tail:
        chunks.append((start, len(script), tail))
    return chunks


def subtitles_from_script(script: str, script_tokens: list[Token], max_chars: int) -> list[Subtitle]:
    chunks = split_script(script, max_chars)
    subtitles: list[Subtitle] = []
    last_end = 0.0

    for idx, (start_char, end_char, text) in enumerate(chunks, start=1):
        contained = [
            t
            for t in script_tokens
            if t.end_char > start_char
            and t.start_char < end_char
            and t.start_time is not None
            and t.end_time is not None
        ]
        if contained:
            start = min(t.start_time for t in contained if t.start_time is not None)
            end = max(t.end_time for t in contained if t.end_time is not None)
        else:
            start = last_end
            end = last_end + max(0.8, visual_len(text) * 0.18)

        start = max(last_end, float(start))
        end = max(start + 0.45, float(end))
        subtitles.append(Subtitle(idx, start, end, text))
        last_end = end

    return subtitles


def subtitles_from_audio_segments(script: str, script_tokens: list[Token], segments: list[object]) -> list[Subtitle]:
    timed_tokens = [
        token
        for token in script_tokens
        if token.start_time is not None and token.end_time is not None
    ]
    if not timed_tokens:
        return []

    subtitles: list[Subtitle] = []
    previous_char_end = 0
    previous_time_end = 0.0

    for segment in segments:
        segment_start = float(getattr(segment, "start", previous_time_end) or previous_time_end)
        segment_end = float(getattr(segment, "end", segment_start + 0.45) or segment_start + 0.45)
        contained = []

        for token in timed_tokens:
            midpoint = ((token.start_time or 0.0) + (token.end_time or 0.0)) / 2
            if segment_start - 0.05 <= midpoint <= segment_end + 0.05:
                contained.append(token)

        if not contained:
            continue

        start_char = min(token.start_char for token in contained)
        end_char = max(token.end_char for token in contained)

        if not subtitles:
            start_char = min(start_char, 0)
        elif start_char > previous_char_end:
            start_char = previous_char_end
        else:
            start_char = max(start_char, previous_char_end)

        if end_char <= start_char:
            continue

        text = script[start_char:end_char].strip()
        if not text:
            previous_char_end = max(previous_char_end, end_char)
            previous_time_end = max(previous_time_end, segment_end)
            continue

        start_time = max(previous_time_end, segment_start)
        end_time = max(start_time + 0.2, segment_end)
        subtitles.append(Subtitle(len(subtitles) + 1, start_time, end_time, text))
        previous_char_end = max(previous_char_end, end_char)
        previous_time_end = end_time

    tail = script[previous_char_end:].strip()
    if tail and subtitles:
        last = subtitles[-1]
        subtitles[-1] = Subtitle(last.index, last.start, last.end, f"{last.text}{tail}")
    elif tail:
        first_time = timed_tokens[0].start_time or 0.0
        last_time = timed_tokens[-1].end_time or max(first_time + 0.45, first_time)
        subtitles.append(Subtitle(1, first_time, max(first_time + 0.45, last_time), tail))

    return subtitles


def clean_subtitle_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def subtitles_from_asr_segments(segments: list[object]) -> list[Subtitle]:
    subtitles: list[Subtitle] = []
    for segment in segments:
        text = clean_subtitle_text(getattr(segment, "text", "") or "")
        if not text:
            continue
        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", start + 0.5) or start + 0.5)
        subtitles.append(Subtitle(len(subtitles) + 1, max(0.0, start), max(start + 0.08, end), text))
    return subtitles


COMMON_ASR_HALLUCINATION_PHRASES = (
    "untertitelung des zdf",
    "untertitel im auftrag des zdf",
    "untertitel der amara org community",
    "subtitles by the amara org community",
    "subtitles by amara org",
    "captions by",
    "sous titres realises par",
    "sous titrage societe radio canada",
)


def normalized_hallucination_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "").lower()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def asr_hallucination_reason(segment: object) -> str:
    text = str(getattr(segment, "text", "") or "").strip()
    normalized = normalized_hallucination_text(text)
    if any(phrase in normalized for phrase in COMMON_ASR_HALLUCINATION_PHRASES):
        return "命中常见片尾伪字幕"

    no_speech = getattr(segment, "no_speech_prob", None)
    avg_logprob = getattr(segment, "avg_logprob", None)
    compression = getattr(segment, "compression_ratio", None)
    try:
        if (
            no_speech is not None
            and avg_logprob is not None
            and float(no_speech) >= 0.65
            and float(avg_logprob) <= -0.80
        ):
            return f"尾部静音概率高(no_speech={float(no_speech):.2f}, logprob={float(avg_logprob):.2f})"
        if (
            compression is not None
            and avg_logprob is not None
            and float(compression) > 2.40
            and float(avg_logprob) <= -0.80
        ):
            return f"尾部重复/低置信(compression={float(compression):.2f}, logprob={float(avg_logprob):.2f})"
    except (TypeError, ValueError):
        pass
    return ""


def filter_trailing_asr_hallucinations(segments: list[object], log) -> list[object]:
    filtered = list(segments)
    removed: list[tuple[object, str]] = []
    while filtered:
        reason = asr_hallucination_reason(filtered[-1])
        if not reason:
            break
        removed.append((filtered.pop(), reason))

    for segment, reason in reversed(removed):
        text = str(getattr(segment, "text", "") or "").strip()
        log(f"ASR 尾部幻觉过滤: 已移除“{text}”（{reason}）。")
    if removed:
        log(f"ASR 尾部幻觉过滤完成，共移除 {len(removed)} 个伪字幕片段。")
    return filtered


def script_line_ranges(script: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    offset = 0
    for raw_line in script.splitlines(keepends=True):
        line_without_newline = raw_line.rstrip("\r\n")
        left_trimmed = len(line_without_newline) - len(line_without_newline.lstrip())
        right_trimmed = len(line_without_newline.rstrip())
        start = offset + left_trimmed
        end = offset + right_trimmed
        text = script[start:end]
        if text.strip():
            ranges.append((start, end, text.strip()))
        offset += len(raw_line)

    if not ranges and script.strip():
        start = len(script) - len(script.lstrip())
        end = len(script.rstrip())
        ranges.append((start, end, script[start:end].strip()))

    return ranges


def subtitles_from_script_lines(script: str, script_tokens: list[Token]) -> list[Subtitle]:
    line_ranges = script_line_ranges(script)
    subtitles: list[Subtitle] = []
    last_end = 0.0

    for line_index, (start_char, end_char, text) in enumerate(line_ranges, start=1):
        contained = [
            token
            for token in script_tokens
            if token.end_char > start_char
            and token.start_char < end_char
            and token.start_time is not None
            and token.end_time is not None
        ]

        if contained:
            start = min(token.start_time for token in contained if token.start_time is not None)
            end = max(token.end_time for token in contained if token.end_time is not None)
        else:
            start = last_end
            end = last_end + max(0.35, visual_len(text) * 0.16)

        start = max(last_end, float(start))
        end = max(start + 0.2, float(end))
        subtitles.append(Subtitle(line_index, start, end, text))
        last_end = end

    return subtitles


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms_total = int(round(seconds * 1000))
    hours = ms_total // 3_600_000
    ms_total %= 3_600_000
    minutes = ms_total // 60_000
    ms_total %= 60_000
    secs = ms_total // 1000
    millis = ms_total % 1000
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def srt_from_subtitles(subtitles: list[Subtitle]) -> str:
    blocks = []
    for sub in subtitles:
        text = f"[{sub.speaker}] {sub.text}" if sub.speaker else sub.text
        blocks.append(
            f"{sub.index}\n{format_timestamp(sub.start)} --> {format_timestamp(sub.end)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def repair_subtitle_text_boundaries(subtitles: list[Subtitle]) -> list[Subtitle]:
    repaired: list[Subtitle] = []
    leading_punctuation = set(".,;:!?，。！？；：、")
    for sub in subtitles:
        text = sub.text.strip()
        moved = ""
        while text and text[0] in leading_punctuation:
            moved += text[0]
            text = text[1:].lstrip()
        if moved and repaired:
            previous = repaired[-1]
            repaired[-1] = Subtitle(previous.index, previous.start, previous.end, f"{previous.text.rstrip()}{moved}", previous.speaker)
        repaired.append(Subtitle(sub.index, sub.start, sub.end, text, sub.speaker))
    return repaired


def repair_subtitle_timings(
    subtitles: list[Subtitle],
    audio_end: float | None = None,
    min_duration: float = 0.12,
    min_gap: float = 0.02,
) -> tuple[list[Subtitle], int]:
    if not subtitles:
        return subtitles, 0

    repaired: list[Subtitle] = []
    changes = 0
    for sub in subtitles:
        start = max(0.0, float(sub.start))
        end = max(start + min_duration, float(sub.end))
        original = (start, end)

        if repaired:
            previous = repaired[-1]
            if start < previous.end + min_gap:
                if start > previous.start + min_duration + min_gap:
                    new_prev_end = max(previous.start + min_duration, start - min_gap)
                    if abs(new_prev_end - previous.end) > 0.001:
                        repaired[-1] = Subtitle(previous.index, previous.start, new_prev_end, previous.text, previous.speaker)
                        changes += 1
                else:
                    start = previous.end + min_gap
                end = max(start + min_duration, end)

        if audio_end is not None and audio_end > 0:
            end = min(end, audio_end)
            if start >= end:
                start = max(0.0, end - min_duration)
                if repaired and start < repaired[-1].end + min_gap:
                    start = repaired[-1].end + min_gap
                    end = max(end, start + min_duration)

        if abs(start - original[0]) > 0.001 or abs(end - original[1]) > 0.001:
            changes += 1
        repaired.append(Subtitle(sub.index, start, max(start + min_duration, end), sub.text, sub.speaker))

    return repaired, changes


def replace_subtitle_texts(subtitles: list[Subtitle], mapping: dict[int, str]) -> list[Subtitle]:
    replaced: list[Subtitle] = []
    for sub in subtitles:
        replaced.append(Subtitle(sub.index, sub.start, sub.end, mapping.get(sub.index, sub.text), sub.speaker))
    return replaced


def torch_cuda_disabled_for_process() -> bool:
    return _TORCH_CUDA_DISABLED_FOR_PROCESS


def is_torch_cuda_runtime_error(exc: BaseException) -> bool:
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        messages.append(str(current).lower())
        current = current.__cause__ or current.__context__
    combined = "\n".join(messages)
    markers = (
        "cuda error",
        "no kernel image is available",
        "invalid device function",
        "device kernel image is invalid",
        "cuda driver version is insufficient",
        "cuda-capable device",
        "cudnn",
        "cublas",
        "cusparse",
    )
    return any(marker in combined for marker in markers)


def disable_torch_cuda_for_process() -> None:
    global _TORCH_CUDA_DISABLED_FOR_PROCESS, _TORCH_CUDA_PROBE_RESULT
    _TORCH_CUDA_DISABLED_FOR_PROCESS = True
    _TORCH_CUDA_PROBE_RESULT = False
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def torch_cuda_usable_in_process(log) -> bool:
    """Launch one real kernel once before WhisperX/pyannote chooses CUDA."""
    global _TORCH_CUDA_PROBE_RESULT
    if _TORCH_CUDA_DISABLED_FOR_PROCESS:
        return False
    if _TORCH_CUDA_PROBE_RESULT is not None:
        return _TORCH_CUDA_PROBE_RESULT

    import torch

    if not torch.cuda.is_available():
        _TORCH_CUDA_PROBE_RESULT = False
        return False
    try:
        sample = torch.ones((1, 8, 64), device="cuda", dtype=torch.float32)
        normalized = torch.nn.functional.group_norm(sample, 8)
        float(normalized.sum().item())
        torch.cuda.synchronize()
        _TORCH_CUDA_PROBE_RESULT = True
        return True
    except Exception as exc:
        log(f"PyTorch CUDA 实际运算失败，本次运行将改用 CPU: {exc}")
        disable_torch_cuda_for_process()
        return False


def resolve_torch_device(device_choice: str, log) -> str:
    wants_cuda = device_choice.lower() == "cuda"
    if wants_cuda and _TORCH_CUDA_DISABLED_FOR_PROCESS:
        log("本次运行已禁用 PyTorch CUDA，将使用 CPU 完成 WhisperX/说话人识别。")
        return "cpu"
    if wants_cuda and torch_cuda_usable_in_process(log):
        return "cuda"
    if wants_cuda:
        log("WhisperX / 说话人识别的 PyTorch CUDA 未通过真实运算检测，将使用 CPU。ASR 的 CTranslate2 CUDA 不受影响。")
    return "cpu"


def run_whisperx_alignment(
    audio_path: str,
    subtitles: list[Subtitle],
    language_code: str,
    device_choice: str,
    log,
    pre_pad: float = 0.2,
    post_pad: float = 0.5,
    align_model_cache: dict[tuple[str, str], tuple[object, object]] | None = None,
    strict: bool = False,
) -> tuple[list[Subtitle], dict[int, list[WordTimestamp] | None]]:
    if not subtitles:
        return subtitles, {}

    if align_model_cache is None:
        align_model_cache = MODEL_RUNTIME.align_models

    missing_words = {sub.index: None for sub in subtitles}

    supported_languages = {
        "en", "fr", "de", "es", "it", "ja", "zh", "nl", "uk", "pt", "ar", "cs", "ru", "pl", "hu", "fi",
        "fa", "el", "tr", "da", "he", "vi", "ko", "ur", "te", "hi", "ca", "ml", "no", "nn", "sk", "sl",
        "hr", "ro", "eu", "gl", "ka", "lv", "tl", "sv", "id",
    }
    language_code = (language_code or "").lower().split("-")[0]
    if language_code not in supported_languages:
        message = f"WhisperX 暂无默认对齐模型语言: {language_code or 'unknown'}"
        if strict:
            raise RuntimeError(message + "，无法生成要求精对齐的批量 SRT。")
        log(message + "，保留现有时间轴。")
        for sub in subtitles:
            log(f"逐词时间戳缺失: 句级字幕 {sub.index} 未执行 WhisperX 对齐。")
        return subtitles, missing_words

    try:
        import whisperx

        add_bundled_ffmpeg_path(log)
        device = resolve_torch_device(device_choice, log)
        cache_key = (language_code, device)
        if cache_key in align_model_cache:
            align_model, metadata = align_model_cache[cache_key]
            log(f"WhisperX 复用语言对齐模型: {language_code} ({device})。")
        else:
            log(f"WhisperX 强制对齐加载语言模型: {language_code} ({device}) ...")
            align_model, metadata = whisperx.load_align_model(language_code=language_code, device=device)
            align_model_cache[cache_key] = (align_model, metadata)
        audio = whisperx.load_audio(audio_path)

        transcript = []
        for sub in subtitles:
            transcript.append(
                {
                    "id": sub.index,
                    "start": max(0.0, sub.start - pre_pad),
                    "end": max(sub.end + post_pad, sub.start + 0.25),
                    "text": sub.text,
                    "avg_logprob": float(sub.index),
                }
            )

        log(f"WhisperX 开始二次对齐 {len(transcript)} 条字幕，窗口扩展: -{pre_pad:.2f}s/+{post_pad:.2f}s ...")
        result = whisperx.align(
            transcript,
            align_model,
            metadata,
            audio,
            device,
            interpolate_method="nearest",
            return_char_alignments=False,
        )

        grouped: dict[int, list[dict]] = {}
        for order, segment in enumerate(result.get("segments", []), start=1):
            marker = segment.get("id", segment.get("avg_logprob", order))
            if marker is None:
                continue
            try:
                index = int(round(float(marker)))
            except (TypeError, ValueError):
                continue
            grouped.setdefault(index, []).append(segment)

        aligned: list[Subtitle] = []
        word_timestamps: dict[int, list[WordTimestamp] | None] = {}
        improved = 0
        for sub in subtitles:
            pieces = grouped.get(sub.index, [])
            starts = [float(piece["start"]) for piece in pieces if piece.get("start") is not None]
            ends = [float(piece["end"]) for piece in pieces if piece.get("end") is not None]
            if starts and ends:
                start = max(0.0, min(starts))
                end = max(start + 0.08, max(ends))
                aligned.append(Subtitle(sub.index, start, end, sub.text, sub.speaker))
                improved += 1
            else:
                aligned.append(sub)

            words: list[WordTimestamp] = []
            for piece in pieces:
                for item in piece.get("words", []) or []:
                    word = str(item.get("word", ""))
                    try:
                        start = float(item["start"]) if item.get("start") is not None else None
                        end = float(item["end"]) if item.get("end") is not None else None
                        score = float(item["score"]) if item.get("score") is not None else None
                    except (TypeError, ValueError):
                        start, end, score = None, None, None
                    words.append(WordTimestamp(sub.index, word, start, end, score))
            if words:
                word_timestamps[sub.index] = words
                missing_count = sum(word.start is None or word.end is None for word in words)
                if missing_count:
                    log(f"逐词时间戳部分缺失: 句级字幕 {sub.index} 有 {missing_count} 个词/字没有完整时间。")
            else:
                word_timestamps[sub.index] = None
                log(f"逐词时间戳缺失: 句级字幕 {sub.index} 未获得 words 对齐结果。")

        log(f"WhisperX 对齐完成，更新 {improved}/{len(subtitles)} 条时间轴。")
        if strict and improved <= 0:
            raise RuntimeError("WhisperX 没有更新任何字幕时间轴，已拒绝输出非精对齐 SRT。")
        if strict and improved < len(subtitles):
            log(f"WhisperX 精对齐警告: {len(subtitles) - improved} 条保留 ASR 粗时间轴。")
        return aligned, word_timestamps
    except Exception as exc:
        attempted_device = locals().get("device")
        if attempted_device == "cuda" and is_torch_cuda_runtime_error(exc):
            log(f"WhisperX CUDA 与当前显卡/运行库不兼容，自动切换 CPU 重试: {exc}")
            disable_torch_cuda_for_process()
            align_model_cache.pop((language_code, "cuda"), None)
            return run_whisperx_alignment(
                audio_path,
                subtitles,
                language_code,
                "cpu",
                log,
                pre_pad=pre_pad,
                post_pad=post_pad,
                align_model_cache=align_model_cache,
                strict=strict,
            )
        if strict:
            raise RuntimeError(f"WhisperX 精对齐失败: {exc}") from exc
        log(f"WhisperX 对齐失败，保留现有时间轴: {exc}")
        log(traceback.format_exc())
        for sub in subtitles:
            log(f"逐词时间戳缺失: 句级字幕 {sub.index} 对齐失败。")
        return subtitles, missing_words


def repair_word_timestamps(
    subtitles: list[Subtitle],
    word_timestamps: dict[int, list[WordTimestamp] | None],
    log,
) -> dict[int, list[WordTimestamp] | None]:
    """Clamp word/character timings to final sentence bounds and remove overlap."""
    sentence_by_id = {sub.index: sub for sub in subtitles}
    repaired: dict[int, list[WordTimestamp] | None] = {}
    for sentence_id, source_words in word_timestamps.items():
        sentence = sentence_by_id.get(sentence_id)
        if sentence is None or not source_words:
            repaired[sentence_id] = None
            continue

        timed = [word for word in source_words if word.start is not None and word.end is not None]
        untimed = [word for word in source_words if word.start is None or word.end is None]
        timed.sort(key=lambda word: (float(word.start), float(word.end)))
        normalized: list[WordTimestamp] = []
        for word in timed:
            start = min(sentence.end, max(sentence.start, float(word.start)))
            end = min(sentence.end, max(start + 0.001, float(word.end)))
            if normalized and start < float(normalized[-1].end):
                previous = normalized[-1]
                boundary = min(
                    sentence.end,
                    max(float(previous.start) + 0.001, (float(previous.end) + start) / 2),
                )
                normalized[-1] = WordTimestamp(
                    previous.sentence_id, previous.word, previous.start, boundary, previous.score
                )
                start = boundary
                end = min(sentence.end, max(start + 0.001, end))
            if start >= sentence.end or end <= start:
                untimed.append(WordTimestamp(sentence_id, word.word, None, None, word.score))
                continue
            normalized.append(WordTimestamp(sentence_id, word.word, start, end, word.score))

        normalized.extend(untimed)
        repaired[sentence_id] = normalized or None
        if untimed:
            log(f"逐词时间戳校验: 句级字幕 {sentence_id} 有 {len(untimed)} 个词/字无法形成有效时间区间。")
    return repaired


def word_timestamp_output_path(output_path: str, export_format: str) -> Path:
    path = Path(output_path)
    return path.with_name(f"{path.stem}.words.{export_format}")


def export_word_timestamps(
    subtitles: list[Subtitle],
    word_timestamps: dict[int, list[WordTimestamp] | None],
    output_path: str,
    export_format: str,
    log,
) -> Path:
    export_format = export_format.strip().lower()
    if export_format not in {"json", "srt"}:
        raise ValueError(f"不支持的逐词时间戳格式: {export_format}")

    target = word_timestamp_output_path(output_path, export_format)
    target.parent.mkdir(parents=True, exist_ok=True)
    if export_format == "json":
        rows: list[dict] = []
        for sub in subtitles:
            words = word_timestamps.get(sub.index)
            if not words:
                rows.append({"sentence_id": sub.index, "words": None})
                continue
            for word in words:
                row = {
                    "sentence_id": sub.index,
                    "word": word.word,
                    "start": round(word.start, 6) if word.start is not None else None,
                    "end": round(word.end, 6) if word.end is not None else None,
                }
                if word.score is not None:
                    row["score"] = round(word.score, 6)
                rows.append(row)
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        blocks: list[str] = []
        index = 1
        for sub in subtitles:
            words = word_timestamps.get(sub.index)
            if not words:
                continue
            for word in words:
                if word.start is None or word.end is None:
                    continue
                blocks.append(
                    f"{index}\n{format_timestamp(word.start)} --> {format_timestamp(word.end)}\n{word.word}"
                )
                index += 1
        target.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8-sig")
    log(f"逐词时间戳已保存: {target}")
    return target


def parse_optional_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    number = int(value)
    return number if number > 0 else None


def run_speaker_diarization(
    audio_path: str,
    hf_token: str,
    device_choice: str,
    min_speakers: int | None,
    max_speakers: int | None,
    log,
) -> list[tuple[float, float, str]]:
    if not hf_token.strip():
        raise RuntimeError("启用说话人识别前，请在 设置 -> 说话人模型 填写 Hugging Face Token，并确认已同意 pyannote 模型许可。")

    import torch
    import whisperx
    from pyannote.audio import Pipeline

    device = resolve_torch_device(device_choice, log)
    repository = "pyannote/speaker-diarization-community-1"

    def create_pipeline():
        loaded = Pipeline.from_pretrained(repository, token=hf_token.strip())
        if device == "cuda":
            loaded.to(torch.device("cuda"))
        return loaded

    pipeline, reused = MODEL_RUNTIME.get_diarization_pipeline(repository, device, create_pipeline)
    action = "复用" if reused else "加载"
    log(f"{action} pyannote 说话人识别模型 ({device}) ...")

    add_bundled_ffmpeg_path(log)
    audio = whisperx.load_audio(audio_path)
    waveform = torch.from_numpy(audio).unsqueeze(0)
    kwargs = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    log("开始说话人识别 ...")
    diarization = pipeline({"waveform": waveform, "sample_rate": 16000}, **kwargs)
    diarization = getattr(diarization, "exclusive_speaker_diarization", diarization)

    turns: list[tuple[float, float, str]] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        turns.append((float(turn.start), float(turn.end), str(speaker)))
    log(f"说话人识别完成，时间段: {len(turns)}")
    return turns


def pyannote_cache_status() -> dict:
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache("pyannote/speaker-diarization-community-1", "config.yaml")
        ok = isinstance(path, str) and Path(path).exists()
        return {
            "ready": ok,
            "label": "pyannote/speaker-diarization-community-1",
            "detail": path if ok else "未在本地缓存中找到 config.yaml",
        }
    except Exception as exc:
        return {"ready": False, "label": "pyannote/speaker-diarization-community-1", "detail": str(exc)}


def prepare_pyannote_model(hf_token: str, log) -> dict:
    if not hf_token.strip():
        raise RuntimeError("请先填写 Hugging Face Token。")
    from pyannote.audio import Pipeline

    log("下载/检查 pyannote 说话人模型 ...")
    Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=hf_token.strip())
    status = pyannote_cache_status()
    if not status["ready"]:
        status["ready"] = True
        status["detail"] = "模型已通过 pyannote 加载，缓存状态由 Hugging Face 管理。"
    return status


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers_to_subtitles(
    subtitles: list[Subtitle],
    speaker_turns: list[tuple[float, float, str]],
) -> list[Subtitle]:
    assigned: list[Subtitle] = []
    for sub in subtitles:
        best_speaker: str | None = None
        best_overlap = 0.0
        for start, end, speaker in speaker_turns:
            overlap = overlap_seconds(sub.start, sub.end, start, end)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker
        assigned.append(Subtitle(sub.index, sub.start, sub.end, sub.text, best_speaker or sub.speaker))
    return assigned


def export_speaker_files(subtitles: list[Subtitle], output_path: str, log) -> None:
    grouped: dict[str, list[Subtitle]] = {}
    for sub in subtitles:
        if sub.speaker:
            grouped.setdefault(sub.speaker, []).append(sub)
    if not grouped:
        return

    base = Path(output_path)
    for speaker, items in grouped.items():
        safe_speaker = re.sub(r"[^A-Za-z0-9_.-]+", "_", speaker).strip("_") or "speaker"
        srt_path = base.with_name(f"{base.stem}.{safe_speaker}.srt")
        txt_path = base.with_name(f"{base.stem}.{safe_speaker}.txt")
        renumbered = [
            Subtitle(idx, item.start, item.end, item.text, item.speaker)
            for idx, item in enumerate(items, start=1)
        ]
        srt_path.write_text(srt_from_subtitles(renumbered), encoding="utf-8-sig")
        txt_lines = [
            f"{format_timestamp(item.start)} --> {format_timestamp(item.end)} {item.text}"
            for item in items
        ]
        txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8-sig")
    log(f"已按说话人导出 {len(grouped)} 组 SRT/TXT。")


MODEL_FILE_GROUPS = [
    ("model.bin", ["model.bin"]),
    ("config.json", ["config.json"]),
    ("tokenizer.json", ["tokenizer.json"]),
    ("preprocessor_config.json", ["preprocessor_config.json"]),
    ("vocabulary.*", ["vocabulary.json", "vocabulary.txt"]),
]
MODEL_SIDEcar_FILES = ["config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"]


def default_user_model_dir() -> Path:
    return DEFAULT_MODEL_ROOT / DEFAULT_MODEL_DIRNAME


def default_model_root_dir() -> Path:
    return DEFAULT_MODEL_ROOT


def model_file_status(model_path: str = "") -> dict:
    path = Path((model_path or "").strip()) if (model_path or "").strip() else default_user_model_dir()
    files = []
    complete = 0
    for label, candidates in MODEL_FILE_GROUPS:
        found = next((name for name in candidates if (path / name).exists()), "")
        ok = bool(found)
        complete += 1 if ok else 0
        files.append({"label": label, "ok": ok, "found": found})
    return {
        "path": str(path),
        "total": len(MODEL_FILE_GROUPS),
        "complete": complete,
        "ready": complete == len(MODEL_FILE_GROUPS),
        "files": files,
    }


def download_faster_whisper_model(
    model_name: str,
    output_dir: Path,
    log,
    progress=None,
    progress_start: float = 0,
    progress_end: float = 100,
) -> str:
    from faster_whisper import utils as fw_utils
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    repo_id = fw_utils._MODELS.get(model_name, model_name)
    allow_patterns = [
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.*",
    ]

    class GuiTqdm(tqdm):
        def update(self, n=1):
            result = super().update(n)
            if progress and self.total:
                ratio = min(1.0, max(0.0, float(self.n) / float(self.total)))
                value = progress_start + (progress_end - progress_start) * ratio
                progress(value, f"下载模型文件: {int(ratio * 100)}%")
            return result

    if progress:
        progress(progress_start, "准备下载模型 ...")
    log(f"从 Hugging Face 下载模型: {repo_id}")
    return snapshot_download(
        repo_id,
        local_dir=str(output_dir),
        local_dir_use_symlinks=False,
        allow_patterns=allow_patterns,
        tqdm_class=GuiTqdm,
        local_files_only=False,
    )


def prepare_model_files(model_path: str, log, progress=None) -> str:
    raw_path = (model_path or "").strip()
    path = Path(raw_path) if raw_path else default_user_model_dir()
    if not path.exists():
        log(f"模型目录不存在，将自动下载到: {path}")
        path.mkdir(parents=True, exist_ok=True)
        download_faster_whisper_model(DEFAULT_MODEL_REPO, path, log, progress, 5, 98)
        log("模型下载完成。")
        if progress:
            progress(100, "模型下载完成")
        return str(path)
    if path.is_file():
        raise RuntimeError("faster-whisper 需要选择模型目录，不是单独的 model.bin 文件。")
    if (path / "model.bin").exists():
        missing = [name for name in MODEL_SIDEcar_FILES[:3] if not (path / name).exists()]
        if missing:
            log("当前目录只有部分模型文件，缺少: " + ", ".join(missing))
            log("尝试从 faster-whisper 的 large-v3-turbo 仓库补齐小文件 ...")
            download_faster_whisper_model(DEFAULT_MODEL_REPO, path, log, progress, 5, 98)
            log("模型文件补齐完成。")
            if progress:
                progress(100, "模型文件补齐完成")
    else:
        log(f"模型目录缺少 model.bin，将自动下载到: {path}")
        download_faster_whisper_model(DEFAULT_MODEL_REPO, path, log, progress, 5, 98)
        log("模型下载完成。")
        if progress:
            progress(100, "模型下载完成")
    return str(path)


def run_srt_job(config: dict, log, progress=None) -> str:
    check_cancelled()
    audio = str(config.get("audio_path", "")).strip()
    output = str(config.get("output_path", "")).strip()
    script = str(config.get("script", "")).strip()
    mode = str(config.get("mode", "auto")).strip().lower() or "auto"
    model_path = str(config.get("model_path", "")).strip() or str(default_user_model_dir())
    device = str(config.get("device", "cuda")).strip() or "cuda"
    compute_type = str(config.get("compute_type", "float16")).strip() or "float16"
    language = str(config.get("language", "auto")).strip() or "auto"
    output_format = str(config.get("output_format", "srt")).strip().lower() or "srt"
    word_export = str(config.get("word_timestamp_export", "none")).strip().lower() or "none"
    performance_preset = normalize_performance_preset(
        str(config.get("performance_preset", "recommended"))
    )

    if not audio or not Path(audio).exists():
        raise RuntimeError("请先选择音频/视频文件。")
    if not output:
        raise RuntimeError("请设置输出文件路径。")
    if mode not in {"auto", "align", "transcribe"}:
        mode = "auto"
    if word_export not in {"none", "json", "srt"}:
        word_export = "none"
    if output_format not in {"srt", "txt"}:
        output_format = "srt"

    effective_mode = "align" if (mode == "align" or (mode == "auto" and script)) else "transcribe"
    if effective_mode == "align" and not script:
        raise RuntimeError("文稿精对齐模式需要粘贴文稿或选择 TXT 文件。")

    add_cuda_dll_paths()
    from faster_whisper import WhisperModel
    emit_task_event("stage", stage="model", label="准备模型")
    log("加载 faster-whisper 模型 ...")
    if progress:
        progress(3, "检查模型文件 ...")
    model_path = prepare_model_files(model_path, log, progress)
    if progress:
        progress(100, "模型准备完成")
    model, reused_model = MODEL_RUNTIME.get_asr_model(
        model_path,
        device,
        compute_type,
        WhisperModel,
    )
    if reused_model:
        log("已复用当前 faster-whisper 模型，跳过重复加载。")

    transcribe_kwargs = transcribe_options(performance_preset)
    if language and language.lower() != "auto":
        transcribe_kwargs["language"] = language

    word_timestamps: dict[int, list[WordTimestamp] | None] = {}
    if effective_mode == "align":
        log("模式: 文稿匹配精对齐")
    else:
        log(f"模式: 纯音频转 {output_format.upper()}")

    if progress:
        progress(-1, "ASR 识别中 ...")
    emit_task_event("stage", stage="asr", label="ASR 识别")
    log("开始 ASR 取时间轴 ...")
    segments_iter, info = model.transcribe(audio, **transcribe_kwargs)
    segments = filter_trailing_asr_hallucinations(
        consume_transcription_segments(segments_iter), log
    )
    detected_language = getattr(info, "language", "unknown")
    log(f"识别语言: {detected_language}，片段数: {len(segments)}")

    if effective_mode == "align":
        _, asr_tokens = extract_asr_tokens(segments)
        if not asr_tokens:
            raise RuntimeError("没有提取到可对齐的 ASR token。")
        log(f"ASR token: {len(asr_tokens)}，开始匹配文稿 ...")
        script_tokens = assign_script_times(script, asr_tokens)
        line_count = len(script_line_ranges(script))
        if line_count > 1:
            log(f"检测到文稿非空行: {line_count}，按文稿行生成 SRT。")
            subtitles = subtitles_from_script_lines(script, script_tokens)
        else:
            subtitles = subtitles_from_audio_segments(script, script_tokens, segments)
    else:
        subtitles = subtitles_from_asr_segments(segments)

    should_run_whisperx = word_export != "none" or (
        effective_mode == "align" and bool(config.get("whisperx_enabled", True))
    )
    if should_run_whisperx:
        check_cancelled()
        emit_task_event("stage", stage="align", label="WhisperX 精对齐")
        if progress:
            progress(58, "WhisperX 精对齐 ...")
        align_language = language if language and language.lower() != "auto" else detected_language
        subtitles, word_timestamps = run_whisperx_alignment(audio, subtitles, align_language, device, log)

    if not subtitles:
        raise RuntimeError("没有生成可用字幕条目，请检查音频是否可识别。")
    audio_end = max((float(getattr(segment, "end", 0.0) or 0.0) for segment in segments), default=None)
    subtitles = repair_subtitle_text_boundaries(subtitles)
    subtitles, repaired_timing_count = repair_subtitle_timings(subtitles, audio_end=audio_end)
    if repaired_timing_count:
        log(f"已整理时间轴，修复重叠/回退: {repaired_timing_count} 处。")
    log(f"生成字幕条目: {len(subtitles)}")
    if bool(config.get("ai_enabled", True)):
        check_cancelled()
        emit_task_event("stage", stage="ai", label="AI 校对")
        if progress:
            progress(76, "AI 校对中 ...")
        base_url = str(config.get("base_url", "")).strip()
        api_key = str(config.get("api_key", "")).strip()
        ai_model = str(config.get("ai_model", "")).strip()
        if not api_key or not base_url or not ai_model:
            raise RuntimeError("启用 AI 修正/校对前，请先配置 API Key、Base URL 和模型名。")
        log("AI 校对文稿字幕 ..." if effective_mode == "align" else "AI 修正 ASR 字幕 ...")
        subtitles = correct_subtitles_with_ai(
            subtitles,
            base_url,
            api_key,
            ai_model,
            str(config.get("system_prompt", "")).strip() or DEFAULT_SYSTEM_PROMPT,
            log,
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    if bool(config.get("diarization_enabled", False)):
        check_cancelled()
        emit_task_event("stage", stage="speaker", label="说话人识别")
        speaker_turns = run_speaker_diarization(
            audio,
            str(config.get("hf_token", "")).strip(),
            device,
            parse_optional_int(str(config.get("min_speakers", "")).strip()),
            parse_optional_int(str(config.get("max_speakers", "")).strip()),
            log,
        )
        subtitles = assign_speakers_to_subtitles(subtitles, speaker_turns)
        subtitles, repaired_timing_count = repair_subtitle_timings(subtitles, audio_end=audio_end)
        if repaired_timing_count:
            log(f"说话人识别后再次整理时间轴: {repaired_timing_count} 处。")

    check_cancelled()
    emit_task_event("stage", stage="export", label="导出结果")
    if progress:
        progress(94, "正在导出结果 ...")
    if output_format == "txt":
        output_text = transcript_text_from_subtitles(subtitles)
    else:
        output_text = srt_from_subtitles(subtitles)
    Path(output).write_text(output_text, encoding="utf-8-sig")
    if word_export != "none":
        word_timestamps = repair_word_timestamps(subtitles, word_timestamps, log)
        export_word_timestamps(subtitles, word_timestamps, output, word_export, log)
    if bool(config.get("diarization_enabled", False)):
        export_speaker_files(subtitles, output, log)
    log(f"{output_format.upper()} 已保存: {output}")
    emit_task_event("stage", stage="done", label="已完成")
    if progress:
        progress(100, f"{output_format.upper()} 已完成")
    return output


def discover_batch_media(input_dir: str, recursive: bool = True) -> list[Path]:
    root = Path(input_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError("请选择有效的批量媒体目录。")
    candidates = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        (path for path in candidates if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES),
        key=lambda path: str(path.relative_to(root)).casefold(),
    )


def batch_txt_output_path(media_path: Path, input_root: Path, output_root: Path) -> Path:
    return batch_output_path(media_path, input_root, output_root, "txt")


def batch_output_path(
    media_path: Path, input_root: Path, output_root: Path, extension: str
) -> Path:
    relative = media_path.relative_to(input_root)
    return output_root / relative.parent / f"{relative.stem}.{extension.lstrip('.')}"


def batch_txt_output_paths(
    media_files: list[Path], input_root: Path, output_root: Path
) -> dict[Path, Path]:
    return batch_output_paths(media_files, input_root, output_root, "txt")


def batch_output_paths(
    media_files: list[Path], input_root: Path, output_root: Path, extension: str
) -> dict[Path, Path]:
    extension = extension.lstrip(".")
    candidates = {
        media_path: batch_output_path(media_path, input_root, output_root, extension)
        for media_path in media_files
    }
    counts: dict[str, int] = {}
    for candidate in candidates.values():
        key = str(candidate).casefold()
        counts[key] = counts.get(key, 0) + 1
    resolved: dict[Path, Path] = {}
    for media_path, candidate in candidates.items():
        if counts[str(candidate).casefold()] > 1:
            candidate = candidate.with_name(
                f"{media_path.stem}{media_path.suffix.lower()}.{extension}"
            )
        resolved[media_path] = candidate
    return resolved


def plain_text_from_subtitles(subtitles: list[Subtitle]) -> str:
    lines = [sub.text.strip() for sub in subtitles if sub.text.strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def transcript_text_from_subtitles(subtitles: list[Subtitle]) -> str:
    """Render the home-page TXT export, preserving speaker labels when present."""
    lines = []
    for sub in subtitles:
        text = sub.text.strip()
        if not text:
            continue
        lines.append(f"[{sub.speaker}] {text}" if sub.speaker else text)
    return "\n".join(lines) + ("\n" if lines else "")


def run_batch_txt_job(config: dict, log, progress=None) -> str:
    check_cancelled()
    input_value = str(config.get("batch_input_dir", "")).strip()
    if not input_value:
        raise RuntimeError("请先选择批量媒体目录。")
    input_root = Path(input_value).expanduser().resolve()
    output_value = str(config.get("batch_output_dir", "")).strip()
    output_root = Path(output_value).expanduser().resolve() if output_value else input_root / "txt_outputs"
    recursive = bool(config.get("batch_recursive", True))
    skip_existing = bool(config.get("batch_skip_existing", True))
    model_path = str(config.get("model_path", "")).strip() or str(default_user_model_dir())
    device = str(config.get("device", "cuda")).strip() or "cuda"
    compute_type = str(config.get("compute_type", "float16")).strip() or "float16"
    base_url = str(config.get("base_url", "")).strip()
    api_key = str(config.get("api_key", "")).strip()
    ai_model = str(config.get("ai_model", "")).strip()
    system_prompt = str(config.get("system_prompt", "")).strip() or DEFAULT_SYSTEM_PROMPT
    performance_preset = normalize_performance_preset(
        str(config.get("performance_preset", "recommended"))
    )

    media_files = discover_batch_media(str(input_root), recursive)
    media_files = select_requested_media(
        media_files, input_root, config.get("batch_only_files")
    )
    if not media_files:
        raise RuntimeError("所选目录中没有找到支持的音频或视频文件。")
    if not base_url or not api_key or not ai_model:
        raise RuntimeError("批量模式需要 LLM 纠错，请先在设置页配置 Base URL、API Key 和模型名。")

    output_root.mkdir(parents=True, exist_ok=True)
    output_paths = batch_txt_output_paths(media_files, input_root, output_root)
    emit_task_event(
        "batch_discovered",
        total=len(media_files),
        files=[str(path.relative_to(input_root)) for path in media_files],
    )
    error_report = output_root / BATCH_ERROR_FILENAME
    log(f"批量模式: 找到 {len(media_files)} 个媒体文件，语言将逐个自动检测。")
    log(f"TXT 输出目录: {output_root}")
    if progress:
        progress(1, f"准备批量任务，共 {len(media_files)} 个文件")

    add_cuda_dll_paths()
    from faster_whisper import WhisperModel
    emit_task_event("stage", stage="model", label="准备批量 ASR 模型")

    def model_progress(value: int, message: str) -> None:
        if progress:
            progress(max(1, min(5, int(max(0, value) * 0.05))), message)

    model_path = prepare_model_files(model_path, log, model_progress)
    log("批量任务只加载一次 faster-whisper 模型 ...")
    model, reused_model = MODEL_RUNTIME.get_asr_model(
        model_path,
        device,
        compute_type,
        WhisperModel,
    )
    if reused_model:
        log("已复用当前 faster-whisper 模型。")
    transcribe_kwargs = transcribe_options(performance_preset)

    successes = 0
    skipped = 0
    failures: list[tuple[Path, str]] = []
    total = len(media_files)
    for position, media_path in enumerate(media_files, start=1):
        check_cancelled()
        relative = media_path.relative_to(input_root)
        output_path = output_paths[media_path]
        percent_start = 5 + int((position - 1) * 95 / total)
        if skip_existing and output_path.exists():
            skipped += 1
            log(f"[{position}/{total}] 跳过已有文件: {relative} -> {output_path.name}")
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="skipped", output=str(output_path),
            )
            if progress:
                progress(5 + int(position * 95 / total), f"已跳过 {position}/{total}: {relative.name}")
            continue

        try:
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="running", output=str(output_path),
            )
            if progress:
                progress(percent_start, f"ASR {position}/{total}: {relative.name}")
            emit_task_event("stage", stage="asr", label=f"ASR {position}/{total}")
            log(f"[{position}/{total}] ASR: {relative}")
            segments_iter, info = model.transcribe(
                str(media_path),
                **transcribe_kwargs,
            )
            segments = filter_trailing_asr_hallucinations(
                consume_transcription_segments(segments_iter), log
            )
            detected_language = str(getattr(info, "language", "unknown") or "unknown")
            subtitles = repair_subtitle_text_boundaries(subtitles_from_asr_segments(segments))
            if not subtitles:
                raise RuntimeError("没有识别到可用文本")
            log(
                f"[{position}/{total}] 检测语言: {detected_language}，"
                f"ASR 片段: {len(subtitles)}，开始 LLM 纠错 ..."
            )
            emit_task_event("stage", stage="ai", label=f"AI 校对 {position}/{total}")
            subtitles = correct_subtitles_with_ai(
                subtitles,
                base_url,
                api_key,
                ai_model,
                system_prompt,
                log,
            )
            text = plain_text_from_subtitles(subtitles)
            if not text.strip():
                raise RuntimeError("LLM 纠错后没有可写入文本")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            emit_task_event("stage", stage="export", label=f"导出 {position}/{total}")
            output_path.write_text(text, encoding="utf-8-sig")
            successes += 1
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="completed", output=str(output_path),
            )
            log(f"[{position}/{total}] 完成: {relative} -> {output_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            message = str(exc).replace("\r", " ").replace("\n", " ")
            failures.append((relative, message))
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="failed", output=str(output_path),
                message=message,
            )
            log(f"[{position}/{total}] 失败但继续处理: {relative}，原因: {message}")
            log(traceback.format_exc())
        if progress:
            progress(5 + int(position * 95 / total), f"批量进度 {position}/{total}")

    if failures:
        lines = [
            "批量转 TXT 失败清单",
            f"输入目录: {input_root}",
            f"成功: {successes}，跳过: {skipped}，失败: {len(failures)}",
            "",
        ]
        lines.extend(f"{path}\t{message}" for path, message in failures)
        error_report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        log(f"失败清单已保存: {error_report}")
    elif error_report.exists():
        try:
            error_report.unlink()
        except OSError:
            pass

    summary = f"批量任务完成：成功 {successes}，跳过 {skipped}，失败 {len(failures)}。"
    log(summary)
    if progress:
        progress(100, summary)
    if not successes and not skipped and failures:
        raise RuntimeError(f"所有文件均处理失败，请查看: {error_report}")
    emit_task_event("stage", stage="done", label="批量任务已完成")
    return str(output_root)


def run_batch_srt_job(config: dict, log, progress=None) -> str:
    check_cancelled()
    input_value = str(config.get("batch_input_dir", "")).strip()
    if not input_value:
        raise RuntimeError("请先选择批量媒体目录。")
    input_root = Path(input_value).expanduser().resolve()
    output_value = str(config.get("batch_output_dir", "")).strip()
    output_root = Path(output_value).expanduser().resolve() if output_value else input_root / "srt_outputs"
    recursive = bool(config.get("batch_recursive", True))
    skip_existing = bool(config.get("batch_skip_existing", True))
    model_path = str(config.get("model_path", "")).strip() or str(default_user_model_dir())
    device = str(config.get("device", "cuda")).strip() or "cuda"
    compute_type = str(config.get("compute_type", "float16")).strip() or "float16"
    base_url = str(config.get("base_url", "")).strip()
    api_key = str(config.get("api_key", "")).strip()
    ai_model = str(config.get("ai_model", "")).strip()
    system_prompt = str(config.get("system_prompt", "")).strip() or DEFAULT_SYSTEM_PROMPT
    performance_preset = normalize_performance_preset(
        str(config.get("performance_preset", "recommended"))
    )

    media_files = discover_batch_media(str(input_root), recursive)
    media_files = select_requested_media(
        media_files, input_root, config.get("batch_only_files")
    )
    if not media_files:
        raise RuntimeError("所选目录中没有找到支持的音频或视频文件。")
    if not base_url or not api_key or not ai_model:
        raise RuntimeError("批量 SRT 需要 LLM 纠错，请先在设置页配置 Base URL、API Key 和模型名。")

    output_root.mkdir(parents=True, exist_ok=True)
    output_paths = batch_output_paths(media_files, input_root, output_root, "srt")
    emit_task_event(
        "batch_discovered",
        total=len(media_files),
        files=[str(path.relative_to(input_root)) for path in media_files],
    )
    error_report = output_root / BATCH_SRT_ERROR_FILENAME
    log(f"批量 SRT: 找到 {len(media_files)} 个媒体文件，语言将逐个自动检测。")
    log(f"SRT 输出目录: {output_root}")
    if progress:
        progress(1, f"准备批量精对齐 SRT，共 {len(media_files)} 个文件")

    add_cuda_dll_paths()
    from faster_whisper import WhisperModel
    emit_task_event("stage", stage="model", label="准备批量 ASR 模型")

    def model_progress(value: int, message: str) -> None:
        if progress:
            progress(max(1, min(5, int(max(0, value) * 0.05))), message)

    model_path = prepare_model_files(model_path, log, model_progress)
    log("批量 SRT 只加载一次 faster-whisper 模型 ...")
    model, reused_model = MODEL_RUNTIME.get_asr_model(
        model_path,
        device,
        compute_type,
        WhisperModel,
    )
    if reused_model:
        log("已复用当前 faster-whisper 模型。")
    transcribe_kwargs = transcribe_options(performance_preset)
    align_model_cache = MODEL_RUNTIME.align_models

    successes = 0
    skipped = 0
    failures: list[tuple[Path, str]] = []
    total = len(media_files)
    for position, media_path in enumerate(media_files, start=1):
        check_cancelled()
        relative = media_path.relative_to(input_root)
        output_path = output_paths[media_path]
        percent_start = 5 + int((position - 1) * 95 / total)
        if skip_existing and output_path.exists():
            skipped += 1
            log(f"[{position}/{total}] 跳过已有 SRT: {relative} -> {output_path.name}")
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="skipped", output=str(output_path),
            )
            if progress:
                progress(5 + int(position * 95 / total), f"已跳过 {position}/{total}: {relative.name}")
            continue

        try:
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="running", output=str(output_path),
            )
            if progress:
                progress(percent_start, f"ASR {position}/{total}: {relative.name}")
            emit_task_event("stage", stage="asr", label=f"ASR {position}/{total}")
            log(f"[{position}/{total}] ASR: {relative}")
            segments_iter, info = model.transcribe(
                str(media_path),
                **transcribe_kwargs,
            )
            segments = filter_trailing_asr_hallucinations(
                consume_transcription_segments(segments_iter), log
            )
            detected_language = str(getattr(info, "language", "unknown") or "unknown")
            subtitles = repair_subtitle_text_boundaries(subtitles_from_asr_segments(segments))
            if not subtitles:
                raise RuntimeError("没有识别到可用字幕")
            log(
                f"[{position}/{total}] 检测语言: {detected_language}，ASR 片段: {len(subtitles)}，"
                "开始 WhisperX 精对齐 ..."
            )
            emit_task_event("stage", stage="align", label=f"精对齐 {position}/{total}")
            subtitles, _word_timestamps = run_whisperx_alignment(
                str(media_path),
                subtitles,
                detected_language,
                device,
                log,
                align_model_cache=align_model_cache,
                strict=True,
            )
            audio_end = max(
                (float(getattr(segment, "end", 0.0) or 0.0) for segment in segments),
                default=None,
            )
            subtitles, repaired_count = repair_subtitle_timings(subtitles, audio_end=audio_end)
            if repaired_count:
                log(f"[{position}/{total}] 精对齐后整理时间轴: {repaired_count} 处。")
            log(f"[{position}/{total}] WhisperX 完成，开始 LLM 纠错 ...")
            emit_task_event("stage", stage="ai", label=f"AI 校对 {position}/{total}")
            subtitles = correct_subtitles_with_ai(
                subtitles,
                base_url,
                api_key,
                ai_model,
                system_prompt,
                log,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            emit_task_event("stage", stage="export", label=f"导出 {position}/{total}")
            output_path.write_text(srt_from_subtitles(subtitles), encoding="utf-8-sig")
            successes += 1
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="completed", output=str(output_path),
            )
            log(f"[{position}/{total}] 完成精对齐 SRT: {relative} -> {output_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            message = str(exc).replace("\r", " ").replace("\n", " ")
            failures.append((relative, message))
            emit_task_event(
                "file_status", position=position, total=total,
                path=str(relative), status="failed", output=str(output_path),
                message=message,
            )
            log(f"[{position}/{total}] 失败但继续处理: {relative}，原因: {message}")
            log(traceback.format_exc())
        if progress:
            progress(5 + int(position * 95 / total), f"批量 SRT 进度 {position}/{total}")

    if failures:
        lines = [
            "批量精对齐 SRT 失败清单",
            f"输入目录: {input_root}",
            f"成功: {successes}，跳过: {skipped}，失败: {len(failures)}",
            "",
        ]
        lines.extend(f"{path}\t{message}" for path, message in failures)
        error_report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        log(f"失败清单已保存: {error_report}")
    elif error_report.exists():
        try:
            error_report.unlink()
        except OSError:
            pass

    summary = f"批量精对齐 SRT 完成：成功 {successes}，跳过 {skipped}，失败 {len(failures)}。"
    log(summary)
    if progress:
        progress(100, summary)
    if not successes and not skipped and failures:
        raise RuntimeError(f"所有文件均处理失败，请查看: {error_report}")
    emit_task_event("stage", stage="done", label="批量任务已完成")
    return str(output_root)


def call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    timeout: int = 120,
) -> AICompletion:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    elif base_url.endswith("/v1"):
        endpoint = base_url + "/chat/completions"
    else:
        endpoint = base_url + "/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    check_cancelled()
    data = AI_TRANSPORT.post_json(endpoint, payload, api_key, timeout)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("AI API 返回缺少 choices[0]。")
    choice = choices[0]
    content = choice.get("message", {}).get("content", "")
    return AICompletion(
        content=str(content or "").strip(),
        finish_reason=str(choice.get("finish_reason") or "").strip(),
    )


def parse_correction_response(text: str, expected_indexes: set[int]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        left, right = line.split("|", 1)
        left = left.strip()
        if not left.isdigit():
            continue
        index = int(left)
        if index in expected_indexes:
            mapping[index] = right.strip()
    return mapping


def missing_correction_indexes(mapping: dict[int, str], expected_indexes: set[int]) -> list[int]:
    return sorted(expected_indexes - set(mapping))


def correction_text_issue(original: str, corrected: str) -> str:
    original = original.strip()
    corrected = corrected.strip()
    if not corrected:
        return "返回文本为空"
    original_compact = re.sub(r"\s+", "", original)
    corrected_compact = re.sub(r"\s+", "", corrected)
    if len(original_compact) >= 8:
        ratio = len(corrected_compact) / max(1, len(original_compact))
        if ratio < 0.55:
            return f"长度仅为原文的 {ratio:.0%}"
        if ratio > 1.80:
            return f"长度达到原文的 {ratio:.0%}"

    original_words = re.findall(r"\w+", original, flags=re.UNICODE)
    corrected_words = re.findall(r"\w+", corrected, flags=re.UNICODE)
    if len(original_words) >= 4 and len(corrected_words) < max(2, int(len(original_words) * 0.5)):
        return f"词数从 {len(original_words)} 降到 {len(corrected_words)}"
    return ""


def invalid_correction_indexes(
    batch: list[Subtitle], mapping: dict[int, str]
) -> dict[int, str]:
    invalid: dict[int, str] = {}
    for sub in batch:
        if sub.index not in mapping:
            continue
        issue = correction_text_issue(sub.text, mapping[sub.index])
        if issue:
            invalid[sub.index] = issue
    return invalid


def completion_finish_reason_is_valid(finish_reason: str) -> bool:
    normalized = (finish_reason or "").strip().lower()
    return normalized in {"", "stop", "completed", "complete", "end_turn"}


def correct_ai_batch_strict(
    batch: list[Subtitle],
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    log,
    attempts: int = 5,
) -> dict[int, str]:
    expected = {sub.index for sub in batch}
    completion_marker = f"__SRTMATCHER_COMPLETE_{batch[0].index}_{batch[-1].index}__"
    entries = "\n".join(f"{sub.index}|{sub.text}" for sub in batch)
    user_content = (
        f"{entries}\n\n"
        "完成全部条目后，必须在最后单独一行原样输出以下完成标记：\n"
        f"{completion_marker}"
    )

    for attempt in range(1, attempts + 1):
        check_cancelled()
        log(f"AI 校对字幕 {batch[0].index}-{batch[-1].index}，第 {attempt} 次 ...")
        completion = call_openai_compatible(base_url, api_key, model, system_prompt, user_content)
        if isinstance(completion, str):
            completion = AICompletion(completion)
        mapping = parse_correction_response(completion.content, expected)
        missing = missing_correction_indexes(mapping, expected)
        invalid = invalid_correction_indexes(batch, mapping)
        marker_found = completion_marker in {
            line.strip() for line in completion.content.splitlines()
        }
        finish_valid = completion_finish_reason_is_valid(completion.finish_reason)
        if (
            not missing
            and not invalid
            and len(mapping) == len(batch)
            and marker_found
            and finish_valid
        ):
            log(
                f"AI 自检通过: {len(mapping)}/{len(batch)} 条完整，"
                f"finish_reason={completion.finish_reason or '未提供'}。"
            )
            return mapping

        issues: list[str] = []
        if not finish_valid:
            issues.append(f"finish_reason={completion.finish_reason or '未知'}")
        if not marker_found:
            issues.append("缺少批次完成标记")
        if missing:
            preview = ", ".join(str(i) for i in missing[:8])
            if len(missing) > 8:
                preview += " ..."
            issues.append(f"缺少 {len(missing)} 条: {preview}")
        if invalid:
            preview = ", ".join(
                f"{index}({reason})" for index, reason in list(invalid.items())[:6]
            )
            if len(invalid) > 6:
                preview += " ..."
            issues.append(f"疑似截断/改写 {len(invalid)} 条: {preview}")
        log("AI 自检未通过：" + "；".join(issues) + "，准备重试。")
        time.sleep(min(1 + attempt, 5))
        check_cancelled()

    if len(batch) > 1:
        mid = len(batch) // 2
        log(f"批次 {batch[0].index}-{batch[-1].index} 多次失败，拆成更小批次重试。")
        left = correct_ai_batch_strict(batch[:mid], base_url, api_key, model, system_prompt, log, attempts)
        right = correct_ai_batch_strict(batch[mid:], base_url, api_key, model, system_prompt, log, attempts)
        return {**left, **right}

    raise RuntimeError(
        f"AI 多次未完整返回条目 {batch[0].index}，已停止保存，避免写入截断结果。"
    )


def correct_subtitles_with_ai(
    subtitles: list[Subtitle],
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    log,
    batch_size: int = 20,
    parallelism: int = 2,
) -> list[Subtitle]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    corrected = list(subtitles)
    batches = [
        corrected[start : start + batch_size]
        for start in range(0, len(corrected), batch_size)
    ]
    if not batches:
        return corrected

    token = current_cancellation_token()

    def correct_one(batch: list[Subtitle]) -> dict[int, str]:
        with task_context(token):
            return correct_ai_batch_strict(
                batch, base_url, api_key, model, system_prompt, log
            )

    mappings: dict[int, dict[int, str]] = {}
    worker_count = max(1, min(int(parallelism or 1), 2, len(batches)))
    if worker_count == 1:
        for index, batch in enumerate(batches):
            check_cancelled()
            mappings[index] = correct_one(batch)
    else:
        log(f"AI 校对启用 {worker_count} 个受控并发请求。")
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="srt-ai") as pool:
            futures = {pool.submit(correct_one, batch): index for index, batch in enumerate(batches)}
            for future in as_completed(futures):
                check_cancelled()
                mappings[futures[future]] = future.result()

    for index in range(len(batches)):
        corrected = replace_subtitle_texts(corrected, mappings[index])
    return corrected

LANGUAGE_CHOICES = [
    ("自动检测", "auto"),
    ("中文", "zh"),
    ("英语", "en"),
    ("日语", "ja"),
    ("韩语", "ko"),
    ("泰语", "th"),
    ("西班牙语", "es"),
    ("法语", "fr"),
    ("德语", "de"),
    ("俄语", "ru"),
    ("葡萄牙语", "pt"),
    ("越南语", "vi"),
]
LANGUAGE_TO_CODE = dict(LANGUAGE_CHOICES)
CODE_TO_LANGUAGE = {code: label for label, code in LANGUAGE_CHOICES}


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        add_cuda_dll_paths()
        import ctranslate2

        result = {
            "ctranslate2": ctranslate2.__version__,
            "cuda_devices": ctranslate2.get_cuda_device_count(),
        }
        try:
            import torch

            result["torch"] = torch.__version__
            result["torch_cuda_available"] = torch.cuda.is_available()
            result["torch_cuda"] = torch.version.cuda
        except Exception as exc:
            result["torch_error"] = str(exc)
        try:
            import whisperx

            result["whisperx"] = getattr(whisperx, "__version__", "available")
        except Exception as exc:
            result["whisperx_error"] = str(exc)
        try:
            import pyannote.audio

            result["pyannote_audio"] = getattr(pyannote.audio, "__version__", "available")
        except Exception as exc:
            result["pyannote_audio_error"] = str(exc)
            result["pyannote_audio_traceback"] = traceback.format_exc()
        if "--self-test-file" in sys.argv:
            target = Path(sys.argv[sys.argv.index("--self-test-file") + 1])
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0)

    print("SRTMatcher core module. Run qt_app.py to start the GUI.")
