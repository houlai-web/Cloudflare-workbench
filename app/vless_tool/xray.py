"""Xray 内核管理、真延迟/下载速度测试与子进程清理。"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .nodes import VlessNode, build_xray_config, build_xray_pool_config

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class XrayTestError(RuntimeError):
    """内核路径/启动等系统性错误。"""


class KernelStartError(XrayTestError):
    """Xray 内核无法启动或本地入站未就绪。"""


@dataclass
class NodeResult:
    node: VlessNode
    latency_ms: float | None = None
    speed_mbps: float | None = None
    downloaded_bytes: int = 0
    status: str = "未测试"
    error_type: str = ""
    detail: str = ""
    attempts: int = 0
    tested_at: str = ""
    latency_samples: list[float] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.latency_ms is not None

    @property
    def speed_mb_s(self) -> float | None:
        if self.speed_mbps is None:
            return None
        return self.speed_mbps / 8

    @property
    def downloaded_mb(self) -> float:
        return self.downloaded_bytes / (1024 * 1024)

    def sort_key(self, mode: str = "latency") -> tuple:
        if mode == "speed":
            return (
                0 if self.latency_ms is not None else 1,
                -(self.speed_mbps or -1),
                self.latency_ms if self.latency_ms is not None else float("inf"),
            )
        return (
            0 if self.latency_ms is not None else 1,
            self.latency_ms if self.latency_ms is not None else float("inf"),
            -(self.speed_mbps or -1),
        )


# ---------------------------------------------------------------------------
# Windows Job Object：即使本程序崩溃，也由操作系统回收所有 Xray 子进程
# ---------------------------------------------------------------------------


class WindowsKillJob:
    """把子进程放入 KILL_ON_JOB_CLOSE 的 Job Object（尽力而为）。"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.handle = None
        self._ctypes = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError("CreateJobObjectW 失败")
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            info.BasicLimitInformation.LimitFlags = 0x00002000
            ok = kernel32.SetInformationJobObject(
                handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(handle)
                raise OSError("SetInformationJobObject 失败")
            self.handle = handle
            self.kernel32 = kernel32
            self.ctypes = ctypes
            self.wintypes = wintypes
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Windows Job Object 初始化失败（仍会显式结束子进程）：%s", exc)
            self.handle = None

    def assign(self, process: subprocess.Popen) -> None:
        if not self.handle or not IS_WINDOWS:
            return
        try:
            self.kernel32.AssignProcessToJobObject(
                self.handle, self.wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("把 Xray 进程加入 Job Object 失败：%s", exc)

    def close(self) -> None:
        if self.handle:
            try:
                self.kernel32.CloseHandle(self.handle)
            except Exception:  # noqa: BLE001
                pass
            self.handle = None


# ---------------------------------------------------------------------------
# 全局子进程注册表：退出时兜底清理
# ---------------------------------------------------------------------------


class ProcessRegistry:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen] = {}
        self.job = WindowsKillJob(logger)
        atexit.register(self.cleanup_all)

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes[process.pid] = process
        self.job.assign(process)

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def terminate(self, process: subprocess.Popen) -> None:
        try:
            if process.poll() is None:
                # 先直接 kill（Xray 不派生子进程），比每次拉起 taskkill.exe 快一个数量级；
                # 失败时再退回 taskkill /T 兜底。
                try:
                    process.kill()
                    process.wait(timeout=1.5)
                except Exception:  # noqa: BLE001
                    if IS_WINDOWS:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW,
                            timeout=5,
                            check=False,
                        )
                        process.wait(timeout=2)
                    else:
                        process.terminate()
                        process.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("结束 Xray 进程 %s 失败：%s", process.pid, exc)
        finally:
            self.unregister(process)

    def cleanup_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self.terminate(process)
        self.job.close()


# 模块级单例，main 退出时也会调用 cleanup_all()
_registry_lock = threading.Lock()
_registry: ProcessRegistry | None = None


def get_registry(logger: logging.Logger) -> ProcessRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ProcessRegistry(logger)
        return _registry


def cleanup_all_processes() -> None:
    if _registry is not None:
        _registry.cleanup_all()


# ---------------------------------------------------------------------------
# 端口分配
# ---------------------------------------------------------------------------


