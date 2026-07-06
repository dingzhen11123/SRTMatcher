from __future__ import annotations

import os
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
APP_BUILD_ID = "2026-07-06-whisperx-window-upgrade"
DEFAULT_APP_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
APP_HOME = DEFAULT_APP_HOME
APP_DIR = APP_HOME / "app"
TOOLS_DIR = APP_HOME / "tools"
VENV_DIR = APP_HOME / ".venv"
UV_EXE = TOOLS_DIR / "uv.exe"
READY_FILE = APP_HOME / ".runtime-ready"
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
SOURCE_FILES = ["app.py", "qt_app.py", "requirements.txt", "README.md"]
PROGRESS_UI = None
INSTALLED_EXE_NAME = "SRTMatcher.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


class ProgressUI:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("SRTMatcher 安装")
        self.root.geometry("640x420")
        self.root.minsize(560, 360)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        self.status = tk.StringVar(value="准备安装 ...")
        self.percent = tk.DoubleVar(value=0)

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        title = ttk.Label(frame, text="正在安装 SRTMatcher", font=("Microsoft YaHei UI", 13, "bold"))
        title.pack(anchor="w")

        ttk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(12, 6))
        ttk.Progressbar(frame, maximum=100, variable=self.percent).pack(fill="x")

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, pady=(14, 0))
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.root.update()

    def pump(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

    def set_progress(self, value: float, message: str) -> None:
        self.percent.set(max(0, min(100, value)))
        self.status.set(message)
        self.pump()

    def write_line(self, message: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            self.pump()
        except Exception:
            pass

    def close_soon(self) -> None:
        try:
            self.root.after(1200, self.root.destroy)
            self.pump()
        except Exception:
            pass

    def wait_until_closed(self) -> None:
        try:
            self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
            self.root.mainloop()
        except Exception:
            pass


def log(message: str) -> None:
    print(message, flush=True)
    if PROGRESS_UI is not None:
        PROGRESS_UI.write_line(message)


def show_progress(value: float, message: str) -> None:
    if PROGRESS_UI is not None:
        PROGRESS_UI.set_progress(value, message)


def init_progress_ui() -> None:
    global PROGRESS_UI
    try:
        PROGRESS_UI = ProgressUI()
    except Exception:
        PROGRESS_UI = None


def wait_before_exit() -> None:
    if PROGRESS_UI is not None:
        PROGRESS_UI.wait_until_closed()
        return
    try:
        input("按回车退出...")
    except Exception:
        pass


def set_install_home(path: Path) -> None:
    global APP_HOME, APP_DIR, TOOLS_DIR, VENV_DIR, UV_EXE, READY_FILE

    APP_HOME = path.expanduser().resolve()
    APP_DIR = APP_HOME / "app"
    TOOLS_DIR = APP_HOME / "tools"
    VENV_DIR = APP_HOME / ".venv"
    UV_EXE = TOOLS_DIR / "uv.exe"
    READY_FILE = APP_HOME / ".runtime-ready"


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
            if PROGRESS_UI is not None:
                PROGRESS_UI.pump()
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
            shutil.copy2(source, APP_DIR / name)


def ensure_uv() -> None:
    if UV_EXE.exists():
        show_progress(20, "uv 已就绪")
        return
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="uv-download-", dir=str(TOOLS_DIR)))
    archive = temp_root / "uv.zip"
    extract_dir = temp_root / "extract"
    log("下载 uv 运行环境管理器 ...")

    def report(block_count: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            done = min(block_count * block_size, total_size)
            percent = done / total_size
            show_progress(12 + percent * 12, f"下载 uv: {int(percent * 100)}%")

    try:
        urllib.request.urlretrieve(UV_URL, archive, reporthook=report)
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


def ensure_python_runtime() -> None:
    ensure_uv()
    run([UV_EXE, "python", "install", "3.12"], progress_value=30, status="安装 Python 3.12 运行环境 ...")
    if not (VENV_DIR / "Scripts" / "python.exe").exists():
        run([UV_EXE, "venv", "--seed", "--python", "3.12", VENV_DIR], progress_value=40, status="创建独立虚拟环境 ...")
    show_progress(45, "Python 运行环境已就绪")


def install_python_dependencies() -> None:
    python = VENV_DIR / "Scripts" / "python.exe"
    run(
        [UV_EXE, "pip", "install", "--python", python, "-r", APP_DIR / "requirements.txt"],
        progress_value=55,
        status="安装基础运行依赖 ...",
    )
    show_progress(88, "基础依赖安装完成")


def main_app_ready() -> bool:
    python = VENV_DIR / "Scripts" / "python.exe"
    if not python.exists():
        return False
    check = subprocess.run(
        [str(python), "-c", "import PySide6, faster_whisper, huggingface_hub"],
        cwd=str(APP_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=NO_WINDOW,
    )
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
    return marker == APP_BUILD_ID


def write_runtime_marker() -> None:
    READY_FILE.write_text(f"{APP_BUILD_ID}\n{int(time.time())}\n", encoding="utf-8")


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
    show_progress(85, "创建桌面快捷方式 ...")
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        "$link=Join-Path $desktop 'SRTMatcher.lnk';"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut($link);"
        f"$shortcut.TargetPath={ps_single_quote(str(installed_exe))};"
        f"$shortcut.WorkingDirectory={ps_single_quote(str(APP_HOME))};"
        "$shortcut.Arguments='--launcher';"
        f"$shortcut.IconLocation={ps_single_quote(str(installed_exe))};"
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


def launch_app() -> None:
    show_progress(100, "安装完成，正在启动 ...")
    pythonw = VENV_DIR / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(APP_DIR / "qt_app.py")], cwd=str(APP_DIR), creationflags=NO_WINDOW)


