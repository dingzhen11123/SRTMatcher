from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
INSTALL_ROOT = APP_DIR.parent if APP_DIR.name.lower() == "app" else APP_DIR
APP_NAME = "SRTMatcher"
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
    "device",
    "compute_type",
    "language",
    "mode",
    "max_chars",
    "ai_enabled",
    "whisperx_enabled",
    "diarization_enabled",
    "hf_token",
    "min_speakers",
    "max_speakers",
    "base_url",
    "api_key",
    "ai_model",
    "system_prompt",
}


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
    if version >= (12, 6):
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
6. 不要输出任何解释、说明、空行或多余字符。

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


def resolve_torch_device(device_choice: str, log) -> str:
    import torch

    wants_cuda = device_choice.lower() == "cuda"
    if wants_cuda and torch.cuda.is_available():
        return "cuda"
    if wants_cuda:
        log("WhisperX / 说话人识别当前未检测到 PyTorch CUDA，将使用 CPU。ASR 的 CTranslate2 CUDA 不受影响。")
    return "cpu"


def run_whisperx_alignment(
    audio_path: str,
    subtitles: list[Subtitle],
    language_code: str,
    device_choice: str,
    log,
    pre_pad: float = 0.2,
    post_pad: float = 0.5,
) -> list[Subtitle]:
    if not subtitles:
        return subtitles

    supported_languages = {
        "en", "fr", "de", "es", "it", "ja", "zh", "nl", "uk", "pt", "ar", "cs", "ru", "pl", "hu", "fi",
        "fa", "el", "tr", "da", "he", "vi", "ko", "ur", "te", "hi", "ca", "ml", "no", "nn", "sk", "sl",
        "hr", "ro", "eu", "gl", "ka", "lv", "tl", "sv", "id",
    }
    language_code = (language_code or "").lower().split("-")[0]
    if language_code not in supported_languages:
        log(f"WhisperX 暂无默认对齐模型语言: {language_code or 'unknown'}，保留现有时间轴。")
        return subtitles

    try:
        import whisperx

        device = resolve_torch_device(device_choice, log)
        log(f"WhisperX 强制对齐加载语言模型: {language_code} ({device}) ...")
        align_model, metadata = whisperx.load_align_model(language_code=language_code, device=device)
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

        log(f"WhisperX 对齐完成，更新 {improved}/{len(subtitles)} 条时间轴。")
        return aligned
    except Exception as exc:
        log(f"WhisperX 对齐失败，保留现有时间轴: {exc}")
        log(traceback.format_exc())
        return subtitles


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
    log(f"加载 pyannote 说话人识别模型 ({device}) ...")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=hf_token.strip())
    if device == "cuda":
        pipeline.to(torch.device("cuda"))

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
    audio = str(config.get("audio_path", "")).strip()
    output = str(config.get("output_path", "")).strip()
    script = str(config.get("script", "")).strip()
    mode = str(config.get("mode", "auto")).strip().lower() or "auto"
    model_path = str(config.get("model_path", "")).strip() or str(default_user_model_dir())
    device = str(config.get("device", "cuda")).strip() or "cuda"
    compute_type = str(config.get("compute_type", "float16")).strip() or "float16"
    language = str(config.get("language", "auto")).strip() or "auto"

    if not audio or not Path(audio).exists():
        raise RuntimeError("请先选择音频/视频文件。")
    if not output:
        raise RuntimeError("请设置输出 SRT 路径。")
    if mode not in {"auto", "align", "transcribe"}:
        mode = "auto"

    effective_mode = "align" if (mode == "align" or (mode == "auto" and script)) else "transcribe"
    if effective_mode == "align" and not script:
        raise RuntimeError("文稿精对齐模式需要粘贴文稿或选择 TXT 文件。")

    add_cuda_dll_paths()
    from faster_whisper import WhisperModel

    log("加载 faster-whisper 模型 ...")
    if progress:
        progress(3, "检查模型文件 ...")
    model_path = prepare_model_files(model_path, log, progress)
    if progress:
        progress(100, "模型准备完成")
    model = WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        local_files_only=Path(model_path).exists(),
    )

    transcribe_kwargs = {
        "beam_size": 5,
        "word_timestamps": True,
        "vad_filter": True,
    }
    if language and language.lower() != "auto":
        transcribe_kwargs["language"] = language

    if effective_mode == "align":
        log("模式: 文稿匹配精对齐")
    else:
        log("模式: 纯音频转 SRT")

    if progress:
        progress(-1, "ASR 识别中 ...")
    log("开始 ASR 取时间轴 ...")
    segments_iter, info = model.transcribe(audio, **transcribe_kwargs)
    segments = list(segments_iter)
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
            log("未检测到多行文稿，按 ASR 音频片段生成 SRT。")
            subtitles = subtitles_from_audio_segments(script, script_tokens, segments)
        if bool(config.get("whisperx_enabled", True)):
            align_language = language if language and language.lower() != "auto" else detected_language
            subtitles = run_whisperx_alignment(audio, subtitles, align_language, device, log)
    else:
        subtitles = subtitles_from_asr_segments(segments)

    if not subtitles:
        raise RuntimeError("没有生成可用字幕条目，请检查音频是否可识别。")
    audio_end = max((float(getattr(segment, "end", 0.0) or 0.0) for segment in segments), default=None)
    subtitles = repair_subtitle_text_boundaries(subtitles)
    subtitles, repaired_timing_count = repair_subtitle_timings(subtitles, audio_end=audio_end)
    if repaired_timing_count:
        log(f"已整理时间轴，修复重叠/回退: {repaired_timing_count} 处。")
    log(f"生成字幕条目: {len(subtitles)}")

    if bool(config.get("ai_enabled", True)):
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

    Path(output).write_text(srt_from_subtitles(subtitles), encoding="utf-8-sig")
    if bool(config.get("diarization_enabled", False)):
        export_speaker_files(subtitles, output, log)
    log(f"SRT 已保存: {output}")
    return output


