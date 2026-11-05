from __future__ import annotations

import codecs
import json
import builtins as _builtins
import re
import sys
import threading
import time
import traceback
from pathlib import Path

_ORIGINAL_IMPORT = _builtins.__import__

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_builtins.__import__ = _ORIGINAL_IMPORT

import app as core
from task_runtime import CancellationToken, TaskCancelled, task_context


AUDIO_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".wma", ".mp4", ".mkv",
    ".mov", ".webm", ".avi", ".mpeg", ".mpg", ".flv",
}
APP_ICON_PATH = Path(__file__).resolve().parent / "srtmatcher-logo.png"


class JobWorker(QObject):
    log = Signal(str)
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal(str)
    task_event = Signal(dict)

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config = config
        self.cancel_token = CancellationToken()

    def cancel(self) -> None:
        self.cancel_token.cancel()

    def run(self) -> None:
        # Exactly one terminal signal is always emitted (in ``finally``) so the
        # QThread quits and the busy state resets even on BaseException.
        terminal: tuple[Signal, str] | None = None
        try:
            job_type = self.config.get("job_type")
            if job_type == "batch_txt":
                runner = core.run_batch_txt_job
            elif job_type == "batch_srt":
                runner = core.run_batch_srt_job
            else:
                runner = core.run_srt_job
            with task_context(self.cancel_token, self.task_event.emit):
                output = runner(
                    self.config, self.log.emit,
                    lambda value, message: self.progress.emit(int(value), message),
                )
            terminal = (self.finished, str(output))
        except TaskCancelled as exc:
            terminal = (self.cancelled, str(exc))
        except BaseException as exc:
            self.log.emit(traceback.format_exc())
            terminal = (self.failed, str(exc) or exc.__class__.__name__)
        finally:
            signal, value = terminal or (self.failed, "任务异常终止。")
            signal.emit(value)


class UiEvents(QObject):
    log = Signal(str)
    busy = Signal(bool)
    progress = Signal(int, str)
    model_path = Signal(str)
    error = Signal(str, str)
    runtime_text = Signal(str)
    runtime_links = Signal(str, bool)
    pyannote_text = Signal(str)
    batch_preview = Signal(int, object, str)
    runtime_downloads = Signal(object)
    background_finished = Signal(str)


WINDOWS_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_speaker_filename(speaker: str) -> str:
    """Keep non-ASCII speaker names; only replace characters Windows forbids."""
    safe = WINDOWS_ILLEGAL_FILENAME_CHARS.sub("_", speaker).strip().rstrip(". ")
    return safe or "speaker"


def decode_script_bytes(data: bytes) -> str | None:
    """Decode a script text file; return None when no supported encoding fits."""
    try:
        if data.startswith(codecs.BOM_UTF8):
            return data[len(codecs.BOM_UTF8):].decode("utf-8")
        if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
            return data.decode("utf-16")
    except UnicodeDecodeError:
        return None
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    return None


