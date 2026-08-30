# 字幕多功能工具（SRTMatcher）

“字幕多功能工具”是 SRTMatcher 面向用户显示的中文名。内部程序名、安装目录和配置目录仍保留 `SRTMatcher`，用于兼容已有安装。

SRTMatcher 是一个轻量 GUI 字幕工具，用于：

- 上传音频/视频 + 文稿，在首页二选一导出精对齐 SRT 或 TXT。
- 只上传音频/视频，直接 ASR 并在首页二选一导出 SRT 或 TXT。
- 使用 AI 接口修正 ASR 字幕文本。
- 使用 WhisperX 二次精对齐时间轴。
- 可选使用 pyannote 做说话人识别，并按说话人导出 SRT/TXT。
- 可选把 WhisperX 已计算的逐词/逐字时间戳额外导出为 JSON 或 SRT。
- 批量扫描多语言音频/视频，逐文件 ASR、LLM 纠错，并按原文件名输出 TXT 或精对齐 SRT。

当前推荐交付方式是 **NSIS 小安装包**。安装包本身不内置大模型、不内置 CUDA/cuDNN、不打包 8GB 依赖；首次启动后由软件内的设置页检测和补齐运行环境、模型文件。

## 给用户安装

分发这个文件：

```text
dist\SRTMatcherSetup-<build-id>-<yyyyMMdd-HHmmss>.exe
```

安装包特性：

- 中文 NSIS 安装界面。
- 允许用户自定义安装目录。
- 默认安装目录来自 NSIS 的上次安装记录；首次默认为 `%LOCALAPPDATA%\SRTMatcher`。
- 创建桌面快捷方式和开始菜单快捷方式。
- 支持覆盖安装升级，不需要先卸载。
- 覆盖安装后，启动器会自动刷新新版程序文件，但不会重复安装已有虚拟环境。

建议用户安装后从桌面快捷方式启动：

```text
字幕多功能工具.lnk
```

不要把安装包本身当作日常启动入口。

## 覆盖升级机制

NSIS 安装脚本使用：

```nsis
SetOverwrite on
```

安装包会覆盖安装目录里的 `SRTMatcher.exe`。

启动器还带有构建版本标记：

```python
APP_BUILD_ID = "2026-08-31-logo-c-shortcut-fix"
```

启动时会检查：

```text
安装目录\.runtime-ready
```

覆盖安装时，NSIS 会先删除旧 `.runtime-ready`。启动器还会计算安装包内与安装目录内 `app.py`、`qt_app.py`、`requirements.txt`、`README.md` 的 SHA-256 指纹；只有版本标记和源码指纹都一致时才直接启动。否则会原子替换新版程序文件，再判断依赖环境是否可用。依赖可用时不会重新安装 `.venv`。

这样即使开发者重新构建时忘记手工修改版本字符串，覆盖安装后也不会继续运行旧 `app.py`。

## 开发者打包

推荐只使用这个命令生成分发安装包：

```powershell
.\build_launcher.ps1
```

产物：

```text
dist\SRTMatcherSetup-<build-id>-<yyyyMMdd-HHmmss>.exe
```

`build_launcher.ps1` 会做这些事：

1. 确认项目内 `.venv` 存在；不存在则调用 `install.ps1`。
2. 使用 PyInstaller 把 `bootstrap.py` 打成一个小型 `SRTMatcher.exe` 启动器。
3. 把 `app.py`、`qt_app.py`、`requirements.txt`、`README.md` 作为数据文件塞进启动器。
4. 调用 NSIS 生成带构建标识和时间戳的新安装包。旧安装包不会被覆盖。
5. 打包前强制把 `installer.nsi` 保存为 UTF-8 BOM，避免中文安装界面乱码。
6. 清理临时 `dist\nsis_payload`。

仓库只保留这一套分发流程，避免维护多种互相冲突的打包方式。

## 本地开发运行

开发机首次准备：

```powershell
.\install.ps1
```