def launch_installed_launcher() -> None:
    installed_exe = APP_HOME / INSTALLED_EXE_NAME
    if installed_exe.exists():
        subprocess.Popen([str(installed_exe), "--launcher", "--app-home", str(APP_HOME)], cwd=str(APP_HOME), creationflags=NO_WINDOW)
    else:
        launch_app()


def setup_main() -> int:
    try:
        install_home = choose_install_dir()
        set_install_home(install_home)
        init_progress_ui()
        log(f"安装目录: {APP_HOME}")
        install_sources()
        install_self_launcher()
        write_launchers()
        create_desktop_shortcut()
        show_progress(100, "安装完成，正在打开启动器 ...")
        log("安装完成。正在打开 SRTMatcher 启动器 ...")
        launch_installed_launcher()
        if PROGRESS_UI is not None:
            PROGRESS_UI.close_soon()
        return 0
    except Exception as exc:
        show_progress(100, "安装失败，请查看黑窗日志")
        log("")
        log(traceback.format_exc())
        log(f"安装失败: {exc}")
        log("请确认安装目录有写入权限。")
        wait_before_exit()
        return 1


def launcher_main() -> int:
    try:
        home_arg = arg_value("--app-home")
        if home_arg:
            set_install_home(Path(home_arg))
        else:
            set_install_home(Path(sys.executable).resolve().parent)

        if runtime_marker_ready():
            launch_app()
            return 0

        init_progress_ui()
        log(f"软件目录: {APP_HOME}")
        install_sources()

        if main_app_ready():
            log("基础运行环境已就绪，正在启动主程序 ...")
            write_runtime_marker()
            launch_app()
            if PROGRESS_UI is not None:
                PROGRESS_UI.close_soon()
            return 0

        log("首次运行需要准备基础运行环境。CUDA 加速和模型下载会在主程序内按按钮处理。")
        ensure_python_runtime()
        install_python_dependencies()
        write_runtime_marker()
        log("基础运行环境安装完成，正在启动主程序 ...")
        launch_app()
        if PROGRESS_UI is not None:
            PROGRESS_UI.close_soon()
        return 0
    except Exception as exc:
        show_progress(100, "启动失败，请查看日志")
        log("")
        log(traceback.format_exc())
        log(f"启动失败: {exc}")
        log("请确认网络可用，并且安装目录有写入权限。")
        wait_before_exit()
        return 1


def main() -> int:
    if is_installed_launcher_mode():
        return launcher_main()
    return setup_main()


if __name__ == "__main__":
    raise SystemExit(main())
