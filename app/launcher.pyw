"""VLESS Workbench 启动器：tkinter 窗口 + 系统托盘 + Web 服务管理。

单文件 EXE 入口。零额外 GUI 依赖（仅标准库 tkinter + ctypes）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Menu, StringVar, Text, Tk, messagebox, ttk

# 让 vless_tool 可导入（源码运行时 cwd 可能不是 app/）
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    from vless_tool.config import Config, ConfigError, find_config_path, _program_root  # noqa: E402
    from vless_tool.logging_setup import setup_logging  # noqa: E402
    from vless_tool.xray import cleanup_all_processes  # noqa: E402
except Exception:  # noqa: BLE001
    # 打包后导入失败时没有控制台，落盘以便排查
    import traceback

    try:
        base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
        _p = base / "app" / "logs" / "launcher_import_error.log"
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise

# 托盘自定义消息
WM_TRAYICON = 0x8000 + 1  # WM_APP + 1
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

# Python 3.13 移除了部分 ctypes.wintypes 类型，手动补全
if not hasattr(wt, "LRESULT"):
    wt.LRESULT = ctypes.c_ssize_t
if not hasattr(wt, "HCURSOR"):
    wt.HCURSOR = ctypes.c_void_p

# 系统图标常量
IDI_APPLICATION = 32512
LR_LOADFROMFILE = 0x00000010
IMAGE_ICON = 1


# ---------------------------------------------------------------- 单实例互斥
def check_single_instance() -> bool:
    """返回 True 表示本进程是唯一实例；False 表示已有实例在运行。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex_name = "VLESS_Workbench_SingleInstance"
    kernel32.CreateMutexW(None, False, mutex_name)
    err = ctypes.get_last_error()
    return err != 183  # ERROR_ALREADY_EXISTS


def _activate_existing_window() -> bool:
    """尝试找到已有主窗口并激活到前台。返回是否找到。"""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "VLESS Workbench")
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    return False


# ---------------------------------------------------------------- 端口检测
def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


# ---------------------------------------------------------------- 托盘图标（ctypes Shell_NotifyIconW）
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004


def _icon_search_paths() -> list[Path]:
    """打包后优先用 sys._MEIPASS 里的 app.ico，源码运行用 app/app.ico。"""
    paths: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "app.ico")
        paths.append(Path(meipass) / "app" / "app.ico")
    paths.append(APP_DIR / "app.ico")
    paths.append(Path(_program_root()) / "app" / "app.ico")
    paths.append(Path(_program_root()) / "app.ico")
    # 去重，保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _find_icon_path() -> Path | None:
    for candidate in _icon_search_paths():
        if candidate.exists():
            return candidate
    return None


def _load_tray_icon() -> tuple[int, str, Path | None]:
    """加载托盘图标，返回 (hicon, 来源, 文件路径)；加载失败回退系统默认图标。"""
    user32 = ctypes.windll.user32
    for candidate in _icon_search_paths():
        if candidate.exists():
            hicon = user32.LoadImageW(
                0, str(candidate), IMAGE_ICON, 0, 0, LR_LOADFROMFILE,
            )
            if hicon:
                return hicon, f"file:{candidate}", candidate
    return user32.LoadIconW(0, IDI_APPLICATION), "system-default", None


class TrayIcon:
    """基于 ctypes 的系统托盘图标，自带消息窗口线程。

    start() 会阻塞等待 NIM_ADD 完成，返回是否成功。
    """

    def __init__(self, on_activate, on_menu, on_exit):
        self._on_activate = on_activate
        self._on_menu = on_menu
        self._on_exit = on_exit
        self._hwnd = None
        self._hicon = None
        self._icon_source = "unknown"
        self._icon_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._add_result: bool | None = None  # NIM_ADD 是否成功

    def start(self) -> bool:
        """启动托盘线程并等待 NIM_ADD 结果。返回是否成功。"""
        self._thread = threading.Thread(target=self._run, daemon=True, name="TrayIcon")
        self._thread.start()
        self._ready.wait(timeout=5)
        return bool(self._add_result)

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        WNDPROC = ctypes.WINFUNCTYPE(wt.LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_TRAYICON:
                if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    # 菜单交给 Tk 主线程弹出（不能在此线程直接调 Tk）
                    self._on_menu()
                elif lparam == WM_LBUTTONUP:
                    self._on_activate()
            # lparam 可能是大于 2^63 的无符号值，转成有符号后再传给 DefWindowProcW
            try:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except ctypes.ArgumentError:
                sl = lparam & 0xFFFFFFFFFFFFFFFF
                if sl >= 0x8000000000000000:
                    sl -= 0x10000000000000000
                try:
                    return user32.DefWindowProcW(hwnd, msg, wparam, sl)
                except ctypes.ArgumentError:
                    return 1  # 避免 WM_NCCREATE 返回 0 导致窗口创建失败
        self._wndproc = WNDPROC(wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "VLESSWorkbenchTray"

        # 注册窗口类（用局部类名防冲突；忽略"类已存在"错误）
        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HCURSOR),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
            ]

        self._wndcls = WNDCLASS(
            style=0, lpfnWndProc=self._wndproc, cbClsExtra=0, cbWndExtra=0,
            hInstance=hinst, hIcon=0, hCursor=0, hbrBackground=0,
            lpszMenuName=None, lpszClassName=class_name,
        )
        reg = user32.RegisterClassW(ctypes.byref(self._wndcls))
        # ERROR_CLASS_ALREADY_EXISTS = 1410，可忽略
        if reg == 0:
            if ctypes.get_last_error() != 1410:
                self._add_result = False
                self._ready.set()
                return

        self._hwnd = user32.CreateWindowExW(
            0, class_name, "VLESSWorkbench", 0, 0, 0, 0, 0, 0, 0, hinst, None,
        )
        if not self._hwnd:
            self._add_result = False
            self._ready.set()
            return
        self._hicon, self._icon_source, self._icon_path = _load_tray_icon()

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = "VLESS Workbench"
        self._add_result = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))

        self._ready.set()

        if not self._add_result:
            return

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        if self._hwnd:
            shell32 = ctypes.windll.shell32
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            self._hwnd = None

    @property
    def is_ready(self) -> bool:
        return self._add_result is True


