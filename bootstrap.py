from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path


APP_NAME = "SRTMatcher"
DISPLAY_NAME = "字幕多功能工具"
APP_BUILD_ID = "2026-11-05-v0.2.0"
DEFAULT_APP_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
APP_HOME = DEFAULT_APP_HOME
APP_DIR = APP_HOME / "app"
TOOLS_DIR = APP_HOME / "tools"
VENV_DIR = APP_HOME / ".venv"
UV_EXE = TOOLS_DIR / "uv.exe"
READY_FILE = APP_HOME / ".runtime-ready"
REQUIREMENTS_FILE = APP_HOME / ".requirements-installed"
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
SOURCE_FILES = [
    "app.py",
    "qt_app.py",
    "task_runtime.py",
    "model_runtime.py",
    "ai_transport.py",
    "asr_runtime.py",
    "batch_runtime.py",
    "requirements.txt",
    "README.md",
    "FFMPEG-NOTICE.txt",
    "srtmatcher-logo.png",
    "srtmatcher.ico",
]
PROGRESS_UI = None
LOG_FILE: Path | None = None
INSTALLED_EXE_NAME = "SRTMatcher.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

STEP_PENDING = "pending"
STEP_ACTIVE = "active"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"

LAUNCHER_STEPS = [
    ("files", "更新程序文件"),
    ("check", "检查运行环境"),
    ("runtime", "准备 Python 运行环境"),
    ("deps", "安装运行依赖"),
    ("launch", "启动主程序"),
]
SETUP_STEPS = [
    ("files", "复制程序文件"),
    ("launcher", "安装启动器"),
    ("shortcut", "创建桌面快捷方式"),
    ("launch", "打开启动器"),
]

# 与主程序深色主题保持一致。
UI_COLORS = {
    "bg": "#0a0f16",
    "surface": "#101720",
    "line": "#1e2b3a",
    "title": "#f8fbff",
    "text": "#dce6f3",
    "muted": "#8fa0b5",
    "faint": "#526176",
    "accent": "#5a7cf0",
    "accent_end": "#2677e8",
    "accent_light": "#69a3ff",
    "ok": "#3ecf8e",
    "error": "#ff7b86",
    "error_bg": "#321a21",
}
UI_FONT = "Microsoft YaHei UI"