本地启动：

```powershell
.\run.ps1
```

依赖安装在项目内：

```text
.venv
```

不会写入系统 Python。开发运行和用户安装运行是两条路径：开发时用项目目录的 `.venv`，用户安装后用安装目录里的 `.venv`。

## 目录与配置

用户配置保存到：

```text
%APPDATA%\SRTMatcher\settings.json
```

软件也会尝试在安装目录旁边写一份便携副本：

```text
安装目录\app\settings.json
```

模型根目录默认是：

```text
安装目录\models
```

faster-whisper 模型实际目录是：

```text
安装目录\models\faster-whisper-large-v3-turbo
```

输出目录可在设置页固定，也可以每次任务单独指定 SRT 输出路径。

## 模型文件

当前使用：

```text
faster-whisper-large-v3-turbo
```

软件会检查这些文件：

```text
model.bin
config.json
tokenizer.json
preprocessor_config.json
vocabulary.json 或 vocabulary.txt
```

如果不完整，用户可以在设置页的“模型”区域点击：

```text
下载/补齐
```

模型下载通过 Hugging Face / faster-whisper 仓库补齐小文件或完整模型。安装包不会内置大模型。

## CUDA 与运行环境

软件内“设置 -> 运行环境”只保留两个核心动作：

- 检测能否运行
- 补齐缺失项

检测逻辑：

1. 调用 `nvidia-smi` 读取 NVIDIA 驱动和可支持 CUDA 版本。
2. 检查 CTranslate2/faster-whisper 是否能看到 CUDA。
3. 检查 PyTorch CUDA 是否可用，因为 WhisperX 和 pyannote 依赖 PyTorch。
4. PyTorch 必须执行一次真实的 CUDA `group_norm` kernel 并同步成功，不能只依赖 `torch.cuda.is_available()`。
5. 如果能直接运行，就不提示用户下载 CUDA/cuDNN。
6. 如果不能运行，再给出 NVIDIA 驱动、CUDA local 包、cuDNN 下载入口或安装 PyTorch CUDA 依赖。

PyTorch CUDA wheel 推荐规则：

```text
nvidia-smi CUDA >= 12.8 -> cu128
nvidia-smi CUDA >= 12.6 -> cu126
nvidia-smi CUDA >= 12.4 -> cu124
nvidia-smi CUDA >= 12.1 -> cu121
否则使用 CPU 或提示升级驱动/CUDA
```

注意：`nvidia-smi` 显示的是驱动支持的最高 CUDA Runtime 版本，不等于用户系统实际安装了完整 CUDA Toolkit。

## 内置 FFmpeg

安装包内置 `ffmpeg.exe`，覆盖安装或首次启动时会复制到：

```text
安装目录\app\ffmpeg.exe
```

WhisperX 精对齐和说话人识别加载音频前会优先把该目录加入当前进程 PATH，并在日志中显示“WhisperX 使用内置 FFmpeg”。用户不需要另外安装 FFmpeg，也不需要手动配置系统 PATH。若安装目录内的 FFmpeg 缺失，才会尝试使用系统 PATH 中的 `ffmpeg.exe`。

FFmpeg 是独立的第三方命令行程序，构建信息和许可证说明见安装目录中的 `FFMPEG-NOTICE.txt`。

## 使用模式

主界面有三种模式：

- 自动判断
- 文稿精对齐
- 纯音频识别（可导出 SRT 或 TXT）

此外还有独立的“批量处理”页，可选择一次处理整个媒体目录并输出 TXT 或精对齐 SRT。

### 工作台与性能预设

应用使用现代化桌面布局：左侧导航负责切换制作工作台、批量任务、系统设置和字幕编辑；右侧使用独立的页面标题、卡片式操作区和统一任务状态底栏。旧的顶部标签页布局已移除。

制作页提供三种性能预设：

- 快速：使用更小的 ASR beam，适合预览或对时效更敏感的任务。
- 推荐：保留升级前的 `beam_size=5`，作为默认质量。
- 高质量：增大 beam，会使用更多时间和计算资源。