class DropTextEdit(QPlainTextEdit):
    fileDropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self.fileDropped.emit(path)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class SettingsScrollArea(QScrollArea):
    def __init__(self, content_height: int = 980) -> None:
        super().__init__()
        self.content_height = content_height
        self.content_widget: QWidget | None = None
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def setContentWidget(self, widget: QWidget) -> None:
        self.content_widget = widget
        widget.setMinimumHeight(self.content_height)
        self.setWidget(widget)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.content_widget is not None:
            width = max(720, self.viewport().width())
            self.content_widget.setMinimumWidth(width)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config_data = core.load_settings()
        self.worker_thread: QThread | None = None
        self.worker: JobWorker | None = None
        self.output_path_custom = bool(self.config_data.get("output_path_custom", False))
        self.ui_events = UiEvents()
        self.last_srt_path = ""
        self.cuda_links: dict[str, str] = {}
        self.preview_generation = 0
        self.batch_row_by_path: dict[str, int] = {}
        self.failed_batch_paths: set[str] = set()
        self.completed_batch_paths: set[str] = set()
        self.skipped_batch_paths: set[str] = set()
        self.close_when_idle = False
        self.job_started_at = 0.0
        self.background_tasks: set[str] = set()
        self.batch_job_running = False
        self.batch_subset_run = False
        self.output_dir_committed = ""
        self.batch_input_dir_committed = ""

        self.setWindowTitle(f"{core.DISPLAY_NAME}（SRTMatcher）")
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.setMinimumSize(1160, 700)
        self.setAcceptDrops(True)

        self.build_ui()
        self.ui_events.log.connect(self.log)
        self.ui_events.busy.connect(lambda busy: self.set_busy(busy, cancellable=False))
        self.ui_events.progress.connect(self.set_progress)
        self.ui_events.model_path.connect(self.apply_model_path)
        self.ui_events.error.connect(lambda title, message: QMessageBox.critical(self, title, message))
        self.ui_events.runtime_text.connect(self.set_runtime_text)
        self.ui_events.runtime_links.connect(self.set_runtime_links)
        self.ui_events.pyannote_text.connect(self.set_pyannote_text)
        self.ui_events.batch_preview.connect(self.apply_batch_preview)
        self.ui_events.runtime_downloads.connect(self.set_runtime_downloads)
        self.ui_events.background_finished.connect(self.on_background_task_finished)
        self.load_config_to_ui()
        self.update_mode_state()
        self.update_output_path()
        self.refresh_model_status()
        self.refresh_pyannote_status()
        self.log(f"配置文件: {core.SETTINGS_PATH}")

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(216)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(20, 24, 20, 20)
        sidebar_layout.setSpacing(8)

        brand_row = QHBoxLayout()
        brand_mark = QLabel()
        brand_mark.setObjectName("BrandMark")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setFixedSize(42, 42)
        brand_pixmap = QPixmap(str(APP_ICON_PATH))
        if brand_pixmap.isNull():
            brand_mark.setText("SM")
        else:
            brand_mark.setPixmap(
                brand_pixmap.scaled(
                    38,
                    38,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_text = QVBoxLayout()
        brand_title = QLabel("SRTMatcher")
        brand_title.setObjectName("BrandTitle")
        brand_subtitle = QLabel("字幕智能工作台")
        brand_subtitle.setObjectName("SidebarMuted")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_subtitle)
        brand_row.addWidget(brand_mark)
        brand_row.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand_row)
        sidebar_layout.addSpacing(26)

        nav_caption = QLabel("工作区")
        nav_caption.setObjectName("NavCaption")
        sidebar_layout.addWidget(nav_caption)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (number, text) in enumerate(
            [("01", "制作工作台"), ("02", "批量任务"), ("03", "系统设置"), ("04", "字幕编辑")]
        ):
            button = QPushButton(f"{number}   {text}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setMinimumHeight(46)
            button.clicked.connect(lambda _checked=False, page=index: self.set_current_page(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            sidebar_layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        sidebar_layout.addStretch(1)

        local_card = QFrame()
        local_card.setObjectName("LocalCard")
        local_layout = QVBoxLayout(local_card)
        local_layout.setContentsMargins(14, 12, 14, 12)
        local_title = QLabel("本地运行")
        local_title.setObjectName("LocalTitle")
        local_detail = QLabel("模型缓存 · GPU 加速\n数据保留在当前设备")
        local_detail.setObjectName("SidebarMuted")
        local_detail.setWordWrap(True)
        local_layout.addWidget(local_title)
        local_layout.addWidget(local_detail)
        sidebar_layout.addWidget(local_card)
        build_label = QLabel("WORKBENCH 2026.08")
        build_label.setObjectName("BuildLabel")
        sidebar_layout.addWidget(build_label)
        shell.addWidget(sidebar)

        main_surface = QFrame()
        main_surface.setObjectName("MainSurface")
        main_layout = QVBoxLayout(main_surface)
        main_layout.setContentsMargins(24, 20, 24, 18)
        main_layout.setSpacing(16)

        topbar = QHBoxLayout()
        page_heading = QVBoxLayout()
        self.page_title = QLabel("制作工作台")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("单文件识别、文稿精对齐与智能校对")
        self.page_subtitle.setObjectName("PageSubtitle")
        page_heading.addWidget(self.page_title)
        page_heading.addWidget(self.page_subtitle)
        topbar.addLayout(page_heading, 1)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Status")
        topbar.addWidget(self.status_label)
        main_layout.addLayout(topbar)

        self.tabs = QStackedWidget()
        self.tabs.setObjectName("PageStack")
        self.tabs.addWidget(self.build_work_tab())
        self.tabs.addWidget(self.build_batch_tab())
        self.tabs.addWidget(self.build_settings_tab())
        self.tabs.addWidget(self.build_speaker_tab())
        self.tabs.currentChanged.connect(self.on_page_changed)
        main_layout.addWidget(self.tabs, 1)

        self.task_dock = QFrame()
        self.task_dock.setObjectName("TaskDock")
        dock_layout = QVBoxLayout(self.task_dock)
        dock_layout.setContentsMargins(12, 10, 12, 10)
        dock_layout.setSpacing(8)
        self.stage_bar = QFrame()
        self.stage_bar.setObjectName("StageBar")
        stage_layout = QHBoxLayout(self.stage_bar)
        stage_layout.setContentsMargins(10, 6, 10, 6)
        stage_layout.setSpacing(6)
        self.stage_labels: list[QLabel] = []
        for text in ["准备", "ASR", "精对齐", "AI 校对", "导出"]:
            label = QLabel(text)
            label.setObjectName("TaskStage")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("stageState", "pending")
            stage_layout.addWidget(label, 1)
            self.stage_labels.append(label)
        dock_layout.addWidget(self.stage_bar)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        progress_row.addWidget(self.progress, 1)
        self.cancel_btn = QPushButton("取消任务")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.clicked.connect(self.cancel_current_job)
        progress_row.addWidget(self.cancel_btn)
        dock_layout.addLayout(progress_row)
        self.task_dock.hide()
        main_layout.addWidget(self.task_dock)
        shell.addWidget(main_surface, 1)

    def set_current_page(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)

    def on_page_changed(self, index: int) -> None:
        pages = [
            ("制作工作台", "单文件识别、文稿精对齐与智能校对"),
            ("批量任务", "扫描整个目录，跟踪每个文件的处理状态"),
            ("系统设置", "管理模型、GPU、AI 接口和说话人能力"),
            ("字幕编辑", "检查说话人标签并导出指定角色的字幕"),
        ]
        title, subtitle = pages[index] if 0 <= index < len(pages) else pages[0]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)

    def mode_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("SegmentedButton")
        btn.setCheckable(True)
        btn.setMinimumHeight(38)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return btn

    def metric_card(self, title: str, tone: str = "neutral") -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("MetricCard")
        card.setProperty("tone", tone)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        value = QLabel("0")
        value.setObjectName("MetricValue")
        label = QLabel(title)
        label.setObjectName("MetricLabel")
        layout.addWidget(value)
        layout.addWidget(label)
        return card, value

    def build_work_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("WorkspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("HeroCard")
        hero.setFixedHeight(108)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(20, 14, 20, 14)
        hero_copy = QVBoxLayout()
        eyebrow = QLabel("AI SUBTITLE WORKBENCH")
        eyebrow.setObjectName("Eyebrow")
        hero_title = QLabel("把素材变成可直接使用的字幕")
        hero_title.setObjectName("HeroTitle")
        hero_subtitle = QLabel("拖入音视频，选择精度，剩下的交给本地 AI 工作流。")
        hero_subtitle.setObjectName("HeroSubtitle")
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(hero_title)
        hero_copy.addWidget(hero_subtitle)
        hero_layout.addLayout(hero_copy, 1)
        hero_badge = QLabel("本地模型  ·  GPU 加速")
        hero_badge.setObjectName("HeroBadge")
        hero_layout.addWidget(hero_badge)
        layout.addWidget(hero)

        mode_bar = QFrame()
        mode_bar.setObjectName("ModeBar")
        mode_bar.setFixedHeight(56)
        mode_layout = QHBoxLayout(mode_bar)
        mode_layout.setContentsMargins(8, 8, 12, 8)
        mode_layout.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.auto_mode_btn = self.mode_button("自动判断")
        self.align_mode_btn = self.mode_button("文稿精对齐")
        self.transcribe_mode_btn = self.mode_button("纯音频识别")
        for idx, btn in enumerate([self.auto_mode_btn, self.align_mode_btn, self.transcribe_mode_btn]):
            self.mode_group.addButton(btn, idx)
            mode_layout.addWidget(btn)
        self.auto_mode_btn.setChecked(True)
        self.mode_group.idClicked.connect(lambda _id: self.on_mode_changed())
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("ModeHint")
        mode_layout.addWidget(self.mode_hint, 1)
        layout.addWidget(mode_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("WorkspaceSplitter")
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        left_column = QWidget()
        left_column.setObjectName("WorkspaceOptionsColumn")
        left_column.setMinimumWidth(430)
        left_column_layout = QVBoxLayout(left_column)
        left_column_layout.setContentsMargins(0, 0, 8, 0)
        left_column_layout.setSpacing(10)

        left_scroll = QScrollArea()
        left_scroll.setObjectName("WorkspaceOptionsScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setMinimumWidth(420)

        left = QWidget()
        left.setObjectName("WorkspaceOptions")
        left.setMinimumWidth(396)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(10)

        files = QGroupBox("01  素材与输出")
        file_grid = QGridLayout(files)
        self.audio_path = QLineEdit()
        self.text_path = QLineEdit()
        self.output_dir = QLineEdit()
        self.output_path = QLineEdit()
        self.model_path = QLineEdit()
        self.model_path.setReadOnly(True)
        self.output_format = QComboBox()
        self.output_format.addItem("SRT 字幕", "srt")
        self.output_format.addItem("TXT 文本", "txt")
        self.output_format.currentIndexChanged.connect(self.on_output_format_changed)
        self.output_dir.editingFinished.connect(self.on_output_dir_edited)
        self.output_path.textEdited.connect(self.on_output_path_edited)
        self.audio_path.textEdited.connect(self.on_audio_path_edited)
        self.add_path_row(file_grid, 0, "音频/视频", self.audio_path, self.pick_audio)
        self.add_path_row(file_grid, 1, "文稿 TXT", self.text_path, self.pick_text)
        file_grid.addWidget(QLabel("导出格式"), 2, 0)
        file_grid.addWidget(self.output_format, 2, 1, 1, 2)
        self.add_path_row(file_grid, 3, "本次保存为", self.output_path, self.pick_output_file, "另存")
        file_grid.setColumnStretch(1, 1)
        left_layout.addWidget(files)

        options = QGroupBox("02  处理方案")
        opt_grid = QGridLayout(options)
        self.language = QComboBox()
        for label, code in core.LANGUAGE_CHOICES:
            self.language.addItem(label, code)
        self.device = QComboBox()
        self.device.addItems(["cuda", "cpu"])
        self.compute_type = QComboBox()
        self.compute_type.addItems(["float16", "int8_float16", "int8"])
        self.performance_preset = QComboBox()
        self.performance_preset.addItem("快速（最低延迟）", "fast")
        self.performance_preset.addItem("推荐（保持当前质量）", "recommended")
        self.performance_preset.addItem("高质量（更慢）", "quality")
        for combo in (self.language, self.performance_preset):
            combo.setMinimumHeight(36)
        opt_grid.addWidget(QLabel("语言"), 0, 0)
        opt_grid.addWidget(self.language, 0, 1)
        opt_grid.addWidget(QLabel("性能预设"), 1, 0)
        opt_grid.addWidget(self.performance_preset, 1, 1)
        self.advanced_runtime = QFrame()
        advanced_grid = QGridLayout(self.advanced_runtime)
        advanced_grid.setContentsMargins(0, 0, 0, 0)
        advanced_grid.addWidget(QLabel("设备"), 0, 0)
        advanced_grid.addWidget(self.device, 0, 1)
        advanced_grid.addWidget(QLabel("精度"), 1, 0)
        advanced_grid.addWidget(self.compute_type, 1, 1)
        self.advanced_runtime.hide()
        self.ai_enabled = QCheckBox("启用 AI 校对/修正")
        self.whisperx_enabled = QCheckBox("WhisperX 精对齐")
        self.diarization_enabled = QCheckBox("说话人识别")
        self.word_timestamp_export = QComboBox()
        self.word_timestamp_export.addItem("不导出", "none")
        self.word_timestamp_export.addItem("JSON", "json")
        self.word_timestamp_export.addItem("SRT", "srt")
        self.word_timestamp_export.setMinimumHeight(36)
        self.word_timestamp_export.currentIndexChanged.connect(self.on_word_export_changed)
        opt_grid.addWidget(self.ai_enabled, 2, 0, 1, 2)
        opt_grid.addWidget(self.whisperx_enabled, 3, 0, 1, 2)
        opt_grid.addWidget(self.diarization_enabled, 4, 0, 1, 2)
        word_export_label = QLabel("额外导出逐词时间戳")
        word_export_label.setMinimumWidth(150)
        opt_grid.addWidget(word_export_label, 5, 0)
        opt_grid.addWidget(self.word_timestamp_export, 5, 1)
        runtime_settings_btn = QPushButton("设备、精度与模型设置")
        runtime_settings_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        opt_grid.addWidget(runtime_settings_btn, 6, 0, 1, 2)
        opt_grid.setColumnMinimumWidth(0, 150)
        opt_grid.setColumnStretch(1, 1)
        left_layout.addWidget(options)

        actions = QHBoxLayout()
        self.start_btn = QPushButton("开始")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setMinimumHeight(48)
        self.start_btn.clicked.connect(self.start_job)
        actions.addWidget(self.start_btn, 1)
        left_column_layout.addWidget(left_scroll, 1)
        left_column_layout.addLayout(actions)
        left_layout.addStretch(1)
        left_scroll.setWidget(left)

        right = QWidget()
        right.setObjectName("EditorPanel")
        right.setMinimumWidth(520)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.script_label = QLabel("文稿")
        self.script_label.setObjectName("Section")
        self.script_text = DropTextEdit()
        self.script_text.setPlaceholderText("文稿精对齐模式：粘贴文稿，或拖入 TXT。\n纯音频模式：这里可以留空。")
        self.script_text.fileDropped.connect(self.handle_drop_path)
        self.script_text.textChanged.connect(self.on_script_changed)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(170)
        self.log_text.document().setMaximumBlockCount(4000)
        right_layout.addWidget(self.script_label)
        right_layout.addWidget(self.script_text, 3)
        log_label = QLabel("日志")
        log_label.setObjectName("Section")
        right_layout.addWidget(log_label)
        right_layout.addWidget(self.log_text, 2)

        splitter.addWidget(left_column)
        splitter.addWidget(right)
        splitter.setSizes([430, 740])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        return page

    def build_batch_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("WorkspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.batch_intro = QLabel(
            "扫描目录中的音频/视频，逐个自动检测语言，先 ASR，再使用设置页中的 LLM 纠错，"
            "最后按原文件名输出 TXT。模型在整批任务中只加载一次。"
        )
        self.batch_intro.setObjectName("Muted")
        self.batch_intro.setWordWrap(True)
        layout.addWidget(self.batch_intro)

        metrics = QHBoxLayout()
        total_card, self.batch_total_metric = self.metric_card("已扫描文件", "accent")
        completed_card, self.batch_completed_metric = self.metric_card("已完成", "success")
        failed_card, self.batch_failed_metric = self.metric_card("失败待重试", "danger")
        metrics.addWidget(total_card)
        metrics.addWidget(completed_card)
        metrics.addWidget(failed_card)
        layout.addLayout(metrics)

        paths = QGroupBox("01  选择任务目录")
        path_grid = QGridLayout(paths)
        self.batch_input_dir = QLineEdit()
        self.batch_output_dir = QLineEdit()
        self.batch_input_btn = self.add_path_row(
            path_grid, 0, "媒体目录", self.batch_input_dir, self.pick_batch_input_dir
        )
        self.batch_input_dir.editingFinished.connect(self.on_batch_input_dir_edited)
        self.batch_output_label = QLabel("TXT 输出目录")
        self.batch_output_btn = QPushButton("选择...")
        self.batch_output_btn.clicked.connect(self.pick_batch_output_dir)
        path_grid.addWidget(self.batch_output_label, 1, 0)
        path_grid.addWidget(self.batch_output_dir, 1, 1)
        path_grid.addWidget(self.batch_output_btn, 1, 2)
        layout.addWidget(paths)

        options = QGroupBox("02  批量处理策略")
        options_layout = QHBoxLayout(options)
        options_layout.addWidget(QLabel("输出类型"))
        self.batch_output_format = QComboBox()
        self.batch_output_format.addItem("TXT（ASR + AI 纠错）", "txt")
        self.batch_output_format.addItem("SRT（ASR + WhisperX 精对齐 + AI 纠错）", "srt")
        self.batch_output_format.currentIndexChanged.connect(self.on_batch_format_changed)
        options_layout.addWidget(self.batch_output_format)
        self.batch_recursive = QCheckBox("递归扫描子目录")
        self.batch_skip_existing = QCheckBox("跳过已经存在的 TXT")
        self.batch_recursive.stateChanged.connect(lambda _state: self.refresh_batch_preview())
        options_layout.addWidget(self.batch_recursive)
        options_layout.addWidget(self.batch_skip_existing)
        options_layout.addStretch(1)
        options_layout.addWidget(QLabel("语言：逐文件自动检测"))
        layout.addWidget(options)

        action_row = QHBoxLayout()
        self.batch_preview_label = QLabel("尚未选择媒体目录")
        self.batch_preview_label.setObjectName("Section")
        self.batch_refresh_btn = QPushButton("扫描文件")
        self.batch_refresh_btn.clicked.connect(self.refresh_batch_preview)
        self.batch_settings_btn = QPushButton("打开 AI 设置")
        self.batch_settings_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        self.batch_open_output_btn = QPushButton("打开输出目录")
        self.batch_open_output_btn.clicked.connect(self.open_batch_output_dir)
        self.batch_retry_btn = QPushButton("重试失败项")
        self.batch_retry_btn.setEnabled(False)
        self.batch_retry_btn.clicked.connect(self.retry_failed_batch_items)
        self.batch_start_btn = QPushButton("开始批量 ASR + LLM 纠错")
        self.batch_start_btn.setObjectName("Primary")
        self.batch_start_btn.clicked.connect(self.start_batch_job)
        action_row.addWidget(self.batch_preview_label, 1)
        action_row.addWidget(self.batch_refresh_btn)
        action_row.addWidget(self.batch_settings_btn)
        action_row.addWidget(self.batch_open_output_btn)
        action_row.addWidget(self.batch_retry_btn)
        action_row.addWidget(self.batch_start_btn)
        layout.addLayout(action_row)

        batch_splitter = QSplitter(Qt.Orientation.Vertical)
        batch_splitter.setChildrenCollapsible(False)
        self.batch_table = QTableWidget(0, 3)
        self.batch_table.setHorizontalHeaderLabels(["文件", "状态", "输出"])
        self.batch_table.setAlternatingRowColors(True)
        self.batch_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.batch_table.horizontalHeader().setStretchLastSection(True)
        batch_splitter.addWidget(self.batch_table)
        self.batch_log_text = QTextEdit()
        self.batch_log_text.setReadOnly(True)
        self.batch_log_text.document().setMaximumBlockCount(8000)
        batch_splitter.addWidget(self.batch_log_text)
        batch_splitter.setSizes([420, 180])
        layout.addWidget(batch_splitter, 1)
        self.on_batch_format_changed()
        return page

    def build_settings_tab(self) -> QWidget:
        scroll = SettingsScrollArea(1110)
        scroll.setObjectName("SettingsScroll")
        page = QWidget()
        page.setObjectName("WorkspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        top = QGroupBox("01  常用设置")
        top.setMinimumHeight(220)
        top_grid = QGridLayout(top)
        top_grid.setColumnStretch(1, 1)
        self.output_dir_pick_btn = self.add_settings_path_row(
            top_grid, 0, "默认输出目录", self.output_dir, self.pick_output_dir, self.open_output_dir
        )
        self.add_settings_path_row(top_grid, 1, "模型目录", self.model_path, self.open_model_dir, None, browse_text="打开")
        top_grid.addWidget(QLabel("设备"), 2, 0)
        top_grid.addWidget(self.device, 2, 1)
        top_grid.addWidget(QLabel("精度"), 3, 0)
        top_grid.addWidget(self.compute_type, 3, 1)
        self.save_settings_btn = QPushButton("保存设置")
        self.save_settings_btn.clicked.connect(self.save_config)
        top_grid.addWidget(self.save_settings_btn, 3, 2)
        self.onset_lead = QDoubleSpinBox()
        self.onset_lead.setRange(0.0, 1.0)
        self.onset_lead.setSingleStep(0.05)
        self.onset_lead.setDecimals(2)
        self.onset_lead.setSuffix(" 秒")
        self.onset_lead.setValue(core.onset_lead_from_config(self.config_data))
        self.onset_lead.setToolTip(
            "SRT 每条字幕的起点提前量（不会与上一条重叠）。\n"
            "按字幕切音频训练时可避免丢掉句首音节；设为 0 则关闭。"
        )
        top_grid.addWidget(QLabel("句首余量"), 4, 0)
        top_grid.addWidget(self.onset_lead, 4, 1)
        self.output_to_source = QCheckBox("输出到上传文件所在文件夹（取消勾选则使用上面的默认输出目录）")
        self.output_to_source.toggled.connect(self.on_output_location_changed)
        top_grid.addWidget(QLabel("输出位置"), 5, 0)
        top_grid.addWidget(self.output_to_source, 5, 1, 1, 3)
        layout.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(10)

        model_box = QGroupBox("02  ASR 模型")
        model_box.setMinimumHeight(260)
        model_layout = QVBoxLayout(model_box)
        model_layout.setSpacing(8)
        self.model_status_label = QLabel("")
        self.model_status_label.setObjectName("Section")
        self.model_status_progress = QProgressBar()
        self.model_status_progress.setRange(0, 100)
        self.model_files_grid = QGridLayout()
        self.model_files_grid.setColumnStretch(2, 1)
        self.model_file_rows: list[tuple[QLabel, QLabel, QLabel]] = []
        for row in range(5):
            name_label = QLabel("")
            state_label = QLabel("")
            found_label = QLabel("")
            found_label.setObjectName("Muted")
            self.model_files_grid.addWidget(name_label, row, 0)
            self.model_files_grid.addWidget(state_label, row, 1)
            self.model_files_grid.addWidget(found_label, row, 2)
            self.model_file_rows.append((name_label, state_label, found_label))
        model_buttons = QHBoxLayout()
        self.refresh_model_btn = QPushButton("刷新")
        self.refresh_model_btn.clicked.connect(self.refresh_model_status)
        self.download_model_btn = QPushButton("下载/补齐")
        self.download_model_btn.clicked.connect(self.check_model_files)
        self.open_model_btn = QPushButton("打开目录")
        self.open_model_btn.clicked.connect(self.open_model_dir)
        self.release_models_btn = QPushButton("释放模型/GPU 内存")
        self.release_models_btn.clicked.connect(self.release_model_memory)
        model_buttons.addWidget(self.refresh_model_btn)
        model_buttons.addWidget(self.download_model_btn)
        model_buttons.addWidget(self.open_model_btn)
        model_buttons.addWidget(self.release_models_btn)
        model_buttons.addStretch(1)
        model_layout.addWidget(self.model_status_label)
        model_layout.addWidget(self.model_status_progress)
        model_layout.addLayout(self.model_files_grid)
        model_layout.addLayout(model_buttons)
        body.addWidget(model_box, 1)

        runtime_box = QGroupBox("03  GPU 与运行环境")
        runtime_box.setMinimumHeight(260)
        runtime_layout = QVBoxLayout(runtime_box)
        runtime_layout.setSpacing(8)
        self.runtime_status = QLabel("点击“检测”后显示 GPU、CUDA、PyTorch 状态。")
        self.runtime_status.setObjectName("Muted")
        self.runtime_status.setWordWrap(True)
        runtime_buttons = QHBoxLayout()
        self.runtime_check_btn = QPushButton("检测能否运行")
        self.runtime_check_btn.clicked.connect(self.check_runtime)
        self.gpu_btn = self.runtime_check_btn
        self.repair_cuda_btn = QPushButton("补齐缺失项")
        self.repair_cuda_btn.clicked.connect(self.complete_runtime_missing)
        self.runtime_links = QLabel("")
        self.runtime_links.setObjectName("Muted")
        self.runtime_links.setWordWrap(True)
        self.runtime_links.setOpenExternalLinks(True)
        self.runtime_links.hide()
        self.model_check_btn = self.download_model_btn
        runtime_buttons.addWidget(self.runtime_check_btn)
        runtime_buttons.addWidget(self.repair_cuda_btn)
        runtime_buttons.addStretch(1)
        runtime_layout.addWidget(self.runtime_status)
        runtime_layout.addLayout(runtime_buttons)
        runtime_layout.addWidget(self.runtime_links)
        runtime_layout.addStretch(1)
        body.addWidget(runtime_box, 1)

        layout.addLayout(body)

        lower = QHBoxLayout()
        lower.setSpacing(10)

        ai_box = QGroupBox("04  AI 校对接口")
        ai_box.setMinimumHeight(440)
        ai_layout = QVBoxLayout(ai_box)
        ai_grid = QGridLayout()
        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_model = QLineEdit()
        self.add_form_row(ai_grid, 0, "Base URL", self.base_url)
        self.add_form_row(ai_grid, 1, "API Key", self.api_key)
        self.add_form_row(ai_grid, 2, "模型名", self.ai_model)
        ai_layout.addLayout(ai_grid)
        self.prompt_text = QPlainTextEdit()
        self.prompt_text.setMinimumHeight(110)
        self.prompt_text.setPlainText(core.resolve_system_prompt(self.config_data.get("system_prompt")))
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(QLabel("系统提示词"))
        prompt_header.addStretch(1)
        self.reset_prompt_btn = QPushButton("恢复默认提示词")
        self.reset_prompt_btn.clicked.connect(
            lambda: self.prompt_text.setPlainText(core.DEFAULT_SYSTEM_PROMPT)
        )
        prompt_header.addWidget(self.reset_prompt_btn)
        ai_layout.addLayout(prompt_header)
        ai_layout.addWidget(self.prompt_text)
        self.glossary_text = QPlainTextEdit()
        self.glossary_text.setMinimumHeight(80)
        self.glossary_text.setPlaceholderText(
            "每行一个专有名词/术语，AI 会优先按这里的写法纠错，例如：\n"
            "Bundestag\n"
            "英伟达 | 英伟大, 应为达"
        )
        self.glossary_text.setPlainText(str(self.config_data.get("ai_glossary", "")))
        ai_layout.addWidget(QLabel("热词 / 术语表（可选，格式：正确写法 | 常见误识别）"))
        ai_layout.addWidget(self.glossary_text)
        lower.addWidget(ai_box, 1)

        speaker_box = QGroupBox("05  说话人模型")
        speaker_box.setMinimumHeight(310)
        speaker_layout = QVBoxLayout(speaker_box)
        speaker_grid = QGridLayout()
        self.hf_token = QLineEdit()
        self.hf_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.min_speakers = QLineEdit()
        self.max_speakers = QLineEdit()
        self.add_form_row(speaker_grid, 0, "HF Token", self.hf_token)
        self.add_form_row(speaker_grid, 1, "最小说话人数", self.min_speakers)
        self.add_form_row(speaker_grid, 2, "最大说话人数", self.max_speakers)
        speaker_layout.addLayout(speaker_grid)
        self.pyannote_status = QLabel("")
        self.pyannote_status.setObjectName("Muted")
        self.pyannote_status.setWordWrap(True)
        speaker_buttons = QHBoxLayout()
        self.check_pyannote_btn = QPushButton("检查")
        self.check_pyannote_btn.clicked.connect(self.refresh_pyannote_status)
        self.download_pyannote_btn = QPushButton("下载/补齐")
        self.download_pyannote_btn.clicked.connect(self.check_pyannote_model)
        self.open_pyannote_btn = QPushButton("授权页面")
        self.open_pyannote_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://huggingface.co/pyannote/speaker-diarization-community-1")))
        speaker_buttons.addWidget(self.check_pyannote_btn)
        speaker_buttons.addWidget(self.download_pyannote_btn)
        speaker_buttons.addWidget(self.open_pyannote_btn)
        speaker_buttons.addStretch(1)
        speaker_layout.addWidget(self.pyannote_status)
        speaker_layout.addLayout(speaker_buttons)
        lower.addWidget(speaker_box, 1)

        layout.addLayout(lower)
        layout.addStretch(1)
        scroll.setContentWidget(page)
        return scroll

    def build_speaker_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("WorkspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        editor_intro = QFrame()
        editor_intro.setObjectName("HeroCard")
        intro_layout = QVBoxLayout(editor_intro)
        intro_layout.setContentsMargins(20, 16, 20, 16)
        intro_title = QLabel("说话人字幕工作区")
        intro_title.setObjectName("HeroTitle")
        intro_text = QLabel("检查标签、修改文本，或按角色单独导出 SRT / TXT。")
        intro_text.setObjectName("HeroSubtitle")
        intro_layout.addWidget(intro_title)
        intro_layout.addWidget(intro_text)
        layout.addWidget(editor_intro)

        toolbar_card = QFrame()
        toolbar_card.setObjectName("ToolbarCard")
        toolbar_box = QVBoxLayout(toolbar_card)
        toolbar_box.setContentsMargins(14, 12, 14, 12)
        toolbar = QHBoxLayout()
        self.speaker_file_label = QLabel("未加载 SRT")
        self.speaker_file_label.setObjectName("Section")
        self.speaker_combo = QComboBox()
        self.reload_speaker_btn = QPushButton("从当前 SRT 加载")
        self.reload_speaker_btn.clicked.connect(self.load_current_srt_to_speaker_table)
        self.save_speaker_btn = QPushButton("保存编辑后的 SRT")
        self.save_speaker_btn.clicked.connect(self.save_edited_srt)
        self.export_speaker_srt_btn = QPushButton("导出该说话人 SRT")
        self.export_speaker_srt_btn.clicked.connect(lambda: self.export_selected_speaker("srt"))
        self.export_speaker_txt_btn = QPushButton("导出该说话人 TXT")
        self.export_speaker_txt_btn.clicked.connect(lambda: self.export_selected_speaker("txt"))
        toolbar.addWidget(self.speaker_file_label, 1)
        toolbar.addWidget(QLabel("说话人"))
        toolbar.addWidget(self.speaker_combo)
        toolbar.addWidget(self.reload_speaker_btn)
        toolbar_box.addLayout(toolbar)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.save_speaker_btn)
        actions.addWidget(self.export_speaker_srt_btn)
        actions.addWidget(self.export_speaker_txt_btn)
        toolbar_box.addLayout(actions)
        layout.addWidget(toolbar_card)
        self.speaker_table = QTableWidget(0, 5)
        self.speaker_table.setHorizontalHeaderLabels(["序号", "开始", "结束", "说话人", "文本"])
        self.speaker_table.verticalHeader().setVisible(False)
        self.speaker_table.itemChanged.connect(self.on_speaker_item_changed)
        layout.addWidget(self.speaker_table, 1)
        return page

    def add_path_row(self, grid: QGridLayout, row: int, label: str, edit: QLineEdit, picker, button_text: str = "选择") -> QPushButton:
        button = QPushButton(button_text)
        button.clicked.connect(picker)
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        grid.addWidget(button, row, 2)
        return button

    def add_form_row(self, grid: QGridLayout, row: int, label: str, edit: QLineEdit) -> None:
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)

    def add_settings_path_row(
        self,
        grid: QGridLayout,
        row: int,
        label: str,
        edit: QLineEdit,
        picker,
        opener,
        browse_text: str = "选择",
        open_text: str = "打开",
    ) -> QPushButton:
        pick_btn = QPushButton(browse_text)
        pick_btn.clicked.connect(picker)
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        grid.addWidget(pick_btn, row, 2)
        if opener is not None:
            open_btn = QPushButton(open_text)
            open_btn.clicked.connect(opener)
            grid.addWidget(open_btn, row, 3)
        return pick_btn

    def load_config_to_ui(self) -> None:
        self.output_dir.setText(self.config_data.get("output_dir", str(core.INSTALL_ROOT / "outputs")))
        self.output_dir_committed = self.output_dir.text().strip()
        self.output_to_source.blockSignals(True)
        self.output_to_source.setChecked(self.config_data.get("output_location", "source") != "fixed")
        self.output_to_source.blockSignals(False)
        self.sync_output_location_widgets()
        self.set_combo_data(self.output_format, self.config_data.get("output_format", "srt"))
        self.output_path.setText(self.config_data.get("output_path", ""))
        self.model_path.setText(str(core.default_model_root_dir()))
        self.set_combo_data(self.language, self.config_data.get("language", "auto"))
        self.set_combo_text(self.device, self.config_data.get("device", "cuda"))
        self.set_combo_text(self.compute_type, self.config_data.get("compute_type", "float16"))
        self.set_combo_data(
            self.performance_preset,
            self.config_data.get("performance_preset", "recommended"),
        )
        self.ai_enabled.setChecked(bool(self.config_data.get("ai_enabled", True)))
        self.whisperx_enabled.setChecked(bool(self.config_data.get("whisperx_enabled", True)))
        self.set_combo_data(self.word_timestamp_export, self.config_data.get("word_timestamp_export", "none"))
        self.diarization_enabled.setChecked(bool(self.config_data.get("diarization_enabled", False)))
        self.base_url.setText(self.config_data.get("base_url", "https://api.openai.com/v1"))
        self.api_key.setText(self.config_data.get("api_key", ""))
        self.ai_model.setText(self.config_data.get("ai_model", core.DEFAULT_AI_MODEL))
        self.hf_token.setText(self.config_data.get("hf_token", ""))
        self.min_speakers.setText(str(self.config_data.get("min_speakers", "")))
        self.max_speakers.setText(str(self.config_data.get("max_speakers", "")))
        mode = self.config_data.get("mode", "auto")
        {"auto": self.auto_mode_btn, "align": self.align_mode_btn, "transcribe": self.transcribe_mode_btn}.get(mode, self.auto_mode_btn).setChecked(True)
        self.set_combo_data(
            self.batch_output_format,
            self.config_data.get("batch_output_format", "txt"),
        )
        self.batch_input_dir.setText(self.config_data.get("batch_input_dir", ""))
        self.batch_output_dir.setText(
            self.config_data.get("batch_output_dir", str(core.INSTALL_ROOT / "batch_outputs"))
        )
        self.batch_recursive.setChecked(bool(self.config_data.get("batch_recursive", True)))
        self.batch_skip_existing.setChecked(bool(self.config_data.get("batch_skip_existing", True)))
        self.on_batch_format_changed()
        self.refresh_batch_preview()

    def set_combo_text(self, combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def set_combo_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def current_mode(self) -> str:
        if self.align_mode_btn.isChecked():
            return "align"
        if self.transcribe_mode_btn.isChecked():
            return "transcribe"
        return "auto"

    def effective_mode(self) -> str:
        mode = self.current_mode()
        if mode == "auto":
            return "align" if self.script_text.toPlainText().strip() else "transcribe"
        return mode

    def on_mode_changed(self) -> None:
        self.update_mode_state()
        current_path = self.output_path.text().strip()
        if self.output_path_custom and current_path:
            # Keep the user's directory/name; only make sure the extension matches the format.
            extension = f".{self.current_output_format()}"
            if Path(current_path).suffix.lower() != extension:
                self.output_path.setText(str(Path(current_path).with_suffix(extension)))
        else:
            self.update_output_path()

    def on_output_path_edited(self, text: str) -> None:
        # Typing a path by hand makes it a custom path; clearing it returns to auto naming.
        self.output_path_custom = bool(text.strip())

    def on_audio_path_edited(self, _text: str) -> None:
        # 和“选择文件”一致：换了媒体就重新生成输出路径，否则新文件的结果会覆盖旧文件的字幕。
        self.output_path_custom = False
        self.update_output_path()

    def on_output_dir_edited(self) -> None:
        value = self.output_dir.text().strip()
        if value == self.output_dir_committed:
            return
        self.output_dir_committed = value
        self.output_path_custom = False
        self.update_output_path()

    def on_script_changed(self) -> None:
        self.update_mode_state()
        if not self.output_path_custom:
            self.update_output_path()

    def on_word_export_changed(self) -> None:
        if self.word_timestamp_export.currentData() != "none":
            self.whisperx_enabled.setChecked(True)

    def current_output_format(self) -> str:
        value = str(self.output_format.currentData() or "srt").lower()
        return value if value in {"srt", "txt"} else "srt"

    def on_output_format_changed(self) -> None:
        extension = f".{self.current_output_format()}"
        current_path = self.output_path.text().strip()
        self.update_mode_state()
        if self.output_path_custom and current_path:
            self.output_path.setText(str(Path(current_path).with_suffix(extension)))
        else:
            self.update_output_path()

    def update_mode_state(self) -> None:
        mode = self.effective_mode()
        output_label = self.current_output_format().upper()
        if mode == "align":
            self.mode_hint.setText(f"将按文稿内容做强制对齐并导出 {output_label}；AI 只校对文本。")
            self.script_label.setText("文稿")
            self.start_btn.setText(f"开始精对齐并导出 {output_label}")
            self.whisperx_enabled.setEnabled(True)
        else:
            self.mode_hint.setText(f"未提供文稿时直接 ASR 生成 {output_label}；开启 AI 后会修正识别文本。")
            self.script_label.setText("文稿（纯音频模式可留空）")
            self.start_btn.setText(f"开始转 {output_label}")
            self.whisperx_enabled.setEnabled(False)

    def output_directory(self) -> Path:
        """Folder for auto-named outputs: next to the media file, or the fixed default folder."""
        audio = self.audio_path.text().strip()
        if self.output_to_source.isChecked() and audio:
            return Path(audio).parent
        return Path(self.output_dir.text().strip() or str(core.INSTALL_ROOT / "outputs"))

    def generated_output_path(self) -> str:
        audio = self.audio_path.text().strip()
        stem = Path(audio).stem if audio else "output"
        suffix = "aligned" if self.effective_mode() == "align" else "transcribed"
        return str(self.output_directory() / f"{stem}.{suffix}.{self.current_output_format()}")

    def sync_output_location_widgets(self) -> None:
        fixed = not self.output_to_source.isChecked()
        self.output_dir.setEnabled(fixed)
        self.output_dir_pick_btn.setEnabled(fixed)

    def on_output_location_changed(self, _checked: bool) -> None:
        self.sync_output_location_widgets()
        self.output_path_custom = False
        self.update_output_path()

    def update_output_path(self) -> None:
        if not self.output_path_custom:
            self.output_path.setText(self.generated_output_path())

    def pick_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择音频/视频", "", "媒体文件 (*.wav *.mp3 *.m4a *.flac *.aac *.ogg *.wma *.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*.*)")
        if path:
            self.audio_path.setText(path)
            self.output_path_custom = False
            self.update_output_path()

    def pick_text(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 TXT 文稿", "", "文本文件 (*.txt);;所有文件 (*.*)")
        if path:
            self.load_text_file(path)

    def pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择固定输出目录")
        if path:
            self.output_dir.setText(path)
            self.output_dir_committed = path.strip()
            self.output_path_custom = False
            self.update_output_path()

    def open_output_dir(self) -> None:
        path = self.output_directory()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def pick_output_file(self) -> None:
        output_format = self.current_output_format()
        label = output_format.upper()
        file_filter = "SRT 字幕 (*.srt)" if output_format == "srt" else "TXT 文本 (*.txt)"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"自定义保存 {label}",
            self.output_path.text() or self.generated_output_path(),
            f"{file_filter};;所有文件 (*.*)",
        )
        if path:
            path = str(Path(path).with_suffix(f".{output_format}"))
            # One-off "save as": do not touch the persistent default output directory.
            self.output_path_custom = True
            self.output_path.setText(path)

    def pick_batch_input_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择包含音频/视频的目录")
        if path:
            self.batch_input_dir.setText(path)
            current_output = self.batch_output_dir.text().strip()
            default_output = str(core.INSTALL_ROOT / "batch_outputs")
            uses_generated_output = bool(
                current_output
                and Path(current_output).name.lower() in {"txt_outputs", "srt_outputs"}
            )
            if not current_output or current_output == default_output or uses_generated_output:
                extension = str(self.batch_output_format.currentData() or "txt")
                self.batch_output_dir.setText(str(Path(path) / f"{extension}_outputs"))
            self.refresh_batch_preview()

    def pick_batch_output_dir(self) -> None:
        extension = str(self.batch_output_format.currentData() or "txt").upper()
        path = QFileDialog.getExistingDirectory(self, f"选择 {extension} 输出目录")
        if path:
            self.batch_output_dir.setText(path)

    def open_batch_output_dir(self) -> None:
        value = self.batch_output_dir.text().strip()
        if not value:
            QMessageBox.warning(self, "缺少目录", "请先选择批量输出目录。")
            return
        path = Path(value)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_batch_format_changed(self, _index: int = -1) -> None:
        if not hasattr(self, "batch_output_format"):
            return
        extension = str(self.batch_output_format.currentData() or "txt")
        is_srt = extension == "srt"
        self.batch_output_label.setText(f"{extension.upper()} 输出目录")
        self.batch_skip_existing.setText(f"跳过已经存在的 {extension.upper()}")
        self.batch_start_btn.setText(
            "开始批量 ASR + 精对齐 + AI 纠错"
            if is_srt else "开始批量 ASR + LLM 纠错"
        )
        self.batch_intro.setText(
            (
                "扫描目录中的音频/视频，逐个自动检测语言，先 ASR，再执行 WhisperX 精对齐，"
                "最后使用设置页中的 LLM 纠错并按原文件名输出 SRT。ASR 模型只加载一次，"
                "对齐模型按语言复用；单个文件失败不会中断整批任务。"
            )
            if is_srt else (
                "扫描目录中的音频/视频，逐个自动检测语言，先 ASR，再使用设置页中的 LLM 纠错，"
                "最后按原文件名输出 TXT。模型在整批任务中只加载一次。"
            )
        )
        output_value = self.batch_output_dir.text().strip()
        if output_value and Path(output_value).name.lower() in {"txt_outputs", "srt_outputs"}:
            self.batch_output_dir.setText(
                str(Path(output_value).parent / f"{extension}_outputs")
            )

    def on_batch_input_dir_edited(self) -> None:
        if self.batch_input_dir.text().strip() != self.batch_input_dir_committed:
            self.refresh_batch_preview()

    def refresh_batch_preview(self) -> None:
        if self.batch_job_running:
            return
        value = self.batch_input_dir.text().strip()
        self.batch_input_dir_committed = value
        self.preview_generation += 1
        generation = self.preview_generation
        self.completed_batch_paths.clear()
        self.skipped_batch_paths.clear()
        self.failed_batch_paths.clear()
        self.update_batch_metrics()
        self.batch_retry_btn.setEnabled(False)
        if not value:
            self.batch_preview_label.setText("尚未选择媒体目录")
            self.batch_table.setRowCount(0)
            self.batch_row_by_path.clear()
            return
        self.batch_preview_label.setText("正在扫描媒体文件 ...")
        recursive = self.batch_recursive.isChecked()

        def scan() -> None:
            try:
                root = Path(value).expanduser().resolve()
                found = core.discover_batch_media(value, recursive)
                relative_files = [str(path.relative_to(root)) for path in found]
                self.ui_events.batch_preview.emit(generation, relative_files, "")
            except Exception as exc:
                self.ui_events.batch_preview.emit(generation, [], str(exc))

        threading.Thread(target=scan, daemon=True, name="batch-preview").start()

    def apply_batch_preview(self, generation: int, files: object, error: str) -> None:
        if generation != self.preview_generation:
            return
        if error:
            self.batch_preview_label.setText(f"目录不可用：{error}")
            self.batch_table.setRowCount(0)
            self.batch_row_by_path.clear()
            return
        paths = [str(path) for path in (files or [])]
        self.batch_total_metric.setText(str(len(paths)))
        preview_limit = 500
        self.batch_preview_label.setText(
            f"已找到 {len(paths)} 个媒体文件"
            + (f"（表格显示前 {preview_limit} 个）" if len(paths) > preview_limit else "")
        )
        self.batch_table.setRowCount(0)
        self.batch_row_by_path.clear()
        for path in paths[:preview_limit]:
            self.ensure_batch_row(path, "待处理", "")

    def update_batch_metrics(self) -> None:
        if hasattr(self, "batch_completed_metric"):
            self.batch_completed_metric.setText(str(len(self.completed_batch_paths)))
            self.batch_failed_metric.setText(str(len(self.failed_batch_paths)))

    def ensure_batch_row(self, path: str, status: str, output: str) -> int:
        key = path.casefold()
        row = self.batch_row_by_path.get(key)
        if row is None:
            row = self.batch_table.rowCount()
            self.batch_table.insertRow(row)
            self.batch_row_by_path[key] = row
            self.batch_table.setItem(row, 0, QTableWidgetItem(path))
        self.batch_table.setItem(row, 1, QTableWidgetItem(status))
        self.batch_table.setItem(row, 2, QTableWidgetItem(output))
        return row

    def pick_model(self) -> None:
        self.open_model_dir()

    def open_model_dir(self) -> None:
        path = Path(core.default_model_root_dir())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def load_text_file(self, path: str) -> None:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.critical(
                self, "无法读取文稿",
                f"读取文件失败（可能不存在或被其他程序占用）：\n{path}\n\n{exc}",
            )
            return
        content = decode_script_bytes(data)
        if content is None:
            QMessageBox.warning(
                self, "无法识别文稿编码",
                f"无法按 UTF-8 / UTF-16 / GBK 读取该文件，已取消加载以免出现乱码：\n{path}\n\n"
                "请用记事本等工具另存为 UTF-8 编码后重试。",
            )
            return
        self.text_path.setText(path)
        self.script_text.setPlainText(content)
        self.update_mode_state()

    def handle_drop_path(self, path: str) -> None:
        suffix = Path(path).suffix.lower()
        if suffix == ".txt":
            self.load_text_file(path)
        elif suffix in AUDIO_SUFFIXES:
            self.audio_path.setText(path)
            self.output_path_custom = False
            self.update_output_path()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            self.handle_drop_path(url.toLocalFile())

    def gather_config(self) -> dict:
        return {
            "audio_path": self.audio_path.text(),
            "text_path": self.text_path.text(),
            "output_dir": self.output_dir.text(),
            "output_location": "source" if self.output_to_source.isChecked() else "fixed",
            "output_path": self.output_path.text(),
            "output_path_custom": self.output_path_custom,
            "output_format": self.current_output_format(),
            "model_path": str(core.default_user_model_dir()),
            "mode": self.current_mode(),
            "device": self.device.currentText(),
            "compute_type": self.compute_type.currentText(),
            "performance_preset": self.performance_preset.currentData(),
            "language": self.language.currentData(),
            "ai_enabled": self.ai_enabled.isChecked(),
            "whisperx_enabled": self.whisperx_enabled.isChecked(),
            "word_timestamp_export": self.word_timestamp_export.currentData(),
            "diarization_enabled": self.diarization_enabled.isChecked(),
            "base_url": self.base_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "ai_model": self.ai_model.text().strip(),
            "hf_token": self.hf_token.text().strip(),
            "min_speakers": self.min_speakers.text().strip(),
            "max_speakers": self.max_speakers.text().strip(),
            "system_prompt": self.prompt_text.toPlainText().strip(),
            "ai_glossary": self.glossary_text.toPlainText().strip(),
            "onset_lead": self.onset_lead.value(),
            "script": self.script_text.toPlainText(),
            "batch_input_dir": self.batch_input_dir.text().strip(),
            "batch_output_dir": self.batch_output_dir.text().strip(),
            "batch_output_format": self.batch_output_format.currentData(),
            "batch_recursive": self.batch_recursive.isChecked(),
            "batch_skip_existing": self.batch_skip_existing.isChecked(),
        }

    def save_config(self) -> None:
        data = self.gather_config()
        data.pop("script", None)
        data.pop("audio_path", None)
        data.pop("text_path", None)
        path = core.save_settings(data)
        self.log(f"配置已保存: {path}")

    def start_job(self) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "正在运行", "当前任务还没有结束。")
            return
        output_path = self.output_path.text().strip() or self.generated_output_path()
        self.output_path.setText(
            str(Path(output_path).with_suffix(f".{self.current_output_format()}"))
        )
        self.save_config()
        config = self.gather_config()
        self.launch_worker(config, self.on_job_finished)

    def start_batch_job(self, _checked: bool = False, *, only_paths: list[str] | None = None) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "正在运行", "当前任务还没有结束。")
            return
        self.save_config()
        config = self.gather_config()
        if only_paths:
            config["batch_only_files"] = list(only_paths)
            for path in only_paths:
                self.ensure_batch_row(path, "等待重试", "")
        else:
            self.failed_batch_paths.clear()
            self.completed_batch_paths.clear()
            self.skipped_batch_paths.clear()
            self.update_batch_metrics()
            self.batch_retry_btn.setEnabled(False)
        config["job_type"] = (
            "batch_srt" if config.get("batch_output_format") == "srt" else "batch_txt"
        )
        # Invalidate any in-flight preview scan so a slow result cannot reset the job's table.
        self.preview_generation += 1
        self.batch_subset_run = bool(only_paths)
        self.launch_worker(config, self.on_batch_finished)

    def retry_failed_batch_items(self) -> None:
        if not self.failed_batch_paths:
            QMessageBox.information(self, "没有失败项", "当前没有可重试的文件。")
            return
        self.start_batch_job(only_paths=sorted(self.failed_batch_paths))

    def launch_worker(self, config: dict, finished_handler) -> None:
        self.batch_job_running = str(config.get("job_type", "")).startswith("batch")
        self.set_busy(True)
        self.job_started_at = time.monotonic()
        self.worker_thread = QThread()
        self.worker = JobWorker(config)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.set_progress)
        self.worker.task_event.connect(self.handle_task_event)
        self.worker.finished.connect(finished_handler)
        self.worker.failed.connect(self.on_job_failed)
        self.worker.cancelled.connect(self.on_job_cancelled)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker.cancelled.connect(self.worker_thread.quit)
        # worker 和 QThread 都由 Python 持有引用，这里不能再接 deleteLater，否则会重复释放。
        self.worker_thread.finished.connect(self.on_worker_thread_finished)
        self.worker_thread.start()

    def on_job_finished(self, output: str) -> None:
        output_format = self.current_output_format()
        if output_format == "srt":
            self.last_srt_path = output
            self.load_srt_to_speaker_table(output)
        else:
            self.last_srt_path = ""
            self.speaker_table.setRowCount(0)
            self.speaker_combo.clear()
            self.speaker_file_label.setText("当前输出为 TXT；说话人编辑仅支持 SRT。")
        QMessageBox.information(self, "完成", f"{output_format.upper()} 已保存:\n{output}")

    def on_job_failed(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)

    def on_job_cancelled(self, message: str) -> None:
        self.log(message or "任务已取消。")
        self.status_label.setText("已取消")

    def on_batch_finished(self, output_dir: str) -> None:
        extension = str(self.batch_output_format.currentData() or "txt").upper()
        QMessageBox.information(self, "批量任务完成", f"{extension} 输出目录：\n{output_dir}")

    def cancel_current_job(self) -> None:
        if self.worker is None or not self.worker_thread or not self.worker_thread.isRunning():
            return
        self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("正在取消 ...")
        self.status_label.setText("正在安全停止，当前模型调用结束后生效 ...")

    def on_worker_thread_finished(self) -> None:
        elapsed = time.monotonic() - self.job_started_at if self.job_started_at else 0.0
        if elapsed > 0:
            self.log(f"本次任务耗时: {elapsed:.1f} 秒")
        # finished 信号发出时线程还没真正退出；这时释放 QThread 会触发
        # “Destroyed while thread is still running”，让整个进程中止。
        thread = self.worker_thread
        worker = self.worker
        self.worker = None
        self.worker_thread = None
        if thread is not None:
            thread.wait()
        del worker, thread
        self.batch_job_running = False
        self.batch_subset_run = False
        self.set_busy(False)
        if self.close_when_idle:
            QTimer.singleShot(0, self.close)

    def set_busy(self, busy: bool, cancellable: bool = True) -> None:
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setValue(0)
            self.update_task_stage("model")
        else:
            self.progress.setRange(0, 100)
        self.progress.setVisible(busy)
        self.task_dock.setVisible(busy)
        # Background tasks (model download, runtime repair) have no worker to cancel.
        self.stage_bar.setVisible(busy and cancellable)
        self.cancel_btn.setVisible(busy and cancellable)
        self.cancel_btn.setEnabled(busy and cancellable)
        self.cancel_btn.setText("取消任务")
        self.status_label.setText("处理中..." if busy else "就绪")
        self.start_btn.setEnabled(not busy)
        self.batch_start_btn.setEnabled(not busy)
        self.batch_refresh_btn.setEnabled(not busy)
        self.batch_recursive.setEnabled(not busy)
        self.batch_input_dir.setEnabled(not busy)
        self.batch_input_btn.setEnabled(not busy)
        self.batch_output_format.setEnabled(not busy)
        self.output_format.setEnabled(not busy)
        self.gpu_btn.setEnabled(not busy)
        self.repair_cuda_btn.setEnabled(not busy)
        self.model_check_btn.setEnabled(not busy)
        self.release_models_btn.setEnabled(not busy)
        self.batch_retry_btn.setEnabled(not busy and bool(self.failed_batch_paths))
        if hasattr(self, "download_pyannote_btn"):
            self.download_pyannote_btn.setEnabled(not busy)
            self.check_pyannote_btn.setEnabled(not busy)

    def update_task_stage(self, stage: str) -> None:
        order = {"model": 0, "asr": 1, "align": 2, "speaker": 3, "ai": 3, "export": 4, "done": 5}
        active = order.get(stage, 0)
        for index, label in enumerate(self.stage_labels):
            if stage == "done" or index < active:
                state = "done"
            elif index == active:
                state = "active"
            else:
                state = "pending"
            label.setProperty("stageState", state)
            label.style().unpolish(label)
            label.style().polish(label)

    def handle_task_event(self, event: dict) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "stage":
            self.update_task_stage(str(event.get("stage", "model")))
            label = str(event.get("label", ""))
            if label:
                self.status_label.setText(label)
            return
        if event_type == "batch_discovered":
            files = [str(path) for path in event.get("files", [])]
            if self.batch_subset_run:
                # Retry of a subset: keep the full table, only make sure the rows exist.
                for path in files:
                    if path.casefold() not in self.batch_row_by_path:
                        self.ensure_batch_row(path, "等待重试", "")
                return
            self.apply_batch_preview(self.preview_generation, files, "")
            return
        if event_type != "file_status":
            return
        path = str(event.get("path", ""))
        raw_status = str(event.get("status", ""))
        status_labels = {
            "running": "处理中",
            "completed": "已完成",
            "skipped": "已跳过",
            "failed": "失败",
        }
        output = str(event.get("output", ""))
        self.ensure_batch_row(path, status_labels.get(raw_status, raw_status), output)
        key = path.casefold()
        if raw_status == "failed":
            self.failed_batch_paths.add(path)
        elif raw_status == "completed":
            self.completed_batch_paths.add(path)
            self.failed_batch_paths = {
                item for item in self.failed_batch_paths if item.casefold() != key
            }
        elif raw_status == "skipped":
            self.skipped_batch_paths.add(path)
        self.update_batch_metrics()
        self.batch_retry_btn.setEnabled(
            not (self.worker_thread and self.worker_thread.isRunning())
            and bool(self.failed_batch_paths)
        )

    def set_progress(self, value: int, message: str) -> None:
        if value < 0:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            if message:
                self.status_label.setText(message)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(max(0, min(100, value)))
        self.progress.setVisible(True)
        if message:
            self.status_label.setText(message)

    def apply_model_path(self, model_dir: str) -> None:
        self.model_path.setText(str(core.default_model_root_dir()))
        self.refresh_model_status()
        self.save_config()

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.log_text.append(line)
        if hasattr(self, "batch_log_text"):
            self.batch_log_text.append(line)

    def refresh_model_status(self) -> None:
        status = core.model_file_status(str(core.default_user_model_dir()))
        self.model_path.setText(str(core.default_model_root_dir()))
        total = int(status["total"])
        complete = int(status["complete"])
        percent = int(complete * 100 / total) if total else 0
        self.model_status_label.setText(f"当前模型: {Path(status['path']).name}    完整度: {complete}/{total}")
        self.model_status_progress.setValue(percent)
        for row, file_info in enumerate(status["files"]):
            name_label, state_label, found_label = self.model_file_rows[row]
            name_label.setText(str(file_info["label"]))
            state_label.setText("已存在" if file_info["ok"] else "缺失")
            found_label.setText(str(file_info["found"] or ""))

    def release_model_memory(self) -> None:
        stats = core.release_cached_models(self.log)
        QMessageBox.information(
            self,
            "模型内存已释放",
            "已释放当前进程中缓存的模型。\n"
            f"ASR: {'已释放' if stats['asr_loaded'] else '未加载'}\n"
            f"对齐模型: {stats['alignment_models']}\n"
            f"说话人模型: {stats['diarization_models']}",
        )

    def set_pyannote_text(self, text: str) -> None:
        self.pyannote_status.setText(text)

    def refresh_pyannote_status(self) -> None:
        status = core.pyannote_cache_status()
        if status["ready"]:
            self.pyannote_status.setText(f"已缓存: {status['label']}")
        else:
            self.pyannote_status.setText(f"未就绪: {status['label']}。需要 HF Token 并先在授权页面同意许可。")

    def check_pyannote_model(self) -> None:
        self.save_config()
        hf_token = self.hf_token.text().strip()

        def task() -> None:
            try:
                status = core.prepare_pyannote_model(hf_token, self.ui_events.log.emit)
                detail = "已缓存" if status.get("ready") else "未就绪"
                self.ui_events.pyannote_text.emit(f"{detail}: {status.get('label', '')}")
                self.ui_events.log.emit("说话人模型检查完成。")
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.pyannote_text.emit("未就绪。请确认 HF Token 正确，并已在授权页面同意许可。")
                self.ui_events.error.emit("说话人模型下载失败", str(exc))

        self.start_background_task("pyannote", task)

    def start_background_task(self, name: str, target, *, busy: bool = True) -> None:
        """Run ``target`` on a plain thread. It must only talk to the UI via ``self.ui_events``.

        Busy tasks are tracked so closing the window asks for confirmation and the
        (non-functional) cancel button stays hidden.
        """
        if busy:
            self.background_tasks.add(name)
            self.set_busy(True, cancellable=False)

        def runner() -> None:
            try:
                target()
            except BaseException:
                self.ui_events.log.emit(traceback.format_exc())
            finally:
                if busy:
                    self.ui_events.background_finished.emit(name)

        threading.Thread(target=runner, daemon=True, name=f"background-{name}").start()

    def on_background_task_finished(self, name: str) -> None:
        self.background_tasks.discard(name)
        if not self.background_tasks and not (self.worker_thread and self.worker_thread.isRunning()):
            self.set_busy(False)

    def set_runtime_downloads(self, downloads: object) -> None:
        self.cuda_links = dict(downloads or {})

    def open_download_link(self, key: str) -> None:
        if not self.cuda_links:
            self.cuda_links = core.recommended_runtime_plan().get("downloads", {})
        url = self.cuda_links.get(key, "")
        if not url:
            QMessageBox.warning(self, "没有链接", "请先检测运行环境。")
            return
        QDesktopServices.openUrl(QUrl(url))

    def set_runtime_text(self, text: str) -> None:
        self.runtime_status.setText(text)

    def set_runtime_links(self, html: str, visible: bool) -> None:
        self.runtime_links.setText(html)
        self.runtime_links.setVisible(visible)

    def runtime_link_html(self, downloads: dict) -> str:
        links = [
            ("NVIDIA 驱动下载", downloads.get("driver", "")),
            (f"CUDA {downloads.get('cuda_version', '')} local 安装包", downloads.get("cuda_local", "")),
            ("cuDNN 下载", downloads.get("cudnn", "")),
        ]
        return "<br>".join(f'<a href="{url}">{label}</a>' for label, url in links if url)

    def build_runtime_report(self, wants_cuda: bool) -> tuple[str, bool, dict]:
        """Runs on a background thread: must not touch widgets (read them before starting)."""
        core.add_cuda_dll_paths()
        import ctranslate2

        plan = core.recommended_runtime_plan()
        nvidia = plan["nvidia"]
        downloads = plan.get("downloads", {})
        ct2_cuda = ctranslate2.get_cuda_device_count()
        torch_runtime = core.check_torch_cuda_runtime()
        torch_cuda = bool(torch_runtime.get("available"))
        torch_usable = bool(torch_runtime.get("usable"))
        try:
            ffmpeg_path = core.add_bundled_ffmpeg_path()
        except Exception:
            ffmpeg_path = "未找到"

        if not wants_cuda:
            ok = True
            headline = "可以直接运行：当前设置为 CPU。"
        elif ct2_cuda > 0 and torch_usable:
            ok = True
            headline = "可以直接运行：ASR 与 WhisperX 的 CUDA 实际运算检测均已通过。"
        elif ct2_cuda > 0 and torch_cuda:
            ok = False
            headline = "CUDA 设备可见，但 PyTorch 真实运算失败；请点击“补齐缺失项”。"
        elif nvidia.get("available"):
            ok = False
            headline = "还不能用 CUDA 直接运行：已检测到 NVIDIA 驱动，但 Python CUDA 依赖未就绪。"
        else:
            ok = False
            headline = "还不能用 CUDA 直接运行：未检测到 nvidia-smi。"

        details = [
            headline,
            f"GPU: {nvidia.get('gpu_name') or '未检测到'}",
            f"Driver: {nvidia.get('driver_version') or '未知'}",
            f"nvidia-smi CUDA: {nvidia.get('cuda_version') or '未知'}",
            f"CTranslate2 CUDA 设备: {ct2_cuda}",
            f"PyTorch: {torch_runtime.get('torch_version') or '未知'} (CUDA {torch_runtime.get('torch_cuda_version') or '无'})",
            f"PyTorch CUDA 设备可见: {torch_cuda}",
            f"PyTorch CUDA 实际运算: {'通过' if torch_usable else '未通过'}",
            f"FFmpeg: {ffmpeg_path}",
        ]
        if torch_runtime.get("device_name"):
            details.append(
                f"PyTorch 设备: {torch_runtime['device_name']}，计算能力: {torch_runtime.get('compute_capability') or '未知'}"
            )
        if torch_runtime.get("supported_architectures"):
            details.append("PyTorch 内置架构: " + ", ".join(torch_runtime["supported_architectures"]))
        if torch_runtime.get("error"):
            details.append("真实运算错误: " + str(torch_runtime["error"]))
        return "\n".join(details), ok, downloads

    def parse_srt_file(self, path: str) -> list[dict]:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        items = []
        blocks = re.split(r"\n\s*\n", text.strip())
        for block in blocks:
            lines = [line.rstrip("\r") for line in block.splitlines()]
            if len(lines) < 3:
                continue
            try:
                index = int(lines[0].strip())
            except ValueError:
                continue
            if "-->" not in lines[1]:
                continue
            start, end = [part.strip() for part in lines[1].split("-->", 1)]
            body = "\n".join(lines[2:]).strip()
            speaker = ""
            match = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, flags=re.S)
            if match:
                speaker = match.group(1).strip()
                body = match.group(2).strip()
            items.append({"index": index, "start": start, "end": end, "speaker": speaker, "text": body})
        return items

    def load_current_srt_to_speaker_table(self) -> None:
        path = self.last_srt_path or self.output_path.text().strip()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "没有 SRT", "当前没有可加载的 SRT 文件。")
            return
        self.load_srt_to_speaker_table(path)

    def load_srt_to_speaker_table(self, path: str) -> bool:
        try:
            items = self.parse_srt_file(path)
        except OSError as exc:
            QMessageBox.critical(
                self, "无法读取 SRT",
                f"读取文件失败（可能不存在或被其他程序占用）：\n{path}\n\n{exc}",
            )
            return False
        self.last_srt_path = path
        self.speaker_table.blockSignals(True)
        try:
            self.speaker_table.setRowCount(0)
            for item in items:
                row = self.speaker_table.rowCount()
                self.speaker_table.insertRow(row)
                values = [item["index"], item["start"], item["end"], item["speaker"], item["text"]]
                for column, value in enumerate(values):
                    self.speaker_table.setItem(row, column, QTableWidgetItem(str(value)))
        finally:
            self.speaker_table.blockSignals(False)
        self.speaker_file_label.setText(str(Path(path).name))
        self.speaker_combo.clear()
        self.refresh_speaker_combo()
        self.speaker_table.resizeColumnsToContents()
        return True

    def refresh_speaker_combo(self) -> None:
        """Rebuild the speaker list from the (possibly edited) table, keeping the selection."""
        speakers = sorted({item["speaker"] for item in self.speaker_table_items() if item["speaker"]})
        if speakers == [self.speaker_combo.itemText(i) for i in range(self.speaker_combo.count())]:
            return
        current = self.speaker_combo.currentText().strip()
        self.speaker_combo.blockSignals(True)
        try:
            self.speaker_combo.clear()
            self.speaker_combo.addItems(speakers)
            if current in speakers:
                self.speaker_combo.setCurrentIndex(speakers.index(current))
        finally:
            self.speaker_combo.blockSignals(False)

    def on_speaker_item_changed(self, item: QTableWidgetItem) -> None:
        if item is not None and item.column() == 3:
            self.refresh_speaker_combo()

    def speaker_table_items(self) -> list[dict]:
        items = []
        for row in range(self.speaker_table.rowCount()):
            def cell(column: int) -> str:
                item = self.speaker_table.item(row, column)
                return item.text().strip() if item else ""

            try:
                index = int(cell(0))
            except ValueError:
                index = row + 1
            items.append({
                "index": index,
                "start": cell(1),
                "end": cell(2),
                "speaker": cell(3),
                "text": cell(4),
            })
        return items

    def srt_text_from_items(self, items: list[dict], speaker_filter: str = "") -> str:
        blocks = []
        output_index = 1
        for item in items:
            if speaker_filter and item["speaker"] != speaker_filter:
                continue
            text = item["text"]
            if item["speaker"]:
                text = f"[{item['speaker']}] {text}"
            blocks.append(f"{output_index}\n{item['start']} --> {item['end']}\n{text}")
            output_index += 1
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def show_write_error(self, path: Path | str, exc: Exception) -> None:
        QMessageBox.critical(
            self, "保存失败",
            f"无法写入文件（可能被其他程序占用，或没有写入权限）：\n{path}\n\n{exc}",
        )

    def save_edited_srt(self) -> None:
        if not self.last_srt_path:
            QMessageBox.warning(self, "没有 SRT", "当前没有可保存的 SRT。")
            return
        try:
            Path(self.last_srt_path).write_text(self.srt_text_from_items(self.speaker_table_items()), encoding="utf-8-sig")
        except OSError as exc:
            self.show_write_error(self.last_srt_path, exc)
            return
        self.log(f"已保存编辑后的 SRT: {self.last_srt_path}")

    def export_selected_speaker(self, kind: str) -> None:
        self.refresh_speaker_combo()
        speaker = self.speaker_combo.currentText().strip()
        if not speaker:
            QMessageBox.warning(self, "未选择说话人", "当前 SRT 没有可导出的说话人标签。")
            return
        items = [item for item in self.speaker_table_items() if item["speaker"] == speaker]
        if not items:
            QMessageBox.warning(self, "没有字幕", f"表格中没有说话人为“{speaker}”的字幕行，未导出文件。")
            return
        base = Path(self.last_srt_path or self.output_path.text().strip() or "speaker.srt")
        safe = safe_speaker_filename(speaker)
        if kind == "srt":
            path = base.with_name(f"{base.stem}.{safe}.edited.srt")
            content = self.srt_text_from_items(items, speaker)
        else:
            path = base.with_name(f"{base.stem}.{safe}.edited.txt")
            content = "\n".join(item["text"] for item in items) + "\n"
        try:
            path.write_text(content, encoding="utf-8-sig")
        except OSError as exc:
            self.show_write_error(path, exc)
            return
        self.log(f"已导出 {speaker}: {path}")

    def check_runtime(self) -> None:
        wants_cuda = self.device.currentText().lower() == "cuda"

        def task() -> None:
            try:
                text, ok, downloads = self.build_runtime_report(wants_cuda)
                self.ui_events.runtime_downloads.emit(downloads)
                self.ui_events.log.emit(text)
                self.ui_events.runtime_text.emit(text)
                if ok:
                    self.ui_events.runtime_links.emit("", False)
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.error.emit("运行环境检查失败", str(exc))

        self.start_background_task("runtime-check", task, busy=False)

    def complete_runtime_missing(self) -> None:
        wants_cuda = self.device.currentText().lower() == "cuda"

        def task() -> None:
            try:
                text, ok, downloads = self.build_runtime_report(wants_cuda)
                self.ui_events.runtime_downloads.emit(downloads)
                if ok and "PyTorch CUDA 实际运算: 通过" in text:
                    self.ui_events.runtime_links.emit("", False)
                    self.ui_events.runtime_text.emit(text + "\n\n无需补齐。")
                    return

                if core.detect_nvidia_smi().get("available"):
                    self.ui_events.runtime_text.emit(text + "\n\n正在补齐 Python CUDA 依赖 ...")
                    core.install_recommended_torch(self.ui_events.log.emit)
                    text_after, ok_after, downloads_after = self.build_runtime_report(wants_cuda)
                    self.ui_events.runtime_downloads.emit(downloads_after)
                    if ok_after:
                        self.ui_events.runtime_links.emit("", False)
                        self.ui_events.runtime_text.emit(text_after + "\n\n补齐完成，请重启软件后再检测一次。")
                        return

                self.ui_events.runtime_text.emit(text + "\n\n需要先安装下面的 NVIDIA 组件，然后重启电脑/软件再检测。")
                self.ui_events.runtime_links.emit(self.runtime_link_html(downloads), True)
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.error.emit("补齐运行环境失败", str(exc))

        self.start_background_task("runtime-repair", task)

    def check_model_files(self) -> None:
        def task() -> None:
            try:
                model_dir = core.prepare_model_files(
                    str(core.default_user_model_dir()),
                    self.ui_events.log.emit,
                    lambda value, message: self.ui_events.progress.emit(int(value), message),
                )
                self.ui_events.model_path.emit(model_dir)
                self.ui_events.log.emit("模型检查完成。")
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.error.emit("模型检查失败", str(exc))

        self.start_background_task("model-download", task)

    def closeEvent(self, event) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            answer = QMessageBox.question(
                self,
                "任务正在运行",
                "当前任务尚未完成。是否安全取消任务后退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.close_when_idle = True
            self.cancel_current_job()
            event.ignore()
            return
        if self.background_tasks:
            answer = QMessageBox.question(
                self,
                "后台任务正在运行",
                "模型下载或运行环境安装仍在进行。现在退出可能导致下载/安装不完整，"
                "下次需要重新补齐。\n\n确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def apply_style(app: QApplication) -> None:
    app.setStyleSheet(
        """
        QWidget {
            font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI";
            font-size: 13px;
            color: #dce6f3;
            background: transparent;
        }
        QMainWindow, QWidget#AppRoot { background: #080c12; }
        QFrame#Sidebar {
            background: #0c121b;
            border-right: 1px solid #182230;
        }
        QFrame#MainSurface { background: #0a0f16; }
        QLabel#BrandMark {
            border: 0;
            color: white;
            background: transparent;
            font-size: 14px;
            font-weight: 800;
        }
        QLabel#BrandTitle { color: #f8fbff; font-size: 17px; font-weight: 750; }
        QLabel#SidebarMuted { color: #718096; font-size: 11px; }
        QLabel#NavCaption, QLabel#BuildLabel {
            color: #526176;
            font-size: 10px;
            font-weight: 700;
            padding: 4px 6px;
        }
        QPushButton#NavButton {
            color: #8fa0b5;
            text-align: left;
            border: 0;
            border-radius: 10px;
            padding: 10px 12px;
            background: transparent;
            font-weight: 600;
        }
        QPushButton#NavButton:hover { color: #eaf2ff; background: #121c29; }
        QPushButton#NavButton:checked {
            color: #ffffff;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #273d72, stop:1 #142544);
            border: 1px solid #31528e;
        }
        QFrame#LocalCard {
            background: #101924;
            border: 1px solid #1c2a3a;
            border-radius: 12px;
        }
        QLabel#LocalTitle { color: #82ddb7; font-weight: 700; }
        QLabel#PageTitle { color: #f7faff; font-size: 27px; font-weight: 750; }
        QLabel#PageSubtitle { color: #718198; font-size: 12px; }
        QLabel#Status {
            color: #9ce6c4;
            background: #10291f;
            border: 1px solid #1e5a42;
            border-radius: 12px;
            padding: 7px 13px;
            font-weight: 700;
        }
        QStackedWidget#PageStack { border: 0; background: transparent; }
        QFrame#HeroCard {
            border: 1px solid #253d66;
            border-radius: 16px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #17233b, stop:0.55 #101a2b, stop:1 #122231);
        }
        QLabel#Eyebrow { color: #69a3ff; font-size: 10px; font-weight: 800; }
        QLabel#HeroTitle { color: #f7faff; font-size: 21px; font-weight: 750; }
        QLabel#HeroSubtitle { color: #8291a7; font-size: 12px; }
        QLabel#HeroBadge {
            color: #a8d8ff;
            background: #102846;
            border: 1px solid #285686;
            border-radius: 12px;
            padding: 7px 12px;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#Muted, QLabel#ModeHint { color: #7b8ba2; }
        QLabel#ModeHint { padding-left: 10px; }
        QLabel#Section { font-weight: 700; color: #edf4ff; }
        QFrame#ModeBar, QFrame#ToolbarCard, QFrame#TaskDock {
            border: 1px solid #1d2a3a;
            border-radius: 13px;
            background: #101720;
        }
        QFrame#StageBar { background: transparent; border: 0; }
        QGroupBox {
            border: 1px solid #1e2b3a;
            border-radius: 14px;
            background: #101720;
            margin-top: 16px;
            padding: 18px 12px 12px 12px;
            font-weight: 700;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 7px;
            color: #aebdd0;
            background: #101720;
        }
        QGroupBox QLabel, QFrame QLabel { background: transparent; }
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {
            border: 1px solid #263649;
            border-radius: 9px;
            padding: 8px 10px;
            background: #0b1119;
            color: #eef5ff;
            selection-background-color: #315fd6;
        }
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {
            border: 1px solid #4d79df;
            background: #0d1520;
        }
        QLineEdit:read-only { color: #9aa9bd; background: #0d131b; }
        QComboBox::drop-down { border: 0; width: 26px; }
        QComboBox QAbstractItemView {
            background: #111923;
            border: 1px solid #29394d;
            selection-background-color: #284d91;
            color: #eef5ff;
            outline: 0;
        }
        QPushButton {
            border: 1px solid #29394c;
            border-radius: 9px;
            padding: 8px 13px;
            background: #17212d;
            color: #dce7f5;
            font-weight: 600;
        }
        QPushButton:hover { background: #202d3c; border-color: #3b5069; color: #ffffff; }
        QPushButton:pressed { background: #101a25; }
        QPushButton:disabled { color: #4f5d6e; background: #10161e; border-color: #1b2531; }
        QPushButton#SegmentedButton {
            border: 0;
            border-radius: 9px;
            padding: 9px 15px;
            color: #8494aa;
            background: transparent;
        }
        QPushButton#SegmentedButton:hover { color: #dce9fa; background: #172230; }
        QPushButton#SegmentedButton:checked {
            color: #ffffff;
            background: #284c91;
            border: 1px solid #3f6cc4;
        }
        QPushButton#Primary {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #5a52e8, stop:1 #2677e8);
            border: 1px solid #688cf2;
            color: white;
            font-size: 14px;
            font-weight: 750;
        }
        QPushButton#Primary:hover { background: #386fe0; border-color: #8aa5ff; }
        QPushButton#Danger { color: #ffb7bd; border-color: #6c2c38; background: #321a21; }
        QPushButton#Danger:hover { background: #4a202a; }
        QCheckBox { spacing: 9px; padding: 4px 2px; color: #c7d3e2; }
        QLabel#TaskStage { border-radius: 8px; padding: 7px 10px; color: #65758a; background: #0c121a; }
        QLabel#TaskStage[stageState="active"] { color: #ffffff; background: #315fd0; font-weight: 750; }
        QLabel#TaskStage[stageState="done"] { color: #a7e8c7; background: #173529; }
        QFrame#MetricCard {
            border: 1px solid #1e2b3a;
            border-radius: 13px;
            background: #101720;
        }
        QFrame#MetricCard[tone="accent"] { border-color: #294b7a; background: #111f32; }
        QFrame#MetricCard[tone="success"] { border-color: #24513e; background: #10241d; }
        QFrame#MetricCard[tone="danger"] { border-color: #59303a; background: #25171b; }
        QLabel#MetricValue { color: #f7fbff; font-size: 22px; font-weight: 780; }
        QLabel#MetricLabel { color: #77889f; font-size: 11px; }
        QSplitter::handle { background: transparent; width: 12px; height: 12px; }
        QProgressBar { border: 0; border-radius: 4px; background: #182230; height: 7px; text-align: center; }
        QProgressBar::chunk { border-radius: 4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6157eb, stop:1 #2794ff); }
        QScrollArea { background: transparent; border: 0; }
        QTableWidget {
            gridline-color: #1c2836;
            alternate-background-color: #0e151e;
            background: #0b1119;
            border: 1px solid #1f2c3b;
            border-radius: 12px;
            selection-background-color: #203f78;
        }
        QTableWidget::item { padding: 7px; border-bottom: 1px solid #172230; }
        QHeaderView::section { background: #121b26; color: #8fa0b5; border: 0; padding: 9px; font-weight: 700; }
        QTableCornerButton::section { background: #121b26; border: 0; }
        QScrollBar:vertical {
            background: transparent; width: 10px; margin: 2px; border-radius: 5px;
        }
        QScrollBar::handle:vertical {
            background: #334155; border-radius: 5px; min-height: 48px;
        }
        QScrollBar::handle:vertical:hover { background: #52657c; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """
    )


def self_test() -> dict:
    core.add_cuda_dll_paths()
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
    return result


def install_exception_hooks(get_window) -> None:
    """Surface uncaught exceptions: the GUI runs under pythonw, so stderr is invisible.

    ``get_window`` returns the MainWindow (or None while it is being constructed).
    Reports are routed through ``ui_events`` signals, so this is safe from any thread.
    """
    state = {"depth": 0}

    def report(exc_type, exc_value, exc_tb) -> None:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            if sys.stderr is not None:
                sys.__excepthook__(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        summary = f"{getattr(exc_type, '__name__', 'Error')}: {exc_value}"
        state["depth"] += 1
        try:
            window = get_window()
            if window is not None:
                window.ui_events.log.emit("未处理的错误：\n" + text)
                if state["depth"] == 1:
                    window.ui_events.error.emit(
                        "程序出错",
                        f"发生未处理的错误，当前操作可能没有完成：\n{summary}\n\n详细信息已写入日志。",
                    )
            elif (
                state["depth"] == 1
                and QApplication.instance() is not None
                and threading.current_thread() is threading.main_thread()
            ):
                QMessageBox.critical(None, "程序出错", f"{summary}\n\n{text[-3000:]}")
        except Exception:
            pass
        finally:
            state["depth"] -= 1

    def thread_report(args) -> None:
        if args.exc_type is SystemExit:
            return
        report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = report
    threading.excepthook = thread_report


PREFERRED_WINDOW_SIZE = (1600, 960)


def show_main_window(window: QMainWindow) -> None:
    """Open at a comfortable fixed size, centered; only maximize when the screen is too small."""
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:
        window.resize(*PREFERRED_WINDOW_SIZE)
        window.show()
        return
    available = screen.availableGeometry()
    minimum = window.minimumSize()
    if available.width() < minimum.width() + 80 or available.height() < minimum.height() + 60:
        window.showMaximized()
        return
    width = max(minimum.width(), min(PREFERRED_WINDOW_SIZE[0], int(available.width() * 0.85)))
    height = max(minimum.height(), min(PREFERRED_WINDOW_SIZE[1], int(available.height() * 0.88)))
    window.resize(width, height)
    frame = window.frameGeometry()
    frame.moveCenter(available.center())
    window.move(frame.topLeft())
    window.show()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        result = self_test()
        if "--self-test-file" in sys.argv:
            target = Path(sys.argv[sys.argv.index("--self-test-file") + 1])
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0)

    qt_app = QApplication(sys.argv)
    if APP_ICON_PATH.exists():
        qt_app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    apply_style(qt_app)
    window_holder: dict[str, MainWindow] = {}
    install_exception_hooks(lambda: window_holder.get("window"))
    window = MainWindow()
    window_holder["window"] = window
    show_main_window(window)
    sys.exit(qt_app.exec())