# ---------------------------------------------------------------- 托盘右键菜单（tkinter 原生）
TRAY_MENU_LABELS = ["显示窗口", "打开页面", "打开工作目录", "退出"]


def build_tray_menu(root, on_show, on_open, on_open_dir, on_exit):
    """构建托盘右键菜单。

    用 tkinter.Menu 而不是 Win32 CreatePopupMenu：文字、宽度、字体全部交给系统，
    避免 Win32 菜单在部分系统上出现「有框无字、宽度为 0」的问题。
    """
    menu = Menu(root, tearoff=0)
    menu.add_command(label=TRAY_MENU_LABELS[0], command=on_show)
    menu.add_command(label=TRAY_MENU_LABELS[1], command=on_open)
    menu.add_command(label=TRAY_MENU_LABELS[2], command=on_open_dir)
    menu.add_separator()
    menu.add_command(label=TRAY_MENU_LABELS[3], command=on_exit)
    return menu


# ---------------------------------------------------------------- 日志队列 handler
class QueueLogHandler(logging.Handler):
    """把日志记录推入 queue.Queue，供 GUI 线程轮询消费。"""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self._queue = log_queue

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._queue.put(msg)
        except Exception:
            pass


# ---------------------------------------------------------------- 主窗口
class Launcher:
    def __init__(self, auto_start: bool = True, port: int | None = None,
                 init_tray: bool = True):
        self.root = Tk()
        self.root.title("VLESS Workbench")
        self.root.geometry("480x420")
        self.root.resizable(False, False)
        # X（关闭）与最小化都收起到托盘，真正退出只走「退出」按钮 / 托盘菜单
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.bind("<Unmap>", self._on_minimize)
        if sys.excepthook:
            self.root.report_callback_exception = sys.excepthook

        self.config: Config | None = None
        self.server_thread: threading.Thread | None = None
        self.server_running = False
        self.tray: TrayIcon | None = None
        self.tray_ready = False
        self.port_var = StringVar(value=str(port if port else 8765))
        self._httpd = None            # waitress 服务实例，用于真正释放端口
        self._server_host = "127.0.0.1"
        self._server_port = 0
        self._hiding = False
        self._hide_count = 0
        self._exiting = False
        self._busy = False             # 启动/停止切换中，按钮禁用防抖
        self._stop_event: threading.Event | None = None
        self._start_deadline = 0.0
        # 窗口「退出」按钮与托盘菜单「退出」共用同一个退出实现
        self._exit_handler = self._do_exit
        # 窗口图标（与托盘同一个 app.ico）
        icon_path = _find_icon_path()
        if icon_path:
            try:
                self.root.iconbitmap(default=str(icon_path))
            except Exception:  # noqa: BLE001
                pass

        # 日志队列
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._setup_logging()
        # 托盘右键菜单（Tk 主线程创建，托盘线程只负责请求弹出）
        self.tray_menu = build_tray_menu(
            self.root,
            on_show=self._tray_activate,
            on_open=lambda: self._tray_activate(open_browser=True),
            on_open_dir=self.open_work_dir,
            # 托盘菜单的「退出」不再弹确认框，直接走干净退出流程
            on_exit=self._do_exit,
        )
        self._log("启动器就绪")
        self._load_config()
        # 创建托盘（窗口创建时就添加，不等最小化）
        if init_tray:
            self._init_tray()
        # 双击 exe：默认自动启动服务；托盘就绪后才允许开浏览器 + withdraw
        if auto_start:
            self.root.after(0, lambda: self.start_server(auto=True))
        # 启动日志轮询
        self.root.after(200, self._poll_logs)

    # ---------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        # 状态区
        frm = ttk.LabelFrame(self.root, text="状态", padding=8)
        frm.pack(fill=X, **pad)
        self.url_var = StringVar(value="未启动")
        self.engine_var = StringVar(value="-")
        self.xray_var = StringVar(value="-")
        self.cfg_var = StringVar(value="-")
        for label, var in [("服务地址：", self.url_var), ("服务引擎：", self.engine_var),
                           ("Xray 内核：", self.xray_var), ("配置路径：", self.cfg_var)]:
            row = ttk.Frame(frm)
            row.pack(fill=X, pady=1)
            ttk.Label(row, text=label, width=10).pack(side=LEFT)
            ttk.Label(row, textvariable=var).pack(side=LEFT)

        # 端口控制
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill=X, **pad)
        ttk.Label(ctrl, text="端口：").pack(side=LEFT)
        ttk.Entry(ctrl, textvariable=self.port_var, width=8).pack(side=LEFT, padx=4)
        # 「启动/停止」合并开关：未运行显示「启动」，运行中显示「停止」，切换中禁用防抖
        self.btn_toggle = ttk.Button(ctrl, text="启动", command=self.on_toggle)
        self.btn_toggle.pack(side=LEFT, padx=4)
        # 「退出」走与托盘菜单完全相同的 _do_exit
        self.btn_exit = ttk.Button(ctrl, text="退出", command=self._exit_handler)
        self.btn_exit.pack(side=LEFT, padx=4)
        self.btn_open_dir = ttk.Button(ctrl, text="打开工作目录", command=self.open_work_dir)
        self.btn_open_dir.pack(side=RIGHT, padx=4)

        # 日志框（Text 控件，非 Treeview）
        log_frm = ttk.LabelFrame(self.root, text="日志", padding=4)
        log_frm.pack(fill=BOTH, expand=True, **pad)
        self.log_text = Text(log_frm, height=10, wrap="word", state="disabled",
                             font=("Consolas", 9))
        self.log_text.pack(fill=BOTH, expand=True)

        # 底栏
        ttk.Label(self.root, text="关闭 / 最小化都收起到托盘；退出用「退出」按钮或托盘菜单",
                  foreground="gray").pack(pady=2)

    # ---------------------------------------------------------- 日志
    def _setup_logging(self):
        """把 vless_tool logger 的输出同时导入 GUI 日志队列。"""
        logger = logging.getLogger("vless_tool")
        # 先落盘 run.log（程序目录/app/logs），方便排查与复核页面来源
        try:
            setup_logging(Path(_program_root()) / "app" / "logs")
        except Exception:  # noqa: BLE001
            pass
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                               datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        # waitress 也有自己的 logger，一并捕获
        for name in ("waitress", "werkzeug", "flask.app"):
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)

    def _poll_logs(self):
        """轮询日志队列，批量插入 Text 控件。"""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_logs)

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert(END, msg + "\n")
        # 限制最多 500 行
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log_text.delete("1.0", f"{line_count - 500}.0")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _log(self, msg: str):
        """启动器自身日志：直接入队（走统一队列）。"""
        self.log_queue.put(f"{time.strftime('%H:%M:%S')} INFO {msg}")

    # ---------------------------------------------------------- 配置
    def _generate_default_config(self, cfg_path: Path) -> Path | None:
        """首次运行没有 config.json 时，从包内/目录的 config.example.json 生成。"""
        candidates = [
            cfg_path.with_name("config.example.json"),
            APP_DIR / "config.example.json",
        ]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass) / "config.example.json")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, cfg_path)
            except OSError as exc:
                logging.getLogger("vless_tool").error("自动生成 config.json 失败：%s", exc)
                self._log(f"自动生成 config.json 失败：{exc}")
                messagebox.showwarning(
                    "配置生成失败",
                    f"无法生成 {cfg_path}：{exc}\n请检查程序目录是否可写。",
                )
                return None
            logging.getLogger("vless_tool").info("已生成默认配置：%s", cfg_path)
            self._log(f"已生成默认配置：{cfg_path}")
            return cfg_path
        logging.getLogger("vless_tool").error(
            "未找到 config.json，且包内/目录中没有 config.example.json，无法自动生成"
        )
        self._log("未找到 config.json，且包内/目录中没有 config.example.json，无法自动生成")
        messagebox.showwarning(
            "缺少配置文件",
            "未找到 config.json，且没有 config.example.json 可自动生成。\n"
            "请把 config.example.json 复制为 config.json 后重试。",
        )
        return None

    def _load_config(self):
        try:
            cfg_path = find_config_path()
            if not cfg_path.exists():
                cfg_path = self._generate_default_config(cfg_path)
                if cfg_path is None:
                    return
            self.config = Config.load(cfg_path)
            try:
                self.config.ensure_dirs()
            except OSError as exc:
                logging.getLogger("vless_tool").error("目录创建失败：%s（请检查程序目录是否可写）", exc)
                self._log(f"目录创建失败：{exc}（请检查程序目录是否可写）")
                messagebox.showwarning(
                    "目录创建失败",
                    f"无法创建 downloads / app\\tmp / app\\logs 等目录：{exc}\n"
                    "请检查程序所在目录是否有写权限。",
                )
                return
            self.cfg_var.set(str(self.config.path))
            self._log(f"配置文件：{self.config.path}")
            self._refresh_xray_status()
        except ConfigError as exc:
            logging.getLogger("vless_tool").error("配置错误：%s", exc)
            self._log(f"配置错误：{exc}")
            messagebox.showwarning("配置错误", str(exc))

    def _refresh_xray_status(self):
        if not self.config:
            return
        xp = self.config.xray_path
        expected = self.config.base_dir / "app" / "xray" / "xray.exe"
        if xp and xp.exists():
            ver = self._xray_version(xp)
            self.xray_var.set(f"{xp.name}（{ver}）")
            self._log(f"内核：{xp.name} {ver}")
        else:
            self.xray_var.set("未找到 Xray 内核")
            msg = (
                f"未找到 Xray 内核，请把 xray.exe 放到 {expected}，"
                "或修改 config.json 的 xray_path"
            )
            logging.getLogger("vless_tool").warning(msg)
            self._log(msg)

    def _xray_version(self, path: Path) -> str:
        try:
            out = subprocess.run(
                [str(path), "version"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return out.splitlines()[0] if out else "未知版本"
        except Exception:
            return "版本检测失败"

    # ---------------------------------------------------------- 服务启停
    def _set_toggle(self, text: str | None = None, state: str | None = None):
        if text is not None:
            self.btn_toggle.config(text=text)
        if state is not None:
            self.btn_toggle.config(state=state)

    def on_toggle(self):
        """合并开关：运行中→停止，未运行→启动；切换中直接忽略重复点击。"""
        if self._busy or self._exiting:
            return
        if self.server_running:
            self.stop_server()
        else:
            self.start_server(auto=False)

    def start_server(self, auto: bool = False) -> bool:
        if self.server_running or self._busy:
            return False
        if not self.config:
            self._log("配置未加载，无法启动")
            self._set_toggle("启动", "normal")
            return False
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._log("端口必须是数字")
            self._set_toggle("启动", "normal")
            return False
        host = "127.0.0.1"
        if port_in_use(host, port):
            # 端口被占用：不自动启动，按钮保持「启动」，说明原因并允许换端口手动启动
            msg = f"端口 {port} 被占用，未启动；请更换端口后点击「启动」"
            self._log(msg)
            self._set_toggle("启动", "normal")
            messagebox.showwarning("端口占用", msg)
            return False
        self._busy = True
        self._set_toggle("启动中…", "disabled")
        self._server_host, self._server_port = host, port
        url = f"http://{host}:{port}/"
        self.url_var.set(url)
        self._log(f"启动 Web 服务：{url}（auto={auto}）")
        self.server_thread = threading.Thread(
            target=self._serve, args=(host, port), daemon=True,
        )
        self.server_thread.start()
        self._start_deadline = time.time() + 15
        self.root.after(150, self._check_server, host, port)
        return True

    def _serve(self, host, port):
        try:
            from vless_tool.webui import create_app
            from waitress import create_server

            app = create_app(self.config)
            # 用 create_server 而不是 serve()，以便退出时 close() 真正释放监听端口
            httpd = create_server(app, host=host, port=port, threads=8, ident="vless_tool")
            self._httpd = httpd
            self._log("服务引擎：waitress（生产级 WSGI）")
            httpd.run()
        except Exception as exc:
            self._log(f"服务异常：{exc}")

    def _check_server(self, host, port):
        if self._exiting:
            return
        if not self._busy:
            return
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError:
            thread_alive = self.server_thread is not None and self.server_thread.is_alive()
            if not thread_alive and self._httpd is None:
                self._start_failed("服务线程已退出，启动失败（详见日志）")
                return
            if time.time() >= self._start_deadline:
                self._start_failed("服务启动超时（15 秒内端口未监听）")
                return
            self.root.after(300, self._check_server, host, port)
            return
        # 端口已监听：进入运行态
        self.server_running = True
        self._busy = False
        self._set_toggle("停止", "normal")
        self.engine_var.set("waitress（生产级 WSGI）")
        self._log(f"服务已就绪：http://{host}:{port}/")
        # 顺序要求：NIM_ADD 已成功 → 打开浏览器 → withdraw()
        if self.tray_ready:
            self.open_browser()
            self.root.after(0, self._hide_to_tray)
        else:
            self._log("托盘未就绪，窗口保持显示（不自动最小化）")

    def _start_failed(self, msg: str):
        self._busy = False
        self.server_running = False
        self.server_thread = None
        self.url_var.set("未启动")
        self._set_toggle("启动", "normal")
        self._log(msg)
        if self._httpd is not None:
            httpd = self._httpd
            self._httpd = None
            try:
                httpd.close()
            except Exception:  # noqa: BLE001
                pass

    def stop_server(self):
        if self._busy or not self.server_running:
            return
        self._busy = True
        self._set_toggle("停止中…", "disabled")
        self._log("停止服务…")
        # 关闭/等端口释放放在后台线程，避免卡住 Tk 事件循环；
        # 后台线程只置 Event，由主线程轮询后更新控件（tkinter 跨线程调用不安全）
        self._stop_event = threading.Event()
        threading.Thread(target=self._stop_worker, daemon=True).start()
        self.root.after(100, self._wait_stopped)

    def _stop_worker(self):
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.close()
            except Exception as exc:  # noqa: BLE001
                self._log(f"关闭服务异常：{exc}")
        # 等监听端口真正释放，避免下次启动报「端口被占用」
        deadline = time.time() + 3
        while time.time() < deadline and port_in_use(self._server_host, self._server_port):
            time.sleep(0.05)
        cleanup_all_processes()
        if self._stop_event is not None:
            self._stop_event.set()

    def _wait_stopped(self):
        if self._exiting:
            return
        if self._stop_event is not None and self._stop_event.is_set():
            self._stop_event = None
            self._on_stopped()
        else:
            self.root.after(100, self._wait_stopped)

    def _on_stopped(self):
        self.server_running = False
        self._busy = False
        self.url_var.set("未启动")
        self._set_toggle("启动", "normal")
        self._log("服务已停止，端口已释放")

    def open_browser(self):
        if self.server_running:
            webbrowser.open(self.url_var.get())

    def open_work_dir(self):
        """打开程序所在目录（打包=exe 所在目录；源码=仓库根目录，复用 _program_root）。"""
        path = _program_root()
        if not path.exists():
            self._log(f"工作目录不存在：{path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            self._log(f"打开工作目录：{path}")
        except Exception:  # noqa: BLE001
            try:
                subprocess.Popen(["explorer", str(path)])
                self._log(f"打开工作目录（explorer）：{path}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"打开工作目录失败：{path}（{exc}）")

    # ---------------------------------------------------------- 托盘
    def _init_tray(self):
        """窗口创建时就添加托盘图标。"""
        self.tray = TrayIcon(
            on_activate=self._tray_activate,
            on_menu=self._popup_tray_menu,
            # 托盘「退出」与窗口「退出」按钮共用同一个 _do_exit
            on_exit=self._exit_handler,
        )
        self.tray_ready = self.tray.start()
        if self.tray_ready:
            self._log("托盘图标已就绪")
        else:
            self._log("托盘初始化失败")
            messagebox.showwarning(
                "托盘初始化失败",
                "系统托盘图标创建失败，最小化时窗口将保持显示。",
            )

    def _on_minimize(self, event):
        # 只处理主窗口自己的 Unmap；子控件 Unmap 会冒泡到 toplevel，曾导致重复日志刷屏
        if event.widget is not self.root:
            return
        # 窗口被最小化时隐藏到托盘
        if self.root.state() != "iconic":
            return
        if self._hiding:
            return
        if not self.tray_ready:
            # 托盘未就绪：恢复窗口显示，不隐藏
            self._log("托盘未就绪，保持窗口显示")
            self.root.after(0, self.root.deiconify)
            return
        # 托盘已就绪才隐藏（X 与最小化都走 _hide_to_tray）
        self.root.after(100, self._hide_to_tray)

    def _hide_to_tray(self):
        if self._exiting:
            return
        if not self.tray_ready:
            # 托盘创建失败时不得 withdraw，窗口保持可见
            self._log("托盘未就绪，不隐藏窗口")
            try:
                self.root.deiconify()
            except Exception:  # noqa: BLE001
                pass
            return
        if self._hiding:
            return
        self._hiding = True
        try:
            self.root.withdraw()
            self._hide_count += 1
            self._log("已最小化到系统托盘（左键恢复 / 右键菜单）")
        finally:
            self._hiding = False

    def _tray_activate(self, open_browser: bool = False):
        def _restore():
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.root.after(0, _restore)
        if open_browser:
            self.root.after(200, self.open_browser)

    def _popup_tray_menu(self):
        """托盘线程调用：切回 Tk 主线程弹出原生菜单。"""
        self.root.after(0, self._do_popup_tray_menu)

    def _do_popup_tray_menu(self):
        x, y = self.root.winfo_pointerxy()
        try:
            self.tray_menu.tk_popup(x, y)
        finally:
            # 必须释放 grab，否则下一次菜单可能无法弹出
            self.tray_menu.grab_release()

    # ---------------------------------------------------------- 退出
    def _do_exit(self):
        """干净退出：停服务 → 释放端口 → 清理 xray 子进程 → NIM_DELETE → 退出进程。

        窗口「退出」按钮与托盘菜单「退出」共用本方法，不再弹确认框。
        """
        if self._exiting:
            return
        self._exiting = True
        self._log("正在退出…")
        # 1. 停服务并释放监听端口（同步执行，保证进程退出前端口已释放）
        if self.server_running:
            httpd = self._httpd
            self._httpd = None
            if httpd is not None:
                try:
                    httpd.close()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"关闭服务异常：{exc}")
            deadline = time.time() + 3
            while time.time() < deadline and port_in_use(self._server_host, self._server_port):
                time.sleep(0.05)
            self.server_running = False
            self._busy = False
        # 2. 清理 xray 子进程
        cleanup_all_processes()
        # 3. 移除托盘（NIM_DELETE）
        if self.tray:
            self.tray.stop()
            self._log("托盘图标已移除")
        # 4. 销毁窗口，mainloop 返回后进程自然退出
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 托盘自检模式
def run_tray_selftest() -> int:
    """无 GUI 托盘自检：NIM_ADD → 模拟左键 → NIM_DELETE。"""
    log_path = Path(_program_root()) / "app" / "logs" / "tray_selftest.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(msg: str):
        lines.append(msg)
        # --noconsole 打包后 sys.stdout 为 None，直接 print 会抛异常导致自检中断
        try:
            if sys.stdout is not None and not getattr(sys.stdout, "closed", False):
                print(msg)
        except Exception:  # noqa: BLE001
            pass
        # 逐行落盘，避免中途异常时日志为空
        try:
            log_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    log(f"=== Tray Self-Test @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    activated = threading.Event()
    menu_requested = threading.Event()

    def fake_activate(open_browser: bool = False):
        activated.set()
        log(f"  [callback] on_activate called (open_browser={open_browser})")

    def fake_menu():
        menu_requested.set()
        log("  [callback] on_menu called (right-click)")

    def fake_exit():
        log("  [callback] on_exit called")

    tray = TrayIcon(on_activate=fake_activate, on_menu=fake_menu, on_exit=fake_exit)

    # Step 1: NIM_ADD
    log("Step 1: Shell_NotifyIconW(NIM_ADD) ...")
    ok = tray.start()
    log(f"  NIM_ADD result: {ok}")
    log(f"  tray hwnd: {tray._hwnd}")
    log(f"  tray hicon: {tray._hicon}")
    default_icon = ctypes.windll.user32.LoadIconW(0, IDI_APPLICATION)
    icon_file = _find_icon_path()
    log(f"  icon source: {tray._icon_source}")
    log(f"  icon path: {tray._icon_path or icon_file}")
    if icon_file is not None:
        log(f"  icon file size: {icon_file.stat().st_size} bytes")
    log(f"  icon loaded from file: {tray._icon_source.startswith('file:')}")
    log(f"  hicon != system default: {bool(tray._hicon) and tray._hicon != default_icon}")
    icon_ok = bool(tray._hicon) and tray._hicon != default_icon and tray._icon_source.startswith("file:")
    if not icon_ok:
        log("FAIL: 图标未从 app.ico 加载（仍在使用系统默认图标）")

    if not ok:
        log("FAIL: NIM_ADD returned False")
        log_path.write_text("\n".join(lines), encoding="utf-8")
        return 1

    # Step 2: 校验右键菜单结构（tkinter 原生菜单，可直接读 label）
    log("Step 2: build tray menu and assert labels ...")
    log(f"  work dir ({'exe' if getattr(sys, 'frozen', False) else 'source'}): {_program_root()}")
    root = Tk()
    root.withdraw()
    menu = build_tray_menu(root, on_show=lambda: None, on_open=lambda: None,
                           on_open_dir=lambda: None, on_exit=lambda: None)
    items: list[tuple[int, str, str]] = []
    for i in range(menu.index("end") + 1):
        kind = menu.type(i)
        label = menu.entrycget(i, "label") if kind == "command" else ""
        items.append((i, kind, label))
        log(f"  entry {i}: type={kind} label={label!r}")
    commands = [(i, label) for i, kind, label in items if kind == "command"]
    labels = [label for _, label in commands]
    log(f"  menu command count: {len(commands)} (expected {len(TRAY_MENU_LABELS)})")
    labels_ok = True
    for pos, expected in enumerate(TRAY_MENU_LABELS):
        got = labels[pos] if pos < len(labels) else "<missing>"
        same = got == expected
        labels_ok = labels_ok and same
        log(f"  entrycget({pos},'label') == '{expected}': {same} (got {got!r})")
    sep_indices = [i for i, kind, _ in items if kind == "separator"]
    sep_ok = sep_indices == [3]
    log(f"  separator index == [3] (before 退出): {sep_ok} (got {sep_indices})")
    labels_non_empty = all(label for label in labels)
    log(f"  all command labels non-empty: {labels_non_empty} (got {labels})")
    if not labels_ok or len(commands) != len(TRAY_MENU_LABELS) or not sep_ok or not labels_non_empty:
        log(f"FAIL: unexpected menu structure {labels}")
        root.destroy()
        log_path.write_text("\n".join(lines), encoding="utf-8")
        return 1
    root.destroy()

    # Step 2b: 退出按钮与托盘「退出」共用同一 _do_exit
    log("Step 2b: assert exit button and tray menu share _do_exit ...")
    exit_ok = False
    launcher = None
    try:
        launcher = Launcher(auto_start=False, init_tray=True)
        launcher.root.withdraw()
        same_handler = launcher._exit_handler == launcher._do_exit
        tray_same = launcher.tray is not None and launcher.tray._on_exit == launcher._do_exit
        btn_text = launcher.btn_exit.cget("text")
        menu_exit_label = launcher.tray_menu.entrycget(4, "label")
        exit_ok = same_handler and tray_same and btn_text == "退出" and menu_exit_label == "退出"
        log(f"  launcher._exit_handler == launcher._do_exit: {same_handler}")
        log(f"  tray._on_exit == launcher._do_exit: {tray_same}")
        log(f"  btn_exit text: {btn_text!r}")
        log(f"  tray_menu entry 4 label: {menu_exit_label!r}")
        log(f"  tray ready (for exit test): {launcher.tray_ready}")
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL: shared exit assertion raised {exc!r}")
    finally:
        if launcher is not None:
            try:
                launcher._do_exit()
            except Exception:  # noqa: BLE001
                pass
    if not exit_ok:
        log("FAIL: 退出按钮与托盘退出未共用同一 _do_exit")

    # Step 3: 模拟左键 → 恢复回调
    log("Step 3: simulate WM_LBUTTONUP ...")
    user32 = ctypes.windll.user32
    user32.PostMessageW(tray._hwnd, WM_TRAYICON, 1, WM_LBUTTONUP)
    time.sleep(0.5)
    log(f"  activate callback invoked: {activated.is_set()}")

    # Step 4: 模拟右键 → 菜单请求回调
    log("Step 4: simulate WM_RBUTTONUP ...")
    user32.PostMessageW(tray._hwnd, WM_TRAYICON, 1, WM_RBUTTONUP)
    time.sleep(0.5)
    log(f"  menu callback invoked: {menu_requested.is_set()}")

    # Step 5: NIM_DELETE
    log("Step 5: Shell_NotifyIconW(NIM_DELETE) ...")
    tray.stop()
    log(f"  NIM_DELETE done; tray hwnd cleared: {tray._hwnd is None}")

    passed = activated.is_set() and menu_requested.is_set() and icon_ok and exit_ok
    log(f"=== Tray Self-Test: {'PASS' if passed else 'FAIL'} ===")
    log("注：菜单弹出视觉效果需在真实桌面确认；此处仅断言结构与回调。")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return 0 if passed else 1


# ---------------------------------------------------------------- 启动器 UI 自检模式
def _pump(root, predicate=None, timeout: float = 15.0) -> bool:
    """在无 mainloop 的自检里驱动 Tk 事件循环，直到条件成立或超时。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            root.update()
        except Exception:  # noqa: BLE001
            return False
        if predicate is not None and predicate():
            return True
        time.sleep(0.02)
    return bool(predicate()) if predicate is not None else True


def _pick_free_port(start: int) -> int:
    for port in range(start, start + 50):
        if not port_in_use("127.0.0.1", port):
            return port
    raise RuntimeError("找不到空闲端口用于 UI 自检")


def run_ui_selftest() -> int:
    """无人工介入的启动器状态机自检：自动启动 → 停止 → 启动 → X/最小化 → 退出。"""
    log_path = Path(_program_root()) / "app" / "logs" / "ui_selftest.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(msg: str):
        lines.append(msg)
        try:
            if sys.stdout is not None and not getattr(sys.stdout, "closed", False):
                print(msg)
        except Exception:  # noqa: BLE001
            pass
        try:
            log_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    log(f"=== Launcher UI Self-Test @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    log(f"  work dir ({'exe' if getattr(sys, 'frozen', False) else 'source'}): {_program_root()}")

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        log(f"  {name}: {'PASS' if cond else 'FAIL'} {detail}".rstrip())
        if not cond:
            failures.append(name)

    port = _pick_free_port(18765)
    log(f"  test port: {port}")

    browser_calls: list[str] = []
    opened_dirs: list[str] = []
    real_open = webbrowser.open
    real_startfile = getattr(os, "startfile", None)
    webbrowser.open = lambda url, *a, **k: (browser_calls.append(url), True)[1]  # type: ignore[assignment]
    os.startfile = lambda p: opened_dirs.append(str(p))  # type: ignore[attr-defined]

    launcher = None
    try:
        # ---- 1. 自动启动（模拟双击 exe）----
        launcher = Launcher(auto_start=True, port=port)
        started = _pump(launcher.root, lambda: launcher.server_running, timeout=20)
        check("auto start (port LISTEN)", started)
        if not started:
            log("FAIL: 自动启动未就绪，后续状态机断言中止")
            return 1
        _pump(launcher.root, lambda: launcher.root.winfo_viewable() == 0, timeout=3)
        check("toggle text after auto start == '停止'",
              launcher.btn_toggle.cget("text") == "停止", repr(launcher.btn_toggle.cget("text")))
        check("toggle button enabled after start",
              str(launcher.btn_toggle.cget("state")) == "normal")
        check("port is listening after auto start", port_in_use("127.0.0.1", port), f"port={port}")
        check("browser opened automatically",
              bool(browser_calls) and browser_calls[-1] == f"http://127.0.0.1:{port}/",
              repr(browser_calls))
        check("tray ready", launcher.tray_ready is True)
        check("window withdrawn to tray after auto start",
              launcher.root.winfo_viewable() == 0,
              f"viewable={launcher.root.winfo_viewable()} hide_count={launcher._hide_count}")

        # ---- 2. 点开关：停止 ----
        launcher.on_toggle()
        stopped = _pump(launcher.root,
                        lambda: (not launcher.server_running) and (not launcher._busy),
                        timeout=10)
        check("toggle -> stop finished", stopped)
        check("toggle text after stop == '启动'",
              launcher.btn_toggle.cget("text") == "启动", repr(launcher.btn_toggle.cget("text")))
        check("port released after stop", not port_in_use("127.0.0.1", port), f"port={port}")

        # ---- 3. 点开关：重新启动 ----
        launcher.on_toggle()
        restarted = _pump(launcher.root, lambda: launcher.server_running, timeout=20)
        check("toggle -> start finished", restarted)
        if restarted:
            _pump(launcher.root, lambda: launcher.root.winfo_viewable() == 0, timeout=3)
            check("toggle text after restart == '停止'",
                  launcher.btn_toggle.cget("text") == "停止", repr(launcher.btn_toggle.cget("text")))
            check("port is listening after restart", port_in_use("127.0.0.1", port), f"port={port}")
            check("browser opened again on restart", len(browser_calls) >= 2, repr(browser_calls))

        # ---- 4. 窗口 X（WM_DELETE_WINDOW）→ _hide_to_tray ----
        launcher.root.deiconify()
        _pump(launcher.root, lambda: launcher.root.winfo_viewable() == 1, timeout=3)
        before_x = launcher._hide_count
        viewable_before_x = launcher.root.winfo_viewable()
        proto = launcher.root.protocol("WM_DELETE_WINDOW")
        cmd = proto[0] if isinstance(proto, (tuple, list)) else proto
        log(f"  WM_DELETE_WINDOW command: {cmd!r}")
        launcher.root.tk.call(cmd)
        _pump(launcher.root, lambda: launcher.root.winfo_viewable() == 0, timeout=3)
        check("X -> _hide_to_tray (viewable 1->0)",
              viewable_before_x == 1 and launcher.root.winfo_viewable() == 0,
              f"before={viewable_before_x} after={launcher.root.winfo_viewable()}")
        check("X increments _hide_count", launcher._hide_count == before_x + 1,
              f"{before_x}->{launcher._hide_count}")
        check("X does not exit process", not launcher._exiting and launcher.tray_ready is True)

        # ---- 5. 最小化 → _hide_to_tray ----
        launcher.root.deiconify()
        _pump(launcher.root, lambda: launcher.root.winfo_viewable() == 1, timeout=3)
        before_min = launcher._hide_count
        launcher.root.iconify()
        hidden = _pump(launcher.root,
                       lambda: launcher.root.state() == "withdrawn" and launcher._hide_count > before_min,
                       timeout=5)
        check("minimize -> _hide_to_tray (withdrawn)", hidden,
              f"state={launcher.root.state()} hide_count={launcher._hide_count}")
        check("minimize viewable == 0", launcher.root.winfo_viewable() == 0)
        check("minimize does not exit process", not launcher._exiting and launcher.tray_ready is True)

        # ---- 6. 打开工作目录（真实路径 + 不存在路径不崩）----
        path_expected = _program_root()
        launcher.open_work_dir()
        check("open work dir uses _program_root()",
              bool(opened_dirs) and opened_dirs[-1] == str(path_expected),
              f"got={opened_dirs[-1] if opened_dirs else None} expected={path_expected}")
        check("work dir exists", path_expected.exists(), str(path_expected))
        captured: list[str] = []
        orig_log = launcher._log
        launcher._log = lambda m: (captured.append(m), orig_log(m))  # type: ignore[assignment]
        orig_root = globals()["_program_root"]
        try:
            globals()["_program_root"] = lambda: Path("Z:/__vless_missing_dir__")
            launcher.open_work_dir()
        finally:
            globals()["_program_root"] = orig_root
            launcher._log = orig_log  # type: ignore[assignment]
        check("missing work dir logs without crash",
              any("工作目录不存在：" in m for m in captured), repr(captured))

        # ---- 7. 退出按钮与托盘退出共用 _do_exit ----
        check("exit button uses _do_exit", launcher._exit_handler == launcher._do_exit)
        check("tray exit uses _do_exit",
              launcher.tray is not None and launcher.tray._on_exit == launcher._do_exit)
        check("exit button text == '退出'", launcher.btn_exit.cget("text") == "退出")
        check("tray menu has 打开工作目录",
              launcher.tray_menu.entrycget(2, "label") == "打开工作目录",
              repr(launcher.tray_menu.entrycget(2, "label")))

        # ---- 8. 干净退出 ----
        tray_obj = launcher.tray
        launcher._do_exit()
        check("exit stops service / releases port", not port_in_use("127.0.0.1", port))
        check("exit removed tray icon (NIM_DELETE)",
              tray_obj is not None and tray_obj._hwnd is None)
        check("exit flag set", launcher._exiting is True)
    except Exception:  # noqa: BLE001
        import traceback
        log("FAIL: exception during ui selftest")
        log(traceback.format_exc())
        failures.append("exception")
    finally:
        webbrowser.open = real_open  # type: ignore[assignment]
        if real_startfile is not None:
            os.startfile = real_startfile  # type: ignore[attr-defined]
        if launcher is not None and not launcher._exiting:
            try:
                launcher._do_exit()
            except Exception:  # noqa: BLE001
                pass

    passed = not failures
    log(f"=== Launcher UI Self-Test: {'PASS' if passed else 'FAIL'} ===")
    if failures:
        log(f"  failed checks: {failures}")
    log("注：托盘图标是否可见、浏览器是否真的弹出、收起到托盘的实际观感需真实桌面肉眼确认。")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return 0 if passed else 1


# ---------------------------------------------------------------- 入口
def _install_error_hook():
    """把未捕获异常写入日志，避免 --noconsole 打包后只剩一个弹窗。"""
    import traceback

    def hook(exc_type, exc, tb):
        try:
            path = Path(_program_root()) / "app" / "logs" / "launcher_error.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(traceback.format_exception(exc_type, exc, tb)), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook
    return hook


def main():
    _install_error_hook()
    # 托盘自检模式
    if "--tray-selftest" in sys.argv:
        sys.exit(run_tray_selftest())
    # 启动器状态机自检模式（自动启动 / 开关 / X / 最小化 / 退出）
    if "--ui-selftest" in sys.argv:
        sys.exit(run_ui_selftest())

    try:
        if not check_single_instance():
            # 已有实例运行：尝试激活其窗口
            if not _activate_existing_window():
                # 找不到窗口（可能已最小化到托盘），提示用户
                ctypes.windll.user32.MessageBoxW(
                    0, "VLESS Workbench 已在运行（见系统托盘）。", "VLESS Workbench", 0x40,
                )
            sys.exit(0)

        # 日志配置
        setup_logging(Path(_program_root()) / "app" / "logs")

        launcher = Launcher()
        launcher.run()
    except Exception:
        import traceback
        crash_log = Path(_program_root()) / "launcher_crash.log"
        try:
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