任务运行时会显示“准备、ASR、精对齐、AI 校对、导出”阶段。取消是协作式的：当前模型或网络调用返回后停止，不会强制终止 GUI 线程。

连续任务会复用相同配置的 faster-whisper 模型、WhisperX 语言对齐模型和 pyannote 模型。需要腾出 GPU 内存时，可在设置页点击“释放模型/GPU 内存”。

批量页会异步扫描目录，用表格显示每个文件的待处理、运行、完成、跳过或失败状态，并可仅重试失败项。

### 文稿精对齐

用户提供音频/视频和文稿。流程是：

1. faster-whisper 转录音频并提取 word timestamps。
2. 代码将文稿 token 和 ASR token 做匹配。
3. 如果文稿有多行，则按文稿行生成字幕条目。
4. WhisperX 对每条字幕做二次精对齐。
5. AI 只修正文稿文字，不改时间轴。
6. 输出 SRT。

### 纯音频转 SRT

用户只提供音频/视频，不提供文稿。流程是：

1. faster-whisper 直接生成 ASR 字幕。
2. AI 修正 ASR 文本错误。
3. 可选 WhisperX/说话人识别。
4. 输出 SRT。

### ASR 尾部幻觉过滤

faster-whisper 偶尔会在视频结尾的静音、背景声或片尾画面上生成并不存在的字幕，例如 `Untertitelung des ZDF ...`。单文件和批量模式现在都会：

- 启用逐词时间戳及 `hallucination_silence_threshold=1.0`。
- 关闭 `condition_on_previous_text`，降低上一片段文本在静音区继续生成的概率。
- 在进入 LLM 前检查连续位于末尾的 ASR 片段。
- 移除已知片尾伪字幕，以及同时具有高静音概率和低识别置信度的尾部片段。
- 在日志中逐条记录被移除的文本和原因。

过滤只从文件最后一条向前处理连续的可疑片段；相同字样如果后面仍有正常语音，不会作为片尾幻觉删除。

### 批量多语言转 TXT / 精对齐 SRT

适合对大量未按语言分类的视频或音频进行统一处理。使用方法：

1. 先在“设置 -> AI 校对接口”填写 Base URL、API Key、模型名和系统提示词。
2. 打开“批量处理”页，在“输出类型”中选择 TXT 或 SRT，再选择媒体目录和输出目录。
3. 按需开启“递归扫描子目录”和“跳过已经存在的 TXT/SRT”。
4. TXT 模式点击“开始批量 ASR + LLM 纠错”；SRT 模式点击“开始批量 ASR + 精对齐 + AI 纠错”。

批量流程会：

- 支持常见音频和视频格式，并对每个文件单独自动检测语言，适合英语、法语、德语等文件混合存放。
- 整批任务只加载一次 faster-whisper 模型，避免 100 个视频重复加载模型。
- 每个文件先 ASR，再使用现有 OpenAI Chat Completions 兼容接口严格纠错；纠错提示词要求保持原语言，不翻译、不改写。
- TXT 模式默认按相对目录输出同名 TXT。例如 `课程\demo.mp4` 输出为 `课程\demo.txt`；文件使用 UTF-8 BOM 编码，每个 ASR/纠错片段占一行，不包含时间轴。
- SRT 模式在 ASR 后强制执行 WhisperX 精对齐，再由 AI 只修正字幕文本而不改变时间轴；不同语言的 WhisperX 对齐模型按语言加载并在整批任务中复用。
- SRT 模式默认按相对目录输出同名 SRT。例如 `课程\demo.mp4` 输出为 `课程\demo.srt`，序号和时间轴使用标准 SRT 格式。
- 如果同一目录存在同名不同格式媒体，为避免覆盖，会保留原媒体扩展名，例如 `demo.mp4.srt` 和 `demo.wav.srt`。
- 默认跳过已经存在的所选输出类型，便于中断后继续运行。
- 单个文件损坏、ASR、WhisperX 精对齐或 LLM 请求失败时继续处理后续文件。TXT 模式生成 `_batch_errors.txt`，SRT 模式生成 `_batch_srt_errors.txt`。