class PortAllocator:
    def __init__(self, start: int, end: int):
        self.start = max(1, int(start))
        self.end = min(65535, int(end))
        if self.end < self.start:
            raise XrayTestError("test.port_start 必须小于 test.port_end")
        self._lock = threading.Lock()
        self._used: set[int] = set()
        self._next = self.start

    @staticmethod
    def _free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def allocate(self, count: int = 1) -> list[int]:
        with self._lock:
            found: list[int] = []
            scanned = 0
            total = self.end - self.start + 1
            while len(found) < count and scanned < total:
                port = self._next
                self._next = self.start if port >= self.end else port + 1
                scanned += 1
                if port in self._used:
                    continue
                if self._free(port):
                    self._used.add(port)
                    found.append(port)
            if len(found) < count:
                for port in found:
                    self._used.discard(port)
                raise XrayTestError(
                    f"在 {self.start}-{self.end} 范围内找不到 {count} 个可用端口，"
                    "请调大 test.port_start/test.port_end"
                )
            return found

    def release(self, ports: list[int]) -> None:
        with self._lock:
            for port in ports:
                self._used.discard(port)


# ---------------------------------------------------------------------------
# 测试器
# ---------------------------------------------------------------------------


def _find_xray_under(root: Path) -> Path | None:
    if not root.exists():
        return None
    for candidate in root.rglob("xray.exe"):
        if candidate.is_file():
            return candidate.resolve()
    for candidate in root.rglob("xray"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_xray_executable(config: Any, logger: logging.Logger | None = None) -> Path:
    """解析内核路径；路径失效时在项目 xray 目录递归查找并回写 config.json。"""
    raw = config.xray_path
    path = Path(raw) if raw is not None else None

    if path is not None:
        if path.is_dir():
            for name in ("xray.exe", "xray"):
                candidate = path / name
                if candidate.exists():
                    return candidate.resolve()
            found = _find_xray_under(path)
        elif path.exists():
            return path.resolve()
        else:
            found = None
    else:
        found = None

    search_root = config.base_dir / "xray"
    if found is None:
        found = _find_xray_under(search_root)
    if found is None and search_root != config.base_dir:
        found = _find_xray_under(config.base_dir)
    if found is None:
        if raw is None:
            raise XrayTestError(
                "未配置 Xray 内核路径（config.json -> xray_path）。\n"
                f"请把 xray.exe 放到 {config.base_dir / 'app' / 'xray' / 'xray.exe'}，"
                "或修改 config.json 的 xray_path 指向它。"
            )
        raise XrayTestError(
            f"未找到 Xray 内核：{path}\n"
            f"请把 xray.exe 放到 {config.base_dir / 'app' / 'xray' / 'xray.exe'}，"
            "或修改 config.json 的 xray_path 指向它。\n"
            "下载地址：https://github.com/XTLS/Xray-core/releases（Windows 64 位压缩包，"
            "解压后把 xray.exe 放到上述路径）。"
        )

    config.data["xray_path"] = str(found)
    try:
        config.save()
    except OSError as exc:
        if logger:
            logger.warning("发现 Xray 内核但回写 config.json 失败：%s", exc)
    if logger:
        logger.info("已自动发现 Xray 内核并更新 xray_path：%s", found)
    print(f"[Xray] 已在 {search_root} 下递归找到内核，并更新 config.json：{found}")
    return found


@dataclass
class KernelProbe:
    exe: Path
    version: str
    port: int
    listened: bool
    pid: int
    exit_code: int | None
    cleaned: bool
    leftover_own: bool
    preexisting_pids: set[int] = field(default_factory=set)
    remaining_pids: set[int] = field(default_factory=set)
    temp_dir: Path | None = None


def _xray_process_pids() -> set[int]:
    if not IS_WINDOWS:
        return set()
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq xray.exe", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=8,
        ).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return set()
    pids: set[int] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _version_of(exe: Path) -> str:
    try:
        result = subprocess.run(
            [str(exe), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XrayTestError(f"无法执行 Xray 内核：{exc}") from exc
    text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if result.returncode != 0 or not first:
        raise XrayTestError(f"Xray 版本命令失败（退出码 {result.returncode}）：{text[:500]}")
    return first


def probe_kernel(config: Any, logger: logging.Logger | None = None) -> KernelProbe:
    """真实启动一次最小 socks 入站内核，确认监听后干净退出；不连接任何外部节点。"""
    logger = logger or logging.getLogger("vless_tool.xray")
    exe = resolve_xray_executable(config, logger)
    version = _version_of(exe)
    print(f"[Xray] 内核路径：{exe}")
    print(f"[Xray] 版本：{version}")

    preexisting = _xray_process_pids()
    temp_root = config.resolve("work_dir")
    temp_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = temp_root / f"kernel_probe_{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)
    port = _free_local_port()
    config_path = work_dir / "config.json"
    log_path = work_dir / "probe.log"
    config_path.write_text(
        json.dumps(
            {
                "log": {"loglevel": "warning"},
                "inbounds": [
                    {
                        "tag": "probe-socks",
                        "listen": "127.0.0.1",
                        "port": port,
                        "protocol": "socks",
                        "settings": {"auth": "noauth", "udp": False},
                    }
                ],
                "outbounds": [{"tag": "direct", "protocol": "freedom"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    process: subprocess.Popen | None = None
    log_handle = None
    listened = False
    cleaned = False
    leftover_own = False
    exit_code: int | None = None
    try:
        log_handle = open(log_path, "ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(exe), "run", "-c", str(config_path)],
                cwd=str(work_dir),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise XrayTestError(f"无法启动 Xray 内核做探测：{exc}") from exc
        deadline = time.time() + 10
        while time.time() < deadline:
            if process.poll() is not None:
                break
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                try:
                    sock.connect(("127.0.0.1", port))
                    listened = True
                    break
                except OSError:
                    pass
            time.sleep(0.1)
        if not listened:
            log_text = ""
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            shutil.rmtree(work_dir, ignore_errors=True)
            raise KernelStartError(
                f"最小配置内核未能在 127.0.0.1:{port} 监听。进程退出码={process.poll()}；"
                f"日志：{log_text[-800:] or '无输出'}"
            )
        print(f"[Xray] 最小 socks 入站已监听：127.0.0.1:{port}（PID {process.pid}）")
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=5,
                        check=False,
                    )
                process.wait(timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("结束探测内核 %s 失败：%s", process.pid, exc)
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            exit_code = process.poll()
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass

    pid = process.pid if process is not None else 0
    deadline = time.time() + 3
    while time.time() < deadline:
        if not _port_is_open(port):
            break
        time.sleep(0.1)
    remaining = _xray_process_pids()
    pid_alive = pid in remaining
    if pid_alive:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=5,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass
        remaining = _xray_process_pids()
        pid_alive = pid in remaining
    leftover_own = pid_alive
    cleaned = not _port_is_open(port) and not leftover_own
    if not cleaned:
        print(f"[Xray] 警告：探测后仍有残留（PID 存活={leftover_own}，端口开放={_port_is_open(port)}）")
    else:
        print(f"[Xray] 探测进程已退出（退出码 {exit_code}），端口 {port} 已释放，无残留。")

    unrelated = remaining - preexisting - ({pid} if pid else set())
    if unrelated:
        print(f"[Xray] 注意：系统上另有 {len(unrelated)} 个非本工具启动的 xray 进程仍在运行，未触碰：{sorted(unrelated)}")
    elif preexisting:
        print(f"[Xray] 说明：启动前已存在 {len(preexisting)} 个 xray 进程（非本工具启动），本次未触碰。")

    shutil.rmtree(work_dir, ignore_errors=True)
    return KernelProbe(
        exe=exe,
        version=version,
        port=port,
        listened=listened,
        pid=pid,
        exit_code=exit_code,
        cleaned=cleaned,
        leftover_own=leftover_own,
        preexisting_pids=preexisting,
        remaining_pids=remaining,
        temp_dir=work_dir,
    )


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _make_work_dir(root: Path) -> Path:
    """为单个节点创建独立工作目录。

    不使用 tempfile.mkdtemp：它在 Windows 上遇到权限类错误时会不断换名重试
    （脚本/安全软件拦截时表现为长时间卡住）。这里单次创建，失败立即报错。
    """
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        candidate = root / f"xray_node_{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise XrayTestError(f"无法创建 Xray 临时目录（{root}）：{exc}") from exc
    raise XrayTestError(f"在 {root} 下连续 10 次未能创建唯一临时目录，请清理后重试")


class XrayTester:
    def __init__(self, config: Any, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.registry = get_registry(logger)
        self.exe = resolve_xray_executable(config)
        self.allocator = PortAllocator(
            int(config.get("test.port_start", 20000)),
            int(config.get("test.port_end", 40000)),
        )
        self.temp_root = config.resolve("work_dir")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        # 延迟并发与测速并发分开：延迟只受套接字/网络限制，测速受带宽限制。
        legacy = int(config.get("test.concurrency", 50))
        self.latency_concurrency = max(
            1, int(config.get("test.latency_concurrency", legacy))
        )
        self.speed_concurrency = max(1, int(config.get("test.speed_concurrency", 5)))
        # 一个内核进程最多承载多少个节点的入站/出站，避免端口与内存无限增长
        self.kernel_batch = max(
            1, min(self.latency_concurrency, int(config.get("test.kernel_batch", 50)))
        )
        self.startup_timeout = max(2, int(config.get("test.startup_timeout", 8)))
        # 延迟探测用独立（更短）超时，避免不可达节点吃满测速用的长超时
        self.latency_timeout = max(
            1.0, float(config.get("test.latency_timeout", config.get("test.request_timeout", 6)))
        )
        self.request_timeout = max(2, int(config.get("test.request_timeout", 10)))
        self.poll_interval = max(0.01, min(0.5, float(config.get("test.poll_interval", 0.03))))
        self.latency_samples = max(1, int(config.get("test.latency_samples", 2)))
        self.latency_mode = str(config.get("test.latency_sample_mode", "best")).lower()
        self.speed_max_bytes = max(0, int(config.get("test.speed_max_bytes", 8_000_000)))
        self.speed_max_seconds = max(1, int(config.get("test.speed_max_seconds", 15)))
        self.speed_timeout = max(2, int(config.get("test.speed_timeout", 20)))
        self.retries = max(0, int(config.get("test.retries", 0)))
        self.inbound = str(config.get("test.test_inbound", "http")).lower()
        if self.inbound not in {"http", "socks"}:
            raise XrayTestError("test.test_inbound 只能是 http 或 socks")
        # 测法：latency 只测延迟，latency_speed 延迟 + 下载测速
        self.test_mode = str(config.get("test.test_mode", "latency_speed")).lower()
        if self.test_mode not in {"latency", "latency_speed"}:
            raise XrayTestError("test.test_mode 只能是 latency 或 latency_speed")
        self.speed_enabled = self.test_mode == "latency_speed"
        # 分段计时（写入 logs/timing.jsonl，并汇总到 run.log）
        self.timing_enabled = bool(config.get("test.timing", True))
        self.timing_path = config.resolve("log_dir") / "timing.jsonl"
        self.timing_records: list[dict[str, Any]] = []
        self._timing_lock = threading.Lock()
        self._http_ms: dict[int, float] = {}
        self._current_process: subprocess.Popen | None = None
        self._last_written_batch: tuple[int, float, float, int] | None = None
        self.kernels_started = 0
        self.last_timing_summary: dict[str, Any] = {}

    # ---- 进程与端口（批量共享内核）----
    def _launch(self, config_path: Path, work_dir: Path) -> tuple[subprocess.Popen, Any]:
        """启动一个承载整批节点入站的内核；`xray run -c` 失败时回退旧 CLI 写法。"""
        stderr_path = work_dir / "process.log"
        command_sets = [
            [str(self.exe), "run", "-c", str(config_path)],
            [str(self.exe), "-c", str(config_path)],
        ]
        last_log = ""
        for command in command_sets:
            stdout_handle = open(stderr_path, "ab", buffering=0)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(work_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                stdout_handle.close()
                raise KernelStartError(f"无法启动 Xray 内核：{exc}") from exc
            self.registry.register(process)
            # 只等 ~0.4s 判断命令写法是否立即失败；端口就绪由 _wait_ports 负责
            early_deadline = time.time() + 0.4
            while time.time() < early_deadline:
                if process.poll() is not None:
                    break
                time.sleep(self.poll_interval)
            if process.poll() is None:
                return process, stdout_handle
            self.registry.terminate(process)
            last_log = self._read_log(work_dir)
            try:
                stdout_handle.close()
            except OSError:
                pass
        raise KernelStartError(last_log[-1000:] or "Xray 内核启动后立即退出（未知原因）")

    @staticmethod
    def _port_ready(port: int) -> bool:
        if port <= 0:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _wait_ports(
        self, process: subprocess.Popen, ports: list[int], work_dir: Path, started: float
    ) -> None:
        """等到整批本地入站全部可连接；内核提前退出立即判失败并附带日志。"""
        pending = set(ports)
        deadline = started + self.startup_timeout
        while pending:
            if process.poll() is not None:
                log_text = self._read_log(work_dir)
                raise KernelStartError(
                    f"Xray 进程提前退出（退出码 {process.returncode}），日志：{log_text[-800:] or '无输出'}"
                )
            for port in list(pending):
                if self._port_ready(port):
                    pending.discard(port)
            if not pending:
                return
            if time.time() >= deadline:
                raise KernelStartError(
                    f"等待 Xray 本地入站端口超时（{len(pending)}/{len(ports)} 个未就绪，"
                    f"startup_timeout={self.startup_timeout}s）"
                )
            time.sleep(self.poll_interval)

    @staticmethod
    def _read_log(work_dir: Path) -> str:
        text = ""
        for name in ("error.log", "process.log"):
            path = work_dir / name
            if path.exists():
                try:
                    text += path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        return text

    # ---- HTTP 测试 ----
    def _proxy_map(self, port: int) -> dict[str, str]:
        if self.inbound == "socks":
            url = f"socks5h://127.0.0.1:{port}"
        else:
            url = f"http://127.0.0.1:{port}"
        return {"http": url, "https": url}

    def _measure_latency(self, session: requests.Session, proxies: dict[str, str]) -> tuple[float, list[float]]:
        url = str(self.config.get("test.latency_url", "https://www.gstatic.com/generate_204"))
        samples: list[float] = []
        errors: list[Exception] = []
        for _ in range(self.latency_samples):
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    proxies=proxies,
                    timeout=self.latency_timeout,
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                # 读完内容，确保握手与首包都计入
                response.content
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                samples.append(elapsed)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if not samples:
            if errors:
                raise errors[-1]
            raise XrayTestError("延迟测试没有采集到有效样本")
        if self.latency_mode == "average":
            return sum(samples) / len(samples), samples
        return min(samples), samples

    def _measure_speed(
        self,
        session: requests.Session,
        proxies: dict[str, str],
    ) -> tuple[float, int, float]:
        url = str(self.config.get("test.speed_url", "") or "").strip()
        if not url:
            return 0.0, 0, 0.0
        started = time.perf_counter()
        downloaded = 0
        try:
            with session.get(
                url,
                proxies=proxies,
                timeout=(self.request_timeout, self.speed_timeout),
                stream=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        downloaded += len(chunk)
                    elapsed = time.perf_counter() - started
                    if self.speed_max_bytes and downloaded >= self.speed_max_bytes:
                        break
                    if elapsed >= self.speed_max_seconds:
                        break
                    if not chunk and elapsed > self.speed_timeout:
                        break
        finally:
            elapsed = max(0.001, time.perf_counter() - started)
        if downloaded == 0:
            raise XrayTestError("测速文件下载 0 字节")
        mbps = downloaded * 8 / elapsed / 1_000_000
        return mbps, downloaded, elapsed

    def _classify_error(self, exc: Exception, log_text: str) -> tuple[str, str]:
        message = str(exc)
        lowered = message.lower()
        log_lower = log_text.lower()
        if isinstance(exc, KernelStartError):
            return "kernel_start_failed", "内核启动失败"
        if isinstance(exc, requests.exceptions.Timeout) or "timed out" in lowered:
            return "connection_timeout", "连接/读取超时"
        if isinstance(
            exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
        ) or any(word in lowered for word in ("connection aborted", "connection reset", "broken pipe", "10054")):
            return "network_broken", "网络中断"
        if any(word in lowered for word in ("ssl", "tls", "certificate", "handshake")):
            return "tls_failed", "TLS 握手或证书校验失败"
        if any(word in log_lower for word in ("tls", "certificate", "handshake")):
            return "tls_failed", "Xray 日志显示 TLS 失败"
        if any(word in log_lower for word in ("failed to dial", "connection refused", "i/o timeout")):
            return "connection_failed", "目标连接失败"
        if any(word in log_lower for word in ("invalid", "unexpected", "protocol", "eof")):
            return "protocol_error", "协议错误或服务端返回异常"
        if isinstance(exc, requests.exceptions.ProxyError) or "proxy" in lowered:
            return "proxy_connection_failed", "本地代理连接失败"
        if "http" in lowered and any(code in lowered for code in ("400", "403", "405", "502")):
            return "proxy_error", "代理返回异常 HTTP 状态"
        return "unknown_failed", message[:200] or "未知错误"

    # ---- 单节点结果 ----
    def _latency_task(self, node: VlessNode, port: int, work_dir: Path) -> NodeResult:
        """一个节点的延迟探测（在共享内核的线程池里执行）。"""
        session = requests.Session()
        session.trust_env = False
        started = time.perf_counter()
        try:
            for attempt in range(1, self.retries + 2):
                try:
                    latency, samples = self._measure_latency(session, self._proxy_map(port))
                    result = NodeResult(
                        node=node,
                        latency_ms=round(latency, 1),
                        latency_samples=[round(x, 1) for x in samples],
                        attempts=attempt,
                        status="延迟成功（未测速）" if not self.speed_enabled else "延迟成功（测速中）",
                        tested_at=datetime.now().isoformat(timespec="seconds"),
                    )
                    return result
                except Exception as exc:  # noqa: BLE001
                    if attempt <= self.retries:
                        self.logger.info("节点 %s 第 %s 次失败，准备重试", node.address, attempt)
                        continue
                    raise exc
            raise XrayTestError("延迟测试未执行")
        except Exception as exc:  # noqa: BLE001
            # 共享内核若已退出，优先归类为「内核退出」而不是节点网络问题
            if self._process_exited(work_dir):
                error_type, description = "kernel_exited", "内核进程退出"
                detail = f"{description}：{str(exc)[:160]}"
            else:
                log_text = self._read_log(work_dir)[-1500:]
                error_type, description = self._classify_error(exc, log_text)
                detail = f"{description}：{str(exc)[:160]}"
            self.logger.warning(
                "节点 %s:%s 测试失败 [%s] %s", node.address, node.port, error_type, exc
            )
            return NodeResult(
                node=node,
                status="失败",
                error_type=error_type,
                detail=detail,
                tested_at=datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            self._http_ms[node.index] = (time.perf_counter() - started) * 1000
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _speed_task(self, result: NodeResult, port: int) -> NodeResult:
        session = requests.Session()
        session.trust_env = False
        started = time.perf_counter()
        try:
            mbps, downloaded, _elapsed = self._measure_speed(session, self._proxy_map(port))
            result.downloaded_bytes = downloaded
            if mbps:
                result.speed_mbps = round(mbps, 3)
                result.status = "成功"
            else:
                result.status = "延迟成功（未测速）"
        except Exception as exc:  # noqa: BLE001
            result.status = "测速失败"
            result.error_type = "speed_failed"
            result.detail = f"延迟 {result.latency_ms} ms，但测速失败：{str(exc)[:160]}"
            self.logger.warning("节点 %s:%s 测速失败：%s", result.node.address, result.node.port, exc)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        return result

    def _process_exited(self, _work_dir: Path | None = None) -> bool:
        process = self._current_process
        return process is not None and process.poll() is not None

    # ---- 批量调度 ----
    def test_nodes(
        self,
        nodes: list[VlessNode],
        progress: Callable[[int, int, NodeResult], None] | None = None,
        update: Callable[[NodeResult], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[NodeResult]:
        """批量测试：每 kernel_batch 个节点共用一个 Xray 内核进程。

        延迟请求受 latency_concurrency 限制；测速请求受 speed_concurrency 限制，
        两者分开，避免测速互相抢带宽、同时让延迟并发可以开到 50。
        """
        if not nodes:
            return []
        started_wall = time.perf_counter()
        total = len(nodes)
        results: list[NodeResult] = []
        batch_total = (total + self.kernel_batch - 1) // self.kernel_batch
        for batch_no, offset in enumerate(range(0, total, self.kernel_batch), start=1):
            if should_stop and should_stop():
                self.logger.info("用户停止：已完成 %s/%s 个节点", len(results), total)
                break
            batch = nodes[offset : offset + self.kernel_batch]
            batch_results = self._run_batch(batch, batch_no, batch_total, total, progress, update)
            results.extend(batch_results)
        wall = time.perf_counter() - started_wall
        self._log_timing_summary(wall, total, len(results))
        return results

    def _run_batch(
        self,
        nodes: list[VlessNode],
        batch_no: int,
        batch_total: int,
        grand_total: int,
        progress: Callable[[int, int, NodeResult], None] | None,
        update: Callable[[NodeResult], None] | None,
    ) -> list[NodeResult]:
        started = time.perf_counter()
        ports: list[int] | None = None
        work_dir: Path | None = None
        process: subprocess.Popen | None = None
        stdout_handle = None
        results: list[NodeResult] = []
        setup_ms = 0.0
        teardown_ms = 0.0
        try:
            try:
                ports = self.allocator.allocate(len(nodes))
                t_ports = time.perf_counter() - started
                work_dir = _make_work_dir(self.temp_root)
                config_path = work_dir / "config.json"
                error_log = work_dir / "error.log"
                pool_config = build_xray_pool_config(
                    list(zip(nodes, ports)), str(error_log), self.inbound
                )
                config_path.write_text(
                    json.dumps(pool_config, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                t_config = time.perf_counter() - started
                process, stdout_handle = self._launch(config_path, work_dir)
                self._current_process = process
                t_launch = time.perf_counter() - started
                self._wait_ports(process, ports, work_dir, started + t_launch)
                t_ready = time.perf_counter() - started
                setup_ms = t_ready * 1000
                self.kernels_started += 1
                self.logger.debug(
                    "[timing] batch %s/%s nodes=%s ports=%.1fms dir=%.1fms config=%.1fms "
                    "launch=%.1fms ready_total=%.1fms",
                    batch_no, batch_total, len(nodes), t_ports * 1000,
                    t_ports * 1000,
                    (t_config - t_ports) * 1000, (t_launch - t_config) * 1000, t_ready * 1000,
                )
            except Exception as exc:  # noqa: BLE001
                # 整批内核起不来：全部标记为内核启动失败，并保留可诊断信息
                log_text = self._read_log(work_dir)[-1000:] if work_dir is not None else ""
                error_type = (
                    "kernel_exited"
                    if isinstance(exc, KernelStartError) and "退出" in str(exc)
                    else "kernel_start_failed"
                )
                description = "内核进程退出" if error_type == "kernel_exited" else "内核启动失败"
                message = f"{description}：{str(exc)[:160]}"
                for node in nodes:
                    result = NodeResult(
                        node=node,
                        status="失败",
                        error_type=error_type,
                        detail=message,
                        tested_at=datetime.now().isoformat(timespec="seconds"),
                    )
                    results.append(result)
                    if progress:
                        try:
                            progress(len(results), grand_total, result)
                        except Exception:  # noqa: BLE001
                            pass
                self.logger.error(
                    "共享内核启动失败（批次 %s/%s，%s 个节点）：%s | 日志：%s",
                    batch_no, batch_total, len(nodes), exc, log_text[-300:],
                )
                return results

            # ---- 延迟阶段 ----
            latencies: list[NodeResult] = []
            worker_count = min(self.latency_concurrency, len(nodes))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="xlat") as pool:
                futures = {
                    pool.submit(self._latency_task, node, port, work_dir): node
                    for node, port in zip(nodes, ports)
                }
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        self.logger.exception("测试任务异常：%s", exc)
                        result = NodeResult(
                            node=node,
                            status="失败",
                            error_type="internal_error",
                            detail=str(exc)[:200],
                            tested_at=datetime.now().isoformat(timespec="seconds"),
                        )
                    latencies.append(result)

            ports_by_index = {node.index: port for node, port in zip(nodes, ports)}
            if not self.speed_enabled:
                results = latencies
                for result in results:
                    self._record_timing(result)
                    if progress:
                        try:
                            progress(len(results), grand_total, result)
                        except Exception:  # noqa: BLE001
                            pass
                return results

            # ---- 测速阶段（只有延迟成功的节点才测，带宽受 speed_concurrency 限制）----
            results = latencies
            candidates = [r for r in results if r.latency_ms is not None]
            if candidates:
                with ThreadPoolExecutor(
                    max_workers=min(self.speed_concurrency, len(candidates)),
                    thread_name_prefix="xspeed",
                ) as pool:
                    futures = {
                        pool.submit(self._speed_task, result, ports_by_index[result.node.index]): result
                        for result in candidates
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        self._record_timing(result)
                        if update:
                            try:
                                update(result)
                            except Exception:  # noqa: BLE001
                                pass
            for result in results:
                if result.latency_ms is None:
                    self._record_timing(result)
                    if update:
                        try:
                            update(result)
                        except Exception:  # noqa: BLE001
                            pass
            return results
        finally:
            # 必须等延迟/测速全部结束后再回收内核
            self._current_process = None
            if process is not None:
                t_before = time.perf_counter()
                self.registry.terminate(process)
                teardown_ms += (time.perf_counter() - t_before) * 1000
            if stdout_handle is not None:
                try:
                    stdout_handle.close()
                except Exception:  # noqa: BLE001
                    pass
            if ports is not None:
                self.allocator.release(ports)
            if work_dir is not None:
                t_before = time.perf_counter()
                if bool(self.config.get("test.keep_temp_on_error", False)):
                    self.logger.info("保留临时目录：%s", work_dir)
                else:
                    shutil.rmtree(work_dir, ignore_errors=True)
                teardown_ms += (time.perf_counter() - t_before) * 1000
            self._last_written_batch = (batch_no, setup_ms, teardown_ms, len(nodes))

    def _record_timing(self, result: NodeResult) -> None:
        http_ms = self._http_ms.pop(result.node.index, None)
        row: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "node_index": result.node.index,
            "address": f"{result.node.address}:{result.node.port}",
            "mode": self.test_mode,
            "status": result.status,
            "error_type": result.error_type,
            "latency_ms": result.latency_ms,
            "http_ms": round(http_ms, 1) if http_ms is not None else None,
            "speed_mbps": result.speed_mbps,
        }
        if self._last_written_batch:
            batch_no, setup_ms, teardown_ms, count = self._last_written_batch
            row["batch"] = batch_no
            row["setup_share_ms"] = round(setup_ms / max(1, count), 2)
            row["teardown_share_ms"] = round(teardown_ms / max(1, count), 2)
        self.timing_records.append(row)
        if self.timing_enabled:
            try:
                with self._timing_lock:
                    self.timing_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.timing_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass
        self.logger.debug(
            "[timing] node=%s status=%s setup_share=%sms http=%sms",
            result.node.index, result.status, row.get("setup_share_ms", "-"),
            row.get("http_ms", "-"),
        )

    def _log_timing_summary(self, wall: float, total: int, done: int) -> None:
        http_values = [
            float(row["latency_ms"])
            for row in self.timing_records
            if row.get("latency_ms") is not None
        ]

        def _pct(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))
            return ordered[idx]

        summary = {
            "mode": self.test_mode,
            "nodes": total,
            "done": done,
            "kernels": self.kernels_started,
            "latency_concurrency": self.latency_concurrency,
            "speed_concurrency": self.speed_concurrency,
            "kernel_batch": self.kernel_batch,
            "wall_seconds": round(wall, 2),
            "throughput_per_second": round(done / wall, 2) if wall > 0 else 0,
            "http_p50_ms": round(_pct(http_values, 0.5), 1),
            "http_p95_ms": round(_pct(http_values, 0.95), 1),
            "setup_avg_ms": round(
                sum(r.get("setup_share_ms", 0) for r in self.timing_records)
                / max(1, len(self.timing_records)),
                1,
            ),
        }
        self.last_timing_summary = summary
        self.logger.info(
            "[timing] mode=%s nodes=%s kernels=%s latency_concurrency=%s speed_concurrency=%s "
            "wall=%.1fs throughput=%.2f nodes/s http_p50=%.0fms http_p95=%.0fms setup_share_avg=%.1fms",
            summary["mode"], total, summary["kernels"], self.latency_concurrency,
            self.speed_concurrency, wall, summary["throughput_per_second"],
            summary["http_p50_ms"], summary["http_p95_ms"], summary["setup_avg_ms"],
        )


class SimulatedTester:
    """--selftest 用的本地模拟内核：不访问网络，不启动真实进程。"""

    def __init__(self, seed: int = 2026):
        self.seed = seed

    def _result_for(self, node: VlessNode) -> NodeResult:
        digest = int(hashlib.sha256(node.link.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(digest + self.seed)
        checked_at = datetime.now().isoformat(timespec="seconds")
        if node.index % 4 == 0:
            return NodeResult(
                node=node,
                status="失败",
                error_type="connection_timeout",
                detail="模拟：目标连接超时",
                attempts=1,
                tested_at=checked_at,
            )
        latency = round(rng.uniform(35, 480), 1)
        if node.index % 6 == 0:
            return NodeResult(
                node=node,
                latency_ms=latency,
                latency_samples=[latency],
                status="测速失败",
                error_type="speed_failed",
                detail="模拟：延迟正常，下载测速失败",
                attempts=1,
                tested_at=checked_at,
            )
        speed = round(rng.uniform(0.8, 35.0), 3)
        downloaded = int(speed * 1_000_000 / 8 * rng.uniform(3, 8))
        return NodeResult(
            node=node,
            latency_ms=latency,
            latency_samples=[latency],
            speed_mbps=speed,
            downloaded_bytes=downloaded,
            status="成功",
            attempts=1,
            tested_at=checked_at,
        )


def simulate_results(nodes: list[VlessNode], progress: Callable[[int, int, NodeResult], None] | None = None) -> list[NodeResult]:
    """模拟测试入口（selftest 专用）。"""
    tester = SimulatedTester()
    results: list[NodeResult] = []
    for idx, node in enumerate(nodes, start=1):
        result = tester._result_for(node)  # noqa: SLF001
        results.append(result)
        if progress:
            progress(idx, len(nodes), result)
    return results
