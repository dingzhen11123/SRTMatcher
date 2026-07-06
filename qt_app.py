from __future__ import annotations

import json
import builtins as _builtins
import re
import sys
import threading
import time
import traceback
from pathlib import Path

_ORIGINAL_IMPORT = _builtins.__import__

from PySide6.QtCore import QObject, QThread, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_builtins.__import__ = _ORIGINAL_IMPORT

import app as core


AUDIO_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".wma", ".mp4", ".mkv",
    ".mov", ".webm", ".avi", ".mpeg", ".mpg", ".flv",
}


class JobWorker(QObject):
    log = Signal(str)
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config = config

    def run(self) -> None:
        try:
            output = core.run_srt_job(
                self.config,
                self.log.emit,
                lambda value, message: self.progress.emit(int(value), message),
            )
            self.finished.emit(output)
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(exc))


class UiEvents(QObject):
    log = Signal(str)
    busy = Signal(bool)
    progress = Signal(int, str)
    model_path = Signal(str)
    error = Signal(str, str)
    runtime_text = Signal(str)
    runtime_links = Signal(str, bool)
    pyannote_text = Signal(str)


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
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def setContentWidget(self, widget: QWidget) -> None:
        self.content_widget = widget
        widget.setMinimumHeight(self.content_height)
        widget.setFixedHeight(self.content_height)
        self.setWidget(widget)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.content_widget is not None:
            width = max(720, self.viewport().width())
            self.content_widget.setFixedWidth(width)


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

        self.setWindowTitle("SRTMatcher")
        self.resize(1120, 720)
        self.setMinimumSize(920, 620)
        self.setAcceptDrops(True)

        self.build_ui()
        self.ui_events.log.connect(self.log)
        self.ui_events.busy.connect(self.set_busy)
        self.ui_events.progress.connect(self.set_progress)
        self.ui_events.model_path.connect(self.apply_model_path)
        self.ui_events.error.connect(lambda title, message: QMessageBox.critical(self, title, message))
        self.ui_events.runtime_text.connect(self.set_runtime_text)
        self.ui_events.runtime_links.connect(self.set_runtime_links)
        self.ui_events.pyannote_text.connect(self.set_pyannote_text)
        self.load_config_to_ui()
        self.update_mode_state()
        self.update_output_path()
        self.refresh_model_status()
        self.refresh_pyannote_status()
        self.log(f"配置文件: {core.SETTINGS_PATH}")

    def build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("SRTMatcher")
        title.setObjectName("Title")
        subtitle = QLabel("文稿精对齐 / 纯音频转 SRT / AI 字幕修正 / 说话人识别")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Status")
        header.addWidget(self.status_label)
        outer.addLayout(header)

        mode_bar = QFrame()
        mode_bar.setObjectName("ModeBar")
        mode_layout = QHBoxLayout(mode_bar)
        mode_layout.setContentsMargins(10, 8, 10, 8)
        mode_layout.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.auto_mode_btn = self.mode_button("自动判断")
        self.align_mode_btn = self.mode_button("文稿精对齐")
        self.transcribe_mode_btn = self.mode_button("纯音频转 SRT")
        for idx, btn in enumerate([self.auto_mode_btn, self.align_mode_btn, self.transcribe_mode_btn]):
            self.mode_group.addButton(btn, idx)
            mode_layout.addWidget(btn)
        self.auto_mode_btn.setChecked(True)
        self.mode_group.idClicked.connect(lambda _id: self.on_mode_changed())
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Muted")
        mode_layout.addWidget(self.mode_hint, 1)
        outer.addWidget(mode_bar)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self.build_work_tab(), "制作")
        self.tabs.addTab(self.build_settings_tab(), "设置")
        self.tabs.addTab(self.build_speaker_tab(), "说话人结果")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        outer.addWidget(self.progress)

    def mode_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setMinimumHeight(38)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return btn

    def build_work_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(10)

        files = QGroupBox("输入")
        file_grid = QGridLayout(files)
        self.audio_path = QLineEdit()
        self.text_path = QLineEdit()
        self.output_dir = QLineEdit()
        self.output_path = QLineEdit()
        self.model_path = QLineEdit()
        self.model_path.setReadOnly(True)
        self.output_dir.editingFinished.connect(lambda: (setattr(self, "output_path_custom", False), self.update_output_path()))
        self.add_path_row(file_grid, 0, "音频/视频", self.audio_path, self.pick_audio)
        self.add_path_row(file_grid, 1, "文稿 TXT", self.text_path, self.pick_text)
        self.add_path_row(file_grid, 2, "本次保存为", self.output_path, self.pick_output_file, "另存")
        left_layout.addWidget(files)

        options = QGroupBox("运行")
        opt_grid = QGridLayout(options)
        self.language = QComboBox()
        for label, code in core.LANGUAGE_CHOICES:
            self.language.addItem(label, code)
        self.device = QComboBox()
        self.device.addItems(["cuda", "cpu"])
        self.compute_type = QComboBox()
        self.compute_type.addItems(["float16", "int8_float16", "int8"])
        opt_grid.addWidget(QLabel("语言"), 0, 0)
        opt_grid.addWidget(self.language, 0, 1)
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
        opt_grid.addWidget(self.ai_enabled, 1, 0, 1, 2)
        opt_grid.addWidget(self.whisperx_enabled, 2, 0, 1, 2)
        opt_grid.addWidget(self.diarization_enabled, 3, 0, 1, 2)
        opt_grid.addWidget(self.advanced_runtime, 4, 0, 1, 2)
        left_layout.addWidget(options)

        actions = QHBoxLayout()
        self.start_btn = QPushButton("开始")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start_job)
        actions.addStretch(1)
        actions.addWidget(self.start_btn)
        left_layout.addLayout(actions)
        left_layout.addStretch(1)

        right = QWidget()
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
        right_layout.addWidget(self.script_label)
        right_layout.addWidget(self.script_text, 3)
        log_label = QLabel("日志")
        log_label.setObjectName("Section")
        right_layout.addWidget(log_label)
        right_layout.addWidget(self.log_text, 2)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([390, 780])
        return page

    def build_ai_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        grid = QGridLayout()
        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_model = QLineEdit()
        self.hf_token = QLineEdit()
        self.hf_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.min_speakers = QLineEdit()
        self.max_speakers = QLineEdit()
        self.add_form_row(grid, 0, "Base URL", self.base_url)
        self.add_form_row(grid, 1, "API Key", self.api_key)
        self.add_form_row(grid, 2, "模型名", self.ai_model)
        self.add_form_row(grid, 3, "Hugging Face Token", self.hf_token)
        self.add_form_row(grid, 4, "最小说话人数", self.min_speakers)
        self.add_form_row(grid, 5, "最大说话人数", self.max_speakers)
        layout.addLayout(grid)

        layout.addWidget(QLabel("系统提示词"))
        self.prompt_text = QPlainTextEdit()
        self.prompt_text.setPlainText(self.config_data.get("system_prompt", core.DEFAULT_SYSTEM_PROMPT))
        layout.addWidget(self.prompt_text, 2)

        test_row = QHBoxLayout()
        self.save_btn = QPushButton("保存配置")
        self.save_btn.clicked.connect(self.save_config)
        test_row.addStretch(1)
        test_row.addWidget(self.save_btn)
        layout.addLayout(test_row)
        return page

    def build_settings_tab(self) -> QWidget:
        scroll = SettingsScrollArea(980)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        top = QGroupBox("常用设置")
        top.setMinimumHeight(180)
        top_grid = QGridLayout(top)
        top_grid.setColumnStretch(1, 1)
        self.add_settings_path_row(top_grid, 0, "默认输出目录", self.output_dir, self.pick_output_dir, self.open_output_dir)
        self.add_settings_path_row(top_grid, 1, "模型目录", self.model_path, self.open_model_dir, None, browse_text="打开")
        top_grid.addWidget(QLabel("设备"), 2, 0)
        top_grid.addWidget(self.device, 2, 1)
        top_grid.addWidget(QLabel("精度"), 3, 0)
        top_grid.addWidget(self.compute_type, 3, 1)
        self.save_settings_btn = QPushButton("保存设置")
        self.save_settings_btn.clicked.connect(self.save_config)
        top_grid.addWidget(self.save_settings_btn, 3, 2)
        layout.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(10)

        model_box = QGroupBox("模型")
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
        model_buttons.addWidget(self.refresh_model_btn)
        model_buttons.addWidget(self.download_model_btn)
        model_buttons.addWidget(self.open_model_btn)
        model_buttons.addStretch(1)
        model_layout.addWidget(self.model_status_label)
        model_layout.addWidget(self.model_status_progress)
        model_layout.addLayout(self.model_files_grid)
        model_layout.addLayout(model_buttons)
        body.addWidget(model_box, 1)

        runtime_box = QGroupBox("运行环境")
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

        ai_box = QGroupBox("AI 校对接口")
        ai_box.setMinimumHeight(310)
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
        self.prompt_text.setPlainText(self.config_data.get("system_prompt", core.DEFAULT_SYSTEM_PROMPT))
        ai_layout.addWidget(QLabel("系统提示词"))
        ai_layout.addWidget(self.prompt_text)
        lower.addWidget(ai_box, 1)

        speaker_box = QGroupBox("说话人模型")
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
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        self.speaker_file_label = QLabel("未加载 SRT")
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
        toolbar.addWidget(self.save_speaker_btn)
        toolbar.addWidget(self.export_speaker_srt_btn)
        toolbar.addWidget(self.export_speaker_txt_btn)
        layout.addLayout(toolbar)
        self.speaker_table = QTableWidget(0, 5)
        self.speaker_table.setHorizontalHeaderLabels(["序号", "开始", "结束", "说话人", "文本"])
        self.speaker_table.verticalHeader().setVisible(False)
        layout.addWidget(self.speaker_table, 1)
        return page

    def add_path_row(self, grid: QGridLayout, row: int, label: str, edit: QLineEdit, picker, button_text: str = "选择") -> None:
        button = QPushButton(button_text)
        button.clicked.connect(picker)
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        grid.addWidget(button, row, 2)

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
    ) -> None:
        pick_btn = QPushButton(browse_text)
        pick_btn.clicked.connect(picker)
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        grid.addWidget(pick_btn, row, 2)
        if opener is not None:
            open_btn = QPushButton(open_text)
            open_btn.clicked.connect(opener)
            grid.addWidget(open_btn, row, 3)

    def load_config_to_ui(self) -> None:
        self.output_dir.setText(self.config_data.get("output_dir", str(core.INSTALL_ROOT / "outputs")))
        self.output_path.setText(self.config_data.get("output_path", ""))
        self.model_path.setText(str(core.default_model_root_dir()))
        self.set_combo_data(self.language, self.config_data.get("language", "auto"))
        self.set_combo_text(self.device, self.config_data.get("device", "cuda"))
        self.set_combo_text(self.compute_type, self.config_data.get("compute_type", "float16"))
        self.ai_enabled.setChecked(bool(self.config_data.get("ai_enabled", True)))
        self.whisperx_enabled.setChecked(bool(self.config_data.get("whisperx_enabled", True)))
        self.diarization_enabled.setChecked(bool(self.config_data.get("diarization_enabled", False)))
        self.base_url.setText(self.config_data.get("base_url", "https://api.openai.com/v1"))
        self.api_key.setText(self.config_data.get("api_key", ""))
        self.ai_model.setText(self.config_data.get("ai_model", "gemini-2.5-pro"))
        self.hf_token.setText(self.config_data.get("hf_token", ""))
        self.min_speakers.setText(str(self.config_data.get("min_speakers", "")))
        self.max_speakers.setText(str(self.config_data.get("max_speakers", "")))
        mode = self.config_data.get("mode", "auto")
        {"auto": self.auto_mode_btn, "align": self.align_mode_btn, "transcribe": self.transcribe_mode_btn}.get(mode, self.auto_mode_btn).setChecked(True)

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
        self.output_path_custom = False
        self.update_mode_state()
        self.update_output_path()

    def on_script_changed(self) -> None:
        self.update_mode_state()
        if not self.output_path_custom:
            self.update_output_path()

    def update_mode_state(self) -> None:
        mode = self.effective_mode()
        if mode == "align":
            self.mode_hint.setText("将按文稿内容做强制对齐；AI 只校对文本，不改时间轴。")
            self.script_label.setText("文稿")
            self.start_btn.setText("开始文稿精对齐")
            self.whisperx_enabled.setEnabled(True)
        else:
            self.mode_hint.setText("未提供文稿时直接 ASR 生成 SRT；开启 AI 后会修正识别文本。")
            self.script_label.setText("文稿（纯音频模式可留空）")
            self.start_btn.setText("开始转 SRT")
            self.whisperx_enabled.setEnabled(False)

    def generated_output_path(self) -> str:
        audio = self.audio_path.text().strip()
        stem = Path(audio).stem if audio else "output"
        suffix = "aligned" if self.effective_mode() == "align" else "transcribed"
        out_dir = Path(self.output_dir.text().strip() or str(core.INSTALL_ROOT / "outputs"))
        return str(out_dir / f"{stem}.{suffix}.srt")

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
            self.output_path_custom = False
            self.update_output_path()

    def open_output_dir(self) -> None:
        path = Path(self.output_dir.text().strip() or str(core.INSTALL_ROOT / "outputs"))
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def pick_output_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "自定义保存 SRT", self.output_path.text() or self.generated_output_path(), "SRT 字幕 (*.srt);;所有文件 (*.*)")
        if path:
            self.output_path_custom = True
            self.output_path.setText(path)
            self.output_dir.setText(str(Path(path).parent))

    def pick_model(self) -> None:
        self.open_model_dir()

    def open_model_dir(self) -> None:
        path = Path(core.default_model_root_dir())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def load_text_file(self, path: str) -> None:
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = Path(path).read_text(encoding="gbk", errors="replace")
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
            "output_path": self.output_path.text(),
            "output_path_custom": self.output_path_custom,
            "model_path": str(core.default_user_model_dir()),
            "mode": self.current_mode(),
            "device": self.device.currentText(),
            "compute_type": self.compute_type.currentText(),
            "language": self.language.currentData(),
            "ai_enabled": self.ai_enabled.isChecked(),
            "whisperx_enabled": self.whisperx_enabled.isChecked(),
            "diarization_enabled": self.diarization_enabled.isChecked(),
            "base_url": self.base_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "ai_model": self.ai_model.text().strip(),
            "hf_token": self.hf_token.text().strip(),
            "min_speakers": self.min_speakers.text().strip(),
            "max_speakers": self.max_speakers.text().strip(),
            "system_prompt": self.prompt_text.toPlainText().strip(),
            "script": self.script_text.toPlainText(),
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
        self.save_config()
        config = self.gather_config()
        self.set_busy(True)
        self.worker_thread = QThread()
        self.worker = JobWorker(config)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.set_progress)
        self.worker.finished.connect(self.on_job_finished)
        self.worker.failed.connect(self.on_job_failed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(lambda: self.set_busy(False))
        self.worker_thread.start()

    def on_job_finished(self, output: str) -> None:
        self.last_srt_path = output
        self.load_srt_to_speaker_table(output)
        QMessageBox.information(self, "完成", f"SRT 已保存:\n{output}")

    def on_job_failed(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setValue(0)
        self.progress.setVisible(busy)
        self.status_label.setText("处理中..." if busy else "就绪")
        self.start_btn.setEnabled(not busy)
        self.gpu_btn.setEnabled(not busy)
        self.repair_cuda_btn.setEnabled(not busy)
        self.model_check_btn.setEnabled(not busy)
        if hasattr(self, "download_pyannote_btn"):
            self.download_pyannote_btn.setEnabled(not busy)
            self.check_pyannote_btn.setEnabled(not busy)

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
        self.log_text.append(f"{time.strftime('%H:%M:%S')} {message}")

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
            self.ui_events.busy.emit(True)
            try:
                status = core.prepare_pyannote_model(hf_token, self.ui_events.log.emit)
                detail = "已缓存" if status.get("ready") else "未就绪"
                self.ui_events.pyannote_text.emit(f"{detail}: {status.get('label', '')}")
                self.ui_events.log.emit("说话人模型检查完成。")
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.pyannote_text.emit("未就绪。请确认 HF Token 正确，并已在授权页面同意许可。")
                self.ui_events.error.emit("说话人模型下载失败", str(exc))
            finally:
                self.ui_events.busy.emit(False)

        threading.Thread(target=task, daemon=True).start()

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

    def build_runtime_report(self) -> tuple[str, bool, dict]:
        core.add_cuda_dll_paths()
        import ctranslate2
        import torch

        plan = core.recommended_runtime_plan()
        nvidia = plan["nvidia"]
        downloads = plan.get("downloads", {})
        ct2_cuda = ctranslate2.get_cuda_device_count()
        torch_cuda = torch.cuda.is_available()
        wants_cuda = self.device.currentText().lower() == "cuda"

        if not wants_cuda:
            ok = True
            headline = "可以直接运行：当前设置为 CPU。"
        elif ct2_cuda > 0:
            ok = True
            headline = "可以直接运行：CUDA 已可用于 ASR。"
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
            f"PyTorch CUDA: {torch_cuda} ({torch.version.cuda})",
        ]
        if ok and wants_cuda and not torch_cuda:
            details.append("WhisperX/说话人识别可能还需要补齐 PyTorch CUDA 依赖。")
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

    def load_srt_to_speaker_table(self, path: str) -> None:
        self.last_srt_path = path
        items = self.parse_srt_file(path)
        self.speaker_table.setRowCount(0)
        speakers = set()
        for item in items:
            row = self.speaker_table.rowCount()
            self.speaker_table.insertRow(row)
            values = [item["index"], item["start"], item["end"], item["speaker"], item["text"]]
            for column, value in enumerate(values):
                self.speaker_table.setItem(row, column, QTableWidgetItem(str(value)))
            if item["speaker"]:
                speakers.add(item["speaker"])
        self.speaker_file_label.setText(str(Path(path).name))
        self.speaker_combo.clear()
        self.speaker_combo.addItems(sorted(speakers))
        self.speaker_table.resizeColumnsToContents()

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

    def save_edited_srt(self) -> None:
        if not self.last_srt_path:
            QMessageBox.warning(self, "没有 SRT", "当前没有可保存的 SRT。")
            return
        Path(self.last_srt_path).write_text(self.srt_text_from_items(self.speaker_table_items()), encoding="utf-8-sig")
        self.log(f"已保存编辑后的 SRT: {self.last_srt_path}")

    def export_selected_speaker(self, kind: str) -> None:
        speaker = self.speaker_combo.currentText().strip()
        if not speaker:
            QMessageBox.warning(self, "未选择说话人", "当前 SRT 没有可导出的说话人标签。")
            return
        base = Path(self.last_srt_path or self.output_path.text().strip() or "speaker.srt")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", speaker).strip("_") or "speaker"
        if kind == "srt":
            path = base.with_name(f"{base.stem}.{safe}.edited.srt")
            path.write_text(self.srt_text_from_items(self.speaker_table_items(), speaker), encoding="utf-8-sig")
        else:
            path = base.with_name(f"{base.stem}.{safe}.edited.txt")
            lines = [item["text"] for item in self.speaker_table_items() if item["speaker"] == speaker]
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8-sig")
        self.log(f"已导出 {speaker}: {path}")

    def check_runtime(self) -> None:
        def task() -> None:
            try:
                text, ok, downloads = self.build_runtime_report()
                self.cuda_links = downloads
                self.log(text)
                self.ui_events.runtime_text.emit(text)
                if ok:
                    self.ui_events.runtime_links.emit("", False)
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.error.emit("运行环境检查失败", str(exc))

        threading.Thread(target=task, daemon=True).start()

    def complete_runtime_missing(self) -> None:
        def task() -> None:
            self.ui_events.busy.emit(True)
            try:
                text, ok, downloads = self.build_runtime_report()
                self.cuda_links = downloads
                if ok and "PyTorch CUDA: True" in text:
                    self.ui_events.runtime_links.emit("", False)
                    self.ui_events.runtime_text.emit(text + "\n\n无需补齐。")
                    return

                if core.detect_nvidia_smi().get("available"):
                    self.ui_events.runtime_text.emit(text + "\n\n正在补齐 Python CUDA 依赖 ...")
                    core.install_recommended_torch(self.ui_events.log.emit)
                    text_after, ok_after, downloads_after = self.build_runtime_report()
                    self.cuda_links = downloads_after
                    if ok_after:
                        self.ui_events.runtime_links.emit("", False)
                        self.ui_events.runtime_text.emit(text_after + "\n\n补齐完成，请重启软件后再检测一次。")
                        return

                self.ui_events.runtime_text.emit(text + "\n\n需要先安装下面的 NVIDIA 组件，然后重启电脑/软件再检测。")
                self.ui_events.runtime_links.emit(self.runtime_link_html(downloads), True)
            except Exception as exc:
                self.ui_events.log.emit(traceback.format_exc())
                self.ui_events.error.emit("补齐运行环境失败", str(exc))
            finally:
                self.ui_events.busy.emit(False)

        threading.Thread(target=task, daemon=True).start()

    def repair_cuda_runtime(self) -> None:
        def task() -> None:
            self.set_busy(True)
            try:
                core.install_recommended_torch(self.log)
                self.log("CUDA 加速依赖安装完成。请重启软件后再检查运行环境。")
            except Exception as exc:
                self.log(traceback.format_exc())
                QMessageBox.critical(self, "CUDA 修复失败", str(exc))
            finally:
                self.set_busy(False)

        threading.Thread(target=task, daemon=True).start()

    def check_model_files(self) -> None:
        def task() -> None:
            self.ui_events.busy.emit(True)
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
            finally:
                self.ui_events.busy.emit(False)

        threading.Thread(target=task, daemon=True).start()


def apply_style(app: QApplication) -> None:
    app.setStyleSheet(
        """
        QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #e5e7eb; background: #111827; }
        QMainWindow, QTabWidget::pane { background: #111827; border: 0; }
        QLabel#Title { font-size: 24px; font-weight: 700; }
        QLabel#Muted { color: #9ca3af; }
        QLabel#Status { color: #93c5fd; padding: 4px 8px; }
        QLabel#Section { font-weight: 700; color: #f3f4f6; }
        QFrame#ModeBar, QGroupBox { border: 1px solid #273244; border-radius: 8px; background: #162033; }
        QGroupBox QLabel, QFrame QLabel { background: transparent; }
        QGroupBox { margin-top: 10px; padding: 12px 8px 8px 8px; font-weight: 700; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #cbd5e1; }
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {
            border: 1px solid #334155; border-radius: 6px; padding: 7px; background: #0f172a; color: #f8fafc;
        }
        QPlainTextEdit, QTextEdit { selection-background-color: #2563eb; }
        QPushButton {
            border: 1px solid #334155; border-radius: 6px; padding: 8px 12px; background: #1f2937; color: #f8fafc;
        }
        QPushButton:hover { background: #2b3648; }
        QPushButton:checked { background: #2563eb; border-color: #60a5fa; }
        QPushButton#Primary { background: #2563eb; border-color: #60a5fa; font-weight: 700; }
        QPushButton#Primary:hover { background: #1d4ed8; }
        QCheckBox { spacing: 8px; padding: 3px; }
        QTabBar::tab { background: #162033; padding: 9px 18px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
        QTabBar::tab:selected { background: #243247; color: #ffffff; }
        QSplitter::handle { background: #111827; width: 8px; }
        QProgressBar { border: 0; background: #1f2937; height: 3px; }
        QProgressBar::chunk { background: #60a5fa; }
        QScrollArea { background: #111827; border: 0; }
        QScrollBar:vertical {
            background: #0f172a; width: 12px; margin: 0; border-radius: 6px;
        }
        QScrollBar::handle:vertical {
            background: #475569; border-radius: 6px; min-height: 48px;
        }
        QScrollBar::handle:vertical:hover { background: #64748b; }
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
    apply_style(qt_app)
    window = MainWindow()
    window.show()
    sys.exit(qt_app.exec())