def call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    timeout: int = 120,
) -> str:
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
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI API 请求失败：HTTP {exc.code} {detail}") from exc

    return data["choices"][0]["message"]["content"].strip()


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
    user_content = "\n".join(f"{sub.index}|{sub.text}" for sub in batch)

    for attempt in range(1, attempts + 1):
        log(f"AI 校对字幕 {batch[0].index}-{batch[-1].index}，第 {attempt} 次 ...")
        response = call_openai_compatible(base_url, api_key, model, system_prompt, user_content)
        mapping = parse_correction_response(response, expected)
        missing = missing_correction_indexes(mapping, expected)
        if not missing and len(mapping) == len(batch):
            return mapping

        preview = ", ".join(str(i) for i in missing[:8])
        if len(missing) > 8:
            preview += " ..."
        log(f"AI 返回不完整，缺少 {len(missing)} 条: {preview}，准备重试。")
        time.sleep(min(1 + attempt, 5))

    if len(batch) > 1:
        mid = len(batch) // 2
        log(f"批次 {batch[0].index}-{batch[-1].index} 多次失败，拆成更小批次重试。")
        left = correct_ai_batch_strict(batch[:mid], base_url, api_key, model, system_prompt, log, attempts)
        right = correct_ai_batch_strict(batch[mid:], base_url, api_key, model, system_prompt, log, attempts)
        return {**left, **right}

    raise RuntimeError(f"AI 多次未按格式返回字幕 {batch[0].index}，已停止保存，避免输出错误 SRT。")


def correct_subtitles_with_ai(
    subtitles: list[Subtitle],
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    log,
    batch_size: int = 40,
) -> list[Subtitle]:
    corrected = subtitles
    for start in range(0, len(corrected), batch_size):
        batch = corrected[start : start + batch_size]
        mapping = correct_ai_batch_strict(batch, base_url, api_key, model, system_prompt, log)
        corrected = replace_subtitle_texts(corrected, mapping)
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