## AI 字幕修正

AI 接口为 OpenAI Chat Completions 兼容格式。

用户需要配置：

```text
Base URL
API Key
模型名
系统提示词
```

请求体默认只发送：

```json
{
  "model": "...",
  "messages": [...],
  "stream": false
}
```

这样做是为了兼容一些对 `temperature` 等参数类型检查较严格的兼容接口。

AI 修正规则：

- AI 只改字幕文本。
- 时间轴、序号、条目数量由程序保管。
- 每批默认最多发送 20 条，降低兼容接口触发输出长度限制的概率。
- 请求要求 AI 在最后返回批次完成标记；标记缺失会自动重试。
- 程序检查接口 `finish_reason`，遇到 `length` 等未完整结束状态会自动重试。
- 程序检查序号、条目数量，以及每条纠错结果相对原 ASR 文本的长度/词数；疑似只返回半句话或过度改写时会自动重试。
- 多次失败会拆成更小批次。
- 单条仍无法通过自检时中断该文件，避免保存半截或过度改写的结果；批量模式会继续处理后续文件并写入失败清单。

## WhisperX 精对齐

WhisperX 不是“自动理解全文稿”的魔法模型。它的 `align()` 依赖输入 segment 的粗时间窗口：

```python
{
    "start": ...,
    "end": ...,
    "text": ...
}
```

窗口给得太宽，句首短词容易被吸到上一句尾部；窗口给得太窄，真实发音可能落在窗口外。

当前项目推荐窗口：

```python
pre_pad = 0.20
post_pad = 0.50

transcript.append({
    "id": subtitle.index,
    "start": max(0.0, subtitle.start - pre_pad),
    "end": max(subtitle.end + post_pad, subtitle.start + 0.25),
    "text": subtitle.text,
})

result = whisperx.align(
    transcript,
    align_model,
    metadata,
    audio,
    device,
    interpolate_method="nearest",
    return_char_alignments=False,
)
```

经验值：

- `pre_pad=0.15~0.30s` 更适合字幕制作。
- `post_pad=0.50~1.00s` 可以略大。
- 不要默认前后各扩 `1.0s`，短词密集时会明显提前句首。
- 一定保留 `id`，不要靠文本内容回填字幕条目。
- WhisperX 失败时必须写日志，不能静默当作成功。
- 对齐完成后必须修复时间轴：非负、递增、不重叠、最小时长。

## 逐词时间戳导出

“制作 -> 运行 -> 额外导出逐词时间戳”提供三个互斥选项：

```text
不导出（默认）
JSON
SRT
```

选择 JSON 或 SRT 时会启用 WhisperX，并直接保留 `whisperx.align()` 返回的 `segments[].words`，不会新增 ASR 或分词逻辑。句级 SRT 仍按原文件名输出，逐词文件追加后缀：

```text
原句级文件：example.aligned.srt
逐词 JSON：example.aligned.words.json
逐词 SRT： example.aligned.words.srt
```

JSON 是逐词/逐字记录数组，字段包括 `sentence_id`、`word`、`start`、`end`，WhisperX 返回置信度时还包括 `score`。某条句级字幕完全没有获得 words 结果时，会写入：

```json
{"sentence_id": 1, "words": null}
```

逐词 SRT 将每个有完整时间戳的词或字写成一个连续编号的字幕块；没有 words 结果的句子会跳过，同时日志会明确记录对应句级字幕序号。

这里的“逐词”沿用 WhisperX 的实际输出粒度。中文在不同对齐模型或 tokenizer 下可能按单字输出；程序会如实导出，不会额外合并中文分词。导出前会把时间戳限制在所属句级字幕区间内，并修复为非负、递增、不重叠。