def enable_dpi_awareness() -> None:
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class ProgressUI:
    """Launcher progress window.

    Tk runs only on the main thread; the worker thread talks to it through
    ``post()`` so the window keeps repainting while files are copied, the
    runtime is checked or dependencies are installed.
    """

    def __init__(self, title: str, subtitle: str, steps: list[tuple[str, str]]) -> None:
        import queue
        import tkinter as tk

        enable_dpi_awareness()
        self.tk = tk
        self.events: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        self.closable = False
        self.finished = False
        self.failed = False
        self.target = 0.0
        self.shown = 0.0
        self.creep = 0.0
        self.angle = 0
        self.log_visible = False
        self.log_lines: list[str] = []
        self.step_state = {key: STEP_PENDING for key, _label in steps}
        self.step_order = [key for key, _label in steps]
        c = UI_COLORS

        root = self.root
        root.title(DISPLAY_NAME)
        root.configure(bg=c["bg"])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        try:
            icon_path = source_root() / "srtmatcher.ico"
            if icon_path.exists():
                root.iconbitmap(default=str(icon_path))
        except Exception:
            pass

        body = tk.Frame(root, bg=c["bg"], padx=self.px(30), pady=self.px(26))
        body.pack(fill="both", expand=True)

        header = tk.Frame(body, bg=c["bg"])
        header.pack(fill="x")
        self.logo_image = self._load_logo(self.px(52))
        if self.logo_image is not None:
            tk.Label(header, image=self.logo_image, bg=c["bg"], bd=0).pack(side="left", padx=(0, self.px(16)))
        heading = tk.Frame(header, bg=c["bg"])
        heading.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(
            heading, text=title, bg=c["bg"], fg=c["title"],
            font=(UI_FONT, 15, "bold"), anchor="w",
        )
        self.title_label.pack(fill="x")
        self.subtitle_label = tk.Label(
            heading, text=subtitle, bg=c["bg"], fg=c["muted"], font=(UI_FONT, 9),
            anchor="w", justify="left", wraplength=self.px(390),
        )
        self.subtitle_label.pack(fill="x", pady=(self.px(4), 0))

        card = tk.Frame(
            body, bg=c["surface"], highlightthickness=1, highlightbackground=c["line"],
            padx=self.px(18), pady=self.px(12),
        )
        card.pack(fill="x", pady=(self.px(22), 0))
        self.step_rows: dict[str, tuple] = {}
        glyph = self.px(18)
        for key, label in steps:
            row = tk.Frame(card, bg=c["surface"])
            row.pack(fill="x", pady=self.px(5))
            canvas = tk.Canvas(row, width=glyph, height=glyph, bg=c["surface"], highlightthickness=0, bd=0)
            canvas.pack(side="left")
            name = tk.Label(row, text=label, bg=c["surface"], fg=c["faint"], font=(UI_FONT, 10), anchor="w")
            name.pack(side="left", padx=(self.px(12), 0))
            detail = tk.Label(row, text="", bg=c["surface"], fg=c["faint"], font=(UI_FONT, 9), anchor="e")
            detail.pack(side="right")
            self.step_rows[key] = (canvas, name, detail)

        self.bar_height = self.px(6)
        self.bar = tk.Canvas(body, height=self.bar_height, bg=c["bg"], highlightthickness=0, bd=0)
        self.bar.pack(fill="x", pady=(self.px(20), 0))
        self.bar_width = self.px(460)
        self.bar.bind("<Configure>", lambda event: setattr(self, "bar_width", max(10, event.width)))

        status_row = tk.Frame(body, bg=c["bg"])
        status_row.pack(fill="x", pady=(self.px(10), 0))
        self.status_label = tk.Label(status_row, text="准备中 ...", bg=c["bg"], fg=c["muted"], font=(UI_FONT, 9), anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.percent_label = tk.Label(status_row, text="0%", bg=c["bg"], fg=c["faint"], font=(UI_FONT, 9))
        self.percent_label.pack(side="right")

        footer = tk.Frame(body, bg=c["bg"])
        footer.pack(fill="x", pady=(self.px(14), 0))
        self.toggle_label = tk.Label(
            footer, text="显示详细日志  ▾", bg=c["bg"], fg=c["accent_light"], font=(UI_FONT, 9), cursor="hand2",
        )
        self.toggle_label.pack(side="left")
        self.toggle_label.bind("<Button-1>", lambda _event: self.toggle_log())
        self.buttons = tk.Frame(footer, bg=c["bg"])
        self._make_button(self.buttons, "关闭", self._close, primary=True).pack(side="right")
        self._make_button(self.buttons, "复制日志", self.copy_log).pack(side="right", padx=(0, self.px(8)))

        self.log_frame = tk.Frame(body, bg=c["bg"])
        self.log_text = tk.Text(
            self.log_frame, height=10, wrap="word", bg=c["surface"], fg=c["muted"], bd=0,
            highlightthickness=1, highlightbackground=c["line"], insertbackground=c["text"],
            font=("Consolas", 9), padx=self.px(10), pady=self.px(8), state="disabled",
        )
        scrollbar = tk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for key in self.step_order:
            self._render_step(key)
        self._draw_bar()
        root.update_idletasks()
        width = self.px(540)
        height = root.winfo_reqheight()
        x = (root.winfo_screenwidth() - width) // 2
        y = (root.winfo_screenheight() - height) // 3
        root.geometry(f"{width}x{height}+{x}+{max(0, y)}")
        self._use_dark_title_bar()
        root.deiconify()
        root.after(16, self._tick)

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _use_dark_title_bar(self) -> None:
        try:
            import ctypes

            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新/旧版 Windows）
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _load_logo(self, size: int):
        path = source_root() / "srtmatcher-logo-64.png"
        if not path.exists():
            path = source_root() / "srtmatcher-logo.png"
        if not path.exists():
            return None
        try:
            image = self.tk.PhotoImage(file=str(path))
            factor = max(1, round(image.width() / size))
            return image.subsample(factor) if factor > 1 else image
        except Exception:
            return None

    def _make_button(self, parent, text: str, command, primary: bool = False):
        c = UI_COLORS
        bg = c["accent"] if primary else c["surface"]
        return self.tk.Button(
            parent, text=text, command=command, bg=bg, fg=c["title"] if primary else c["text"],
            activebackground=c["accent_end"] if primary else c["line"], activeforeground=c["title"],
            relief="flat", bd=0, padx=self.px(16), pady=self.px(5), font=(UI_FONT, 9), cursor="hand2",
        )

    # ---- thread-safe API -------------------------------------------------
    def post(self, *event) -> None:
        self.events.put(event)

    # ---- main-thread only ------------------------------------------------
    def mainloop(self) -> None:
        self.root.mainloop()

    def _tick(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except Exception:
            pass
        if not self.finished:
            # 长时间没有新进度时让进度条缓慢前移，表明程序仍在工作。
            self.creep = min(self.creep + 0.015, 6.0)
        goal = min(99.0 if not self.finished else 100.0, self.target + (0 if self.finished else self.creep))
        self.shown += (goal - self.shown) * 0.12
        self.angle = (self.angle + 9) % 360
        for key, state in self.step_state.items():
            if state == STEP_ACTIVE:
                self._render_step(key)
        self._draw_bar()
        try:
            self.root.after(16, self._tick)
        except Exception:
            pass

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._append_log(event[1])
        elif kind == "progress":
            value, message = event[1], event[2]
            if value > self.target:
                self.target = max(0.0, min(100.0, float(value)))
                self.creep = 0.0
            if message:
                self.status_label.configure(text=message, fg=UI_COLORS["muted"])
        elif kind == "step":
            key, state, detail = event[1], event[2], event[3]
            if key in self.step_state:
                self.step_state[key] = state
                self.step_rows[key][2].configure(text=detail or "")
                self._render_step(key)
        elif kind == "finish":
            self._finish(event[1], event[2], event[3])

    def _finish(self, ok: bool, message: str, hint: str) -> None:
        c = UI_COLORS
        self.finished = True
        self.closable = True
        if ok:
            self.target = 100.0
            self.title_label.configure(text="准备就绪")
            self.status_label.configure(text=message or "正在打开主程序 ...", fg=c["ok"])
            self.root.after(900, self._close)
            return
        self.failed = True
        for key, state in self.step_state.items():
            if state == STEP_ACTIVE:
                self.step_state[key] = STEP_FAILED
                self._render_step(key)
        self.title_label.configure(text="启动失败", fg=c["error"])
        self.subtitle_label.configure(text=message)
        self.status_label.configure(text=hint, fg=c["error"])
        self.buttons.pack(side="right")
        if not self.log_visible:
            self.toggle_log()

    def _render_step(self, key: str) -> None:
        c = UI_COLORS
        canvas, name, detail = self.step_rows[key]
        state = self.step_state[key]
        size = int(canvas.cget("width"))
        pad = max(2, size // 9)
        canvas.delete("all")
        if state == STEP_DONE:
            canvas.create_oval(pad, pad, size - pad, size - pad, fill=c["ok"], outline="")
            canvas.create_line(
                size * 0.30, size * 0.52, size * 0.45, size * 0.66, size * 0.71, size * 0.36,
                fill=c["surface"], width=max(2, size // 9), capstyle="round", joinstyle="round",
            )
            name.configure(fg=c["text"])
            detail.configure(fg=c["muted"])
        elif state == STEP_ACTIVE:
            canvas.create_oval(pad, pad, size - pad, size - pad, outline=c["line"], width=max(2, size // 9))
            canvas.create_arc(
                pad, pad, size - pad, size - pad, start=-self.angle, extent=110,
                style="arc", outline=c["accent_light"], width=max(2, size // 9),
            )
            name.configure(fg=c["title"])
            detail.configure(fg=c["accent_light"])
        elif state == STEP_FAILED:
            canvas.create_oval(pad, pad, size - pad, size - pad, fill=c["error"], outline="")
            inset = size * 0.34
            line_width = max(2, size // 9)
            canvas.create_line(inset, inset, size - inset, size - inset, fill=c["surface"], width=line_width, capstyle="round")
            canvas.create_line(size - inset, inset, inset, size - inset, fill=c["surface"], width=line_width, capstyle="round")
            name.configure(fg=c["error"])
            detail.configure(fg=c["error"])
        elif state == STEP_SKIPPED:
            canvas.create_oval(pad, pad, size - pad, size - pad, outline=c["line"], width=max(1, size // 12))
            canvas.create_line(size * 0.36, size / 2, size * 0.64, size / 2, fill=c["faint"], width=max(1, size // 10))
            name.configure(fg=c["faint"])
            detail.configure(fg=c["faint"])
        else:
            canvas.create_oval(pad, pad, size - pad, size - pad, outline=c["line"], width=max(1, size // 12))
            name.configure(fg=c["faint"])
            detail.configure(fg=c["faint"])

    def _draw_bar(self) -> None:
        c = UI_COLORS
        bar = self.bar
        bar.delete("all")
        h = self.bar_height
        w = self.bar_width
        r = h / 2
        bar.create_line(r, r, w - r, r, fill=c["line"], width=h, capstyle="round")
        fraction = max(0.0, min(1.0, self.shown / 100.0))
        if fraction > 0.004:
            end = r + (w - h) * fraction
            color = c["error"] if self.failed else (c["ok"] if self.finished and fraction > 0.995 else c["accent"])
            bar.create_line(r, r, end, r, fill=color, width=h, capstyle="round")
        self.percent_label.configure(text=f"{int(round(self.shown))}%")

    def _append_log(self, message: str) -> None:
        self.log_lines.append(message)
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="both", expand=True, pady=(self.px(12), 0))
            self.toggle_label.configure(text="隐藏详细日志  ▴")
        else:
            self.log_frame.pack_forget()
            self.toggle_label.configure(text="显示详细日志  ▾")
        self.root.update_idletasks()
        width = self.root.winfo_width()
        self.root.geometry(f"{width}x{self.root.winfo_reqheight()}")

    def copy_log(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(self.log_lines))
            self.status_label.configure(text="日志已复制到剪贴板", fg=UI_COLORS["muted"])
        except Exception:
            pass

    def _on_close_request(self) -> None:
        if self.closable:
            self._close()

    def _close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass
    if LOG_FILE is not None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except Exception:
            pass
    if PROGRESS_UI is not None:
        PROGRESS_UI.post("log", message)


def show_progress(value: float, message: str) -> None:
    if PROGRESS_UI is not None:
        PROGRESS_UI.post("progress", value, message)


def set_step(key: str, state: str, detail: str = "") -> None:
    if PROGRESS_UI is not None:
        PROGRESS_UI.post("step", key, state, detail)


def finish_ui(ok: bool, message: str = "", hint: str = "") -> None:
    if PROGRESS_UI is not None:
        PROGRESS_UI.post("finish", ok, message, hint)


def start_log_file() -> None:
    """Keep a copy of the launcher log next to the install, since the exe has no console."""
    global LOG_FILE
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        LOG_FILE = APP_HOME / "launcher.log"
        LOG_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + f" {APP_BUILD_ID}\n", encoding="utf-8")
    except Exception:
        LOG_FILE = None


def run_with_progress_ui(title: str, subtitle: str, steps: list[tuple[str, str]], job) -> int:
    """Run ``job`` on a worker thread while the Tk window stays responsive."""
    global PROGRESS_UI
    import threading

    try:
        ui = ProgressUI(title, subtitle, steps)
    except Exception:
        return job()

    PROGRESS_UI = ui
    result = {"code": 1}

    def worker() -> None:
        try:
            result["code"] = job()
        except BaseException as exc:  # job() reports its own errors; this is a last resort
            log(traceback.format_exc())
            finish_ui(False, str(exc), "请复制日志后反馈。")

    thread = threading.Thread(target=worker, name="launcher-worker", daemon=True)
    thread.start()
    try:
        ui.mainloop()
    finally:
        PROGRESS_UI = None
    thread.join(timeout=2)
    return result["code"]


def set_install_home(path: Path) -> None:
    global APP_HOME, APP_DIR, TOOLS_DIR, VENV_DIR, UV_EXE, READY_FILE, REQUIREMENTS_FILE

    APP_HOME = path.expanduser().resolve()
    APP_DIR = APP_HOME / "app"
    TOOLS_DIR = APP_HOME / "tools"
    VENV_DIR = APP_HOME / ".venv"
    UV_EXE = TOOLS_DIR / "uv.exe"
    READY_FILE = APP_HOME / ".runtime-ready"
    REQUIREMENTS_FILE = APP_HOME / ".requirements-installed"


def arg_value(name: str) -> str | None:
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def is_installed_launcher_mode() -> bool:
    exe_name = Path(sys.executable).name.lower()
    return "--launcher" in sys.argv or exe_name == INSTALLED_EXE_NAME.lower()


def choose_install_dir() -> Path:
    if "--install-dir" in sys.argv:
        index = sys.argv.index("--install-dir")
        if index + 1 >= len(sys.argv):
            raise RuntimeError("--install-dir 后面需要跟安装目录。")
        return Path(sys.argv[index + 1])

    env_dir = os.environ.get("SRTMATCHER_INSTALL_DIR")
    if env_dir:
        return Path(env_dir)

    default = DEFAULT_APP_HOME
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "SRTMatcher 安装",
            "请选择 SRTMatcher 的安装目录。\n\n"
            f"默认建议目录：{default}\n\n"
            "可以选择 D 盘或任意你有写入权限的目录。",
        )
        selected = filedialog.askdirectory(
            title="选择 SRTMatcher 安装目录",
            initialdir=str(default.parent if default.parent.exists() else Path.home()),
            mustexist=False,
        )
        root.destroy()
        if selected:
            selected_path = Path(selected)
            if selected_path.name.lower() != APP_NAME.lower():
                selected_path = selected_path / APP_NAME
            return selected_path
    except Exception:
        pass

    log(f"默认安装目录: {default}")
    typed = input("请输入安装目录，直接回车使用默认目录: ").strip().strip('"')
    return Path(typed) if typed else default


def source_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def source_bundle_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        if not path.exists():
            digest.update(b"\0missing\0")
            continue
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_build_id() -> str:
    return f"{APP_BUILD_ID}:{source_bundle_hash(source_root())}"


def run(args: list[str], cwd: Path | None = None, progress_value: float | None = None, status: str | None = None) -> None:
    if status is not None:
        show_progress(progress_value if progress_value is not None else 0, status)
    log("> " + " ".join(str(arg) for arg in args))
    import queue

    proc = subprocess.Popen(
        [str(arg) for arg in args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    import threading

    threading.Thread(target=reader, daemon=True).start()
    reader_done = False
    while not reader_done or proc.poll() is None:
        try:
            item = lines.get(timeout=0.1)
            if item is None:
                reader_done = True
            elif item.strip():
                log(item.rstrip())
        except queue.Empty:
            pass
    code = proc.wait()
    while not lines.empty():
        item = lines.get_nowait()
        if item and item.strip():
            log(item.rstrip())
    if code != 0:
        raise RuntimeError(f"命令失败，退出码 {code}: {' '.join(str(arg) for arg in args)}")


def retry_file_action(action, description: str, attempts: int = 8, delay: float = 0.7):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except OSError as exc:
            last_error = exc
            if getattr(exc, "winerror", None) != 32:
                raise
            log(f"{description} 被系统临时占用，等待后重试 {attempt}/{attempts} ...")
            time.sleep(delay)
    raise RuntimeError(f"{description} 多次重试失败: {last_error}")


def install_sources() -> None:
    show_progress(8, "复制程序文件 ...")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root = source_root()
    for name in SOURCE_FILES:
        source = root / name
        if source.exists():
            target = APP_DIR / name
            temp_target = target.with_name(f".{target.name}.upgrade.tmp")
            retry_file_action(lambda: shutil.copy2(source, temp_target), f"复制 {name}")
            retry_file_action(lambda: os.replace(temp_target, target), f"更新 {name}")


def ensure_uv() -> None:
    if UV_EXE.exists():
        show_progress(20, "uv 已就绪")
        return
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="uv-download-", dir=str(TOOLS_DIR)))
    archive = temp_root / "uv.zip"
    extract_dir = temp_root / "extract"
    log("下载 uv 运行环境管理器 ...")

    try:
        # urlretrieve 没有超时，网络卡住会让启动器永远停在这里。
        with urllib.request.urlopen(UV_URL, timeout=60) as response, archive.open("wb") as handle:
            total_size = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if total_size > 0:
                    percent = min(1.0, done / total_size)
                    show_progress(20 + percent * 5, f"下载 uv: {int(percent * 100)}%")
        show_progress(25, "解压 uv ...")
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)

        candidates = [path for path in extract_dir.rglob("uv.exe") if path.is_file()]
        if not candidates:
            raise RuntimeError("uv.exe 下载包里没有找到 uv.exe。")

        temp_uv = TOOLS_DIR / "uv.exe.tmp"
        retry_file_action(lambda: shutil.copy2(candidates[0], temp_uv), "复制 uv.exe")
        retry_file_action(lambda: os.replace(temp_uv, UV_EXE), "写入 uv.exe")
    finally:
        try:
            retry_file_action(lambda: shutil.rmtree(temp_root, ignore_errors=False), "清理 uv 临时目录", attempts=3)
        except Exception as exc:
            log(f"uv 临时目录暂时无法清理，可忽略: {exc}")

    if not UV_EXE.exists():
        raise RuntimeError("uv.exe 下载失败。")


def venv_python_works() -> bool:
    python = VENV_DIR / "Scripts" / "python.exe"
    if not python.exists():
        return False
    try:
        check = subprocess.run(
            [str(python), "-c", "import sys"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            timeout=60,
        )
    except Exception:
        return False
    return check.returncode == 0


def ensure_python_runtime() -> None:
    ensure_uv()
    run([UV_EXE, "python", "install", "3.12"], progress_value=30, status="安装 Python 3.12 运行环境 ...")
    if VENV_DIR.exists() and not venv_python_works():
        # 例如 uv 管理的基础 Python 被清理后，.venv 里的 python.exe 还在但已无法运行。
        log("现有虚拟环境无法运行，删除后重建 ...")
        retry_file_action(lambda: shutil.rmtree(VENV_DIR), "删除损坏的虚拟环境")
    if not (VENV_DIR / "Scripts" / "python.exe").exists():
        run([UV_EXE, "venv", "--seed", "--python", "3.12", VENV_DIR], progress_value=40, status="创建独立虚拟环境 ...")
    show_progress(45, "Python 运行环境已就绪")


def requirements_hash() -> str:
    path = APP_DIR / "requirements.txt"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def requirements_installed() -> bool:
    """True when the current requirements.txt was installed successfully before."""
    expected = requirements_hash()
    try:
        return bool(expected) and REQUIREMENTS_FILE.read_text(encoding="utf-8").strip() == expected
    except OSError:
        return False


def install_python_dependencies() -> None:
    python = VENV_DIR / "Scripts" / "python.exe"
    run(
        [UV_EXE, "pip", "install", "--python", python, "-r", APP_DIR / "requirements.txt"],
        progress_value=55,
        status="安装运行依赖 ...",
    )
    REQUIREMENTS_FILE.write_text(requirements_hash() + "\n", encoding="utf-8")
    show_progress(88, "运行依赖安装完成")


def main_app_ready() -> bool:
    python = VENV_DIR / "Scripts" / "python.exe"
    if not python.exists():
        return False
    local_modules = ", ".join(Path(name).stem for name in SOURCE_FILES if name.endswith(".py"))
    try:
        check = subprocess.run(
            [
                str(python),
                "-c",
                f"import PySide6, faster_whisper, huggingface_hub, httpx; import {local_modules}",
            ],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=NO_WINDOW,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False
    return check.returncode == 0


def runtime_marker_ready() -> bool:
    if not (
        READY_FILE.exists()
        and (VENV_DIR / "Scripts" / "pythonw.exe").exists()
        and (APP_DIR / "qt_app.py").exists()
        and (APP_DIR / "app.py").exists()
    ):
        return False
    try:
        marker = READY_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception:
        return False
    expected_id = runtime_build_id()
    if marker != expected_id:
        return False
    return source_bundle_hash(APP_DIR) == source_bundle_hash(source_root())


def write_runtime_marker() -> None:
    READY_FILE.write_text(f"{runtime_build_id()}\n{int(time.time())}\n", encoding="utf-8")


def install_self_launcher() -> None:
    show_progress(55, "安装启动器 ...")
    if not getattr(sys, "frozen", False):
        return
    target = APP_HOME / INSTALLED_EXE_NAME
    source = Path(sys.executable)
    if source.resolve() == target.resolve():
        return
    temp_target = target.with_suffix(".exe.tmp")
    retry_file_action(lambda: shutil.copy2(source, temp_target), "复制启动器")
    retry_file_action(lambda: os.replace(temp_target, target), "写入启动器")


def write_launchers() -> None:
    show_progress(75, "写入启动脚本 ...")
    launcher = APP_HOME / "SRTMatcher.bat"
    installed_exe = APP_HOME / INSTALLED_EXE_NAME
    if installed_exe.exists():
        launcher.write_text(
            f'@echo off\r\nstart "" "{installed_exe}" --launcher --app-home "{APP_HOME}"\r\n',
            encoding="utf-8",
        )
    else:
        pythonw = VENV_DIR / "Scripts" / "pythonw.exe"
        launcher.write_text(
            f'@echo off\r\ncd /d "{APP_DIR}"\r\n"{pythonw}" "{APP_DIR / "qt_app.py"}"\r\n',
            encoding="utf-8",
        )


def ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_desktop_shortcut() -> None:
    installed_exe = APP_HOME / INSTALLED_EXE_NAME
    if not installed_exe.exists():
        return
    icon_path = APP_HOME / "srtmatcher.ico"
    if not icon_path.exists():
        bundled_icon = source_root() / "srtmatcher.ico"
        if bundled_icon.exists():
            retry_file_action(lambda: shutil.copy2(bundled_icon, icon_path), "写入快捷方式图标")
    if not icon_path.exists():
        icon_path = APP_DIR / "srtmatcher.ico"
    if not icon_path.exists():
        icon_path = installed_exe
    show_progress(85, "创建桌面快捷方式 ...")
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        f"$link=Join-Path $desktop '{DISPLAY_NAME}.lnk';"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut($link);"
        f"$shortcut.TargetPath={ps_single_quote(str(installed_exe))};"
        f"$shortcut.WorkingDirectory={ps_single_quote(str(APP_HOME))};"
        "$shortcut.Arguments='--launcher';"
        f"$shortcut.IconLocation={ps_single_quote(str(icon_path))};"
        "$shortcut.Save();"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:
        log(f"桌面快捷方式创建失败，可忽略: {exc}")


APP_STDERR_NAME = "app-stderr.log"
APP_START_GRACE_SECONDS = 4.0


def launch_app(show_status: bool = True) -> None:
    """Start the GUI and fail loudly if it exits right away (import errors, DLL errors ...)."""
    if show_status:
        show_progress(96, "正在启动主程序 ...")
    pythonw = VENV_DIR / "Scripts" / "pythonw.exe"
    stderr_path = APP_HOME / APP_STDERR_NAME
    try:
        stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
    except OSError:
        stderr_handle = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [str(pythonw), str(APP_DIR / "qt_app.py")],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            creationflags=NO_WINDOW,
        )
    finally:
        if stderr_handle is not subprocess.DEVNULL:
            stderr_handle.close()
    try:
        code = proc.wait(timeout=APP_START_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return
    if code == 0:
        return
    try:
        detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        detail = ""
    tail = "\n".join(detail.splitlines()[-15:])
    if tail:
        log(tail)
    # 让下次启动重新走完整检查，而不是每次都静默失败。
    try:
        READY_FILE.unlink()
    except OSError:
        pass
    last_line = detail.splitlines()[-1] if detail else f"退出码 {code}"
    raise RuntimeError(f"主程序启动后立即退出：{last_line}")


def launch_installed_launcher() -> None:
    installed_exe = APP_HOME / INSTALLED_EXE_NAME
    if installed_exe.exists():
        subprocess.Popen([str(installed_exe), "--launcher", "--app-home", str(APP_HOME)], cwd=str(APP_HOME), creationflags=NO_WINDOW)
    else:
        launch_app()


_INSTANCE_MUTEX = None


def acquire_single_instance(name: str) -> bool:
    global _INSTANCE_MUTEX
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
    except Exception:
        pass
    return True


def show_error_box(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def report_failure(exc: BaseException, current_step: str | None, hint: str) -> int:
    log("")
    log(traceback.format_exc())
    log(f"失败: {exc}")
    if current_step:
        set_step(current_step, STEP_FAILED)
    finish_ui(False, str(exc), hint)
    if PROGRESS_UI is None:
        # 窗口版 exe 没有控制台，界面也起不来时只能弹系统对话框。
        where = f"\n\n详细日志: {LOG_FILE}" if LOG_FILE else ""
        show_error_box(DISPLAY_NAME, f"{exc}\n\n{hint}{where}")
    return 1


def setup_job() -> int:
    step: str | None = None
    try:
        start_log_file()
        log(f"安装目录: {APP_HOME}")
        step = "files"
        set_step(step, STEP_ACTIVE)
        install_sources()
        set_step(step, STEP_DONE)
        step = "launcher"
        set_step(step, STEP_ACTIVE)
        install_self_launcher()
        write_launchers()
        set_step(step, STEP_DONE)
        step = "shortcut"
        set_step(step, STEP_ACTIVE)
        create_desktop_shortcut()
        set_step(step, STEP_DONE)
        step = "launch"
        set_step(step, STEP_ACTIVE)
        show_progress(100, "安装完成，正在打开启动器 ...")
        log("安装完成。正在打开启动器 ...")
        launch_installed_launcher()
        set_step(step, STEP_DONE)
        finish_ui(True, "安装完成，正在打开启动器 ...")
        return 0
    except Exception as exc:
        return report_failure(exc, step, "请确认安装目录有写入权限。")


def setup_main() -> int:
    try:
        set_install_home(choose_install_dir())
    except Exception as exc:
        return report_failure(exc, None, "请确认安装目录有写入权限。")
    return run_with_progress_ui(
        f"正在安装{DISPLAY_NAME}",
        f"安装到 {APP_HOME}",
        SETUP_STEPS,
        setup_job,
    )


def launcher_job() -> int:
    step: str | None = None
    try:
        start_log_file()
        log(f"软件目录: {APP_HOME}")
        step = "files"
        set_step(step, STEP_ACTIVE)
        install_sources()
        set_step(step, STEP_DONE)

        step = "check"
        set_step(step, STEP_ACTIVE, "需要几秒钟")
        show_progress(15, "检查运行环境 ...")
        importable = main_app_ready()
        if importable and requirements_installed():
            log("基础运行环境已就绪。")
            set_step(step, STEP_DONE, "已就绪")
            set_step("runtime", STEP_SKIPPED, "无需安装")
            set_step("deps", STEP_SKIPPED, "无需安装")
        else:
            if importable:
                set_step(step, STEP_DONE, "依赖清单有更新")
                log("依赖清单与上次安装不同，补装依赖（已满足的包会直接跳过）。")
            else:
                set_step(step, STEP_DONE, "需要安装")
                log("需要准备基础运行环境。CUDA 加速和模型下载会在主程序内按按钮处理。")
            step = "runtime"
            set_step(step, STEP_ACTIVE)
            ensure_python_runtime()
            set_step(step, STEP_DONE)
            step = "deps"
            set_step(step, STEP_ACTIVE, "可能需要几分钟")
            install_python_dependencies()
            if not main_app_ready():
                raise RuntimeError("依赖安装完成，但主程序模块仍无法导入。")
            set_step(step, STEP_DONE)
        write_runtime_marker()

        step = "launch"
        set_step(step, STEP_ACTIVE)
        log("正在启动主程序 ...")
        launch_app()
        set_step(step, STEP_DONE)
        finish_ui(True, "正在打开主程序 ...")
        return 0
    except Exception as exc:
        return report_failure(exc, step, "请确认网络可用、安装目录有写入权限，可点“复制日志”反馈。")


def launcher_main() -> int:
    home_arg = arg_value("--app-home")
    if home_arg:
        set_install_home(Path(home_arg))
    else:
        set_install_home(Path(sys.executable).resolve().parent)

    try:
        if runtime_marker_ready():
            launch_app(show_status=False)
            return 0
    except Exception:
        pass  # 走下面带界面的完整检查流程，失败原因会显示在窗口里。

    if not acquire_single_instance("SRTMatcherLauncherSetup"):
        return 0  # 另一个启动器已经在准备/升级，避免并发复制文件和 pip。

    upgrading = (APP_DIR / "app.py").exists()
    if upgrading:
        title = f"正在更新{DISPLAY_NAME}"
        subtitle = "检测到新版本，正在替换程序文件。设置、模型和运行环境都会保留。"
    else:
        title = f"正在准备{DISPLAY_NAME}"
        subtitle = "首次启动需要准备运行环境，CUDA 加速和模型可稍后在软件设置页补齐。"
    return run_with_progress_ui(title, subtitle, LAUNCHER_STEPS, launcher_job)


def main() -> int:
    if is_installed_launcher_mode():
        return launcher_main()
    return setup_main()


if __name__ == "__main__":
    raise SystemExit(main())