## 精对齐偏差排查

遇到“看起来对不准”时，先判断是文件问题还是剪辑软件缓存：

1. 直接用文本编辑器打开 SRT，检查时间轴是否递增。
2. 如果 SRT 文件正常，但剪辑软件里还是旧时间轴，优先怀疑同名 SRT 缓存。
3. 解决方法：改名重新导入，或先从剪辑软件素材库删除旧字幕素材。

如果确认 SRT 文件本身不准，再看这些点：

1. 日志里是否出现 `WhisperX 对齐完成，更新 X/Y 条时间轴`。
2. 是否实际运行了新安装目录里的 `app.py`，而不是旧安装目录。
3. 抽取问题句前后 1-2 条，测试 `pre_pad/post_pad`。
4. 打印 WhisperX 返回的 `words`，看句首词是否被拉到上一句尾部或静音段。
5. 人耳重点校验句首，字幕最敏感的是句首提前/滞后。

## 说话人识别

说话人识别使用：

```text
pyannote/speaker-diarization-community-1
```

它是本地推理，但模型下载需要 Hugging Face Access Token，并且对应账号必须在 Hugging Face 页面同意模型许可。

用户需要在设置页填写：

```text
HF Token
最小说话人数
最大说话人数
```

常见错误：

- `403 Forbidden / gated repo`：Token 所属账号没有同意模型许可，或 Token 没有 read 权限。
- `cannot find requested files in local cache`：网络无法访问 Hugging Face，或模型还没有成功下载到缓存。

说话人识别不是人声分离，不会导出某个人的单独音轨。它只给字幕段落标记说话人，并支持导出某个说话人的 SRT/TXT。

## 支持语言

ASR 语言由 faster-whisper 支持。GUI 提供常用语言下拉选择，包括：

```text
自动、中文、英语、德语、日语、韩语等
```

WhisperX 精对齐还要求对应语言有可用的 align model。当前代码会检查语言是否在 WhisperX 常见支持列表中，不支持时保留已有粗时间轴。

## Windows 路径注意事项

GUI 正常支持中文路径。

但开发者写临时测试脚本时，不要从非 UTF-8 PowerShell stdin 里硬编码中文路径，否则路径可能变成：

```text
??3d???.wav
```

更稳的做法是：

```python
from pathlib import Path

audio = list(Path(r"C:\Users\...\Music").glob("MiniMax_2026-07-06_11_52_31_*.wav"))[0]
```

## 项目内重要文件

```text
app.py               核心逻辑：ASR、文稿匹配、WhisperX、AI、说话人识别、模型/环境检测
qt_app.py            PySide6 GUI
task_runtime.py      任务取消、运行上下文和结构化进度事件
model_runtime.py     ASR、WhisperX、pyannote 模型复用与内存释放
ai_transport.py      AI HTTP 连接池和兼容接口传输
asr_runtime.py       ASR 性能预设
batch_runtime.py     批量任务选择与失败项重试过滤
bootstrap.py         安装后启动器：复制源码、准备 .venv、快速启动、覆盖升级版本标记
installer.nsi        NSIS 安装脚本
build_launcher.ps1   推荐打包脚本
requirements.txt     用户安装目录 .venv 的依赖清单
```

## 发布前检查清单

发布新安装包前建议执行：

```powershell
.\.venv\Scripts\python.exe -m py_compile .\bootstrap.py .\app.py .\qt_app.py
.\build_launcher.ps1
```

然后确认：

- `dist\SRTMatcherSetup-<build-id>-<timestamp>.exe` 已新建，原有安装包仍保留。
- NSIS 日志显示 `Processing script file: ".\installer.nsi" (UTF8)`。
- 覆盖安装后首次启动能刷新新版 `app.py/qt_app.py`。
- 二次启动不再进入环境安装流程。
- 设置页可以滚动。
- 同名 SRT 导入剪辑软件前，必要时改名避免缓存。
