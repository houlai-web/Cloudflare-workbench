"""配置加载、路径解析与全局配置访问。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_NAME = "config.json"


class ConfigError(RuntimeError):
    """配置文件本身不合法时抛出。"""


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """把用户配置合并到内置默认值上，避免旧配置缺字段导致崩溃。"""
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


DEFAULT_CONFIG: dict[str, Any] = {
    "xray_path": "./app/xray/xray.exe",
    "download_dir": "./downloads",
    "work_dir": "./app/tmp/work",
    "export_dir": "./downloads",
    "temp_dir": "./app/tmp",
    "log_dir": "./app/logs",
    "regions": [
        {"name": "日本", "code": "JP"},
        {"name": "新加坡", "code": "SG"},
        {"name": "美国", "code": "US"},
        {"name": "香港", "code": "HK"},
        {"name": "台湾", "code": "TW"},
        {"name": "韩国", "code": "KR"},
        {"name": "德国", "code": "DE"},
        {"name": "荷兰", "code": "NL"},
        {"name": "英国", "code": "GB"},
        {"name": "加拿大", "code": "CA"},
    ],
    "fofa": {
        "_readme": "仅用于生成 FOFA 查询语句与搜索链接，不含账号 / API Key / Cookie。",
        "query_template": 'country="{code}" && protocol="vless"',
    },
    "extract": {
        "port_allowlist": [],
        "port_blocklist": [22, 25, 135, 137, 138, 139, 445, 3389],
        "exclude_private_ip": True,
        "max_items": 0,
        "default_port": 443,
        "preview": 10,
    },
    "generate": {
        "sample_vless": "",
        "name_template": "{region}-{index:03d}-{latency}",
        "max_nodes": 50,
        "latency_placeholder": "待测",
        "strict_uuid": True,
    },
    "test": {
        "concurrency": 50,
        # 延迟并发与测速并发分开：延迟只受套接字/网络限制，测速受带宽限制
        "latency_concurrency": 50,
        "speed_concurrency": 5,
        # 一个内核进程最多承载多少节点的入站/出站
        "kernel_batch": 50,
        "latency_timeout": 6,
        "poll_interval": 0.03,
        "timing": True,
        "startup_timeout": 8,
        "latency_url": "https://www.gstatic.com/generate_204",
        "latency_samples": 2,
        "latency_sample_mode": "best",
        "request_timeout": 10,
        "speed_url": "https://speed.cloudflare.com/__down?bytes=10000000",
        "speed_max_bytes": 8000000,
        "speed_max_seconds": 15,
        "speed_timeout": 20,
        "retries": 0,
        "test_inbound": "http",
        "test_mode": "latency_speed",
        "port_start": 20000,
        "port_end": 40000,
        "keep_temp_on_error": False,
    },
    "publish": {
        "_readme": "把内容上传到 CF 文本主机（同 上传测速结果到CF.bat）。域名/token/默认文件名可在页面修改，上传成功后写回本段。",
        "enabled": True,
        "domain": "txt.example.com",
        "token": "",
        # 生成内容格式：ip_port_country = `IP:端口#国旗-国家`（默认）；vless = 分享链接
        "content_mode": "ip_port_country",
        # 0 = 不截断，上传全部内容（CF.bat 硬编码只取前 65 行，这里默认不再照抄）
        "max_lines": 0,
        # 页面「发布」面板默认文件名：{date}=YYYYMMDD；{timestamp}=YYYYMMDD_HHMMSS
        "filename_default": "{date}.txt",
        "timeout": 15
    },
    "output": {
        "csv_encoding": "utf-8-sig",
    },
}


def _program_root() -> Path:
    """程序根目录：PyInstaller 打包后为 exe 所在目录；源码运行时为 app/ 的父目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent  # vless_tool/config.py -> app -> root


def find_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """按优先级查找 config.json：显式参数 → 环境变量 VLESS_CONFIG → exe/仓库根目录 → app/ → cwd。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("VLESS_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    root = _program_root()
    app_root = root / "app"
    candidates = [
        root / DEFAULT_CONFIG_NAME,          # 程序根目录（config.json / exe 同级）
        app_root / DEFAULT_CONFIG_NAME,      # app/ 内部
        Path.cwd() / DEFAULT_CONFIG_NAME,    # 当前工作目录
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0]


class Config:
    """封装配置字典，统一处理相对路径与目录创建。"""

    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        # 相对路径统一以程序根目录（config.json/exe 所在目录）为基准
        self.base_dir = _program_root()

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg_path = find_config_path(path)
        if not cfg_path.exists():
            raise ConfigError(f"找不到配置文件：{cfg_path}")
        try:
            raw = cfg_path.read_text(encoding="utf-8-sig")
            incoming = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件 JSON 格式错误：第 {exc.lineno} 行，{exc.msg}") from exc
        except OSError as exc:
            raise ConfigError(f"读取配置文件失败：{exc}") from exc
        if not isinstance(incoming, dict):
            raise ConfigError("配置文件根节点必须是 JSON 对象")
        data = _deep_merge(DEFAULT_CONFIG, incoming)
        return cls(data, cfg_path)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按 a.b.c 形式读取配置。"""
        node: Any = self.data
        for key in dotted_key.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def resolve(self, dotted_key: str) -> Path:
        """把配置中的路径解析为绝对路径（相对于配置文件所在目录）。"""
        value = str(self.get(dotted_key, "") or "").strip()
        path = Path(os.path.expandvars(os.path.expanduser(value)))
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()

    def ensure_dirs(self) -> None:
        for key in ("download_dir", "work_dir", "export_dir", "temp_dir", "log_dir"):
            self.resolve(key).mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        """原子写回 config.json，避免中途崩溃留下半个文件。"""
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self.path)

    @property
    def xray_path(self) -> Path | None:
        raw = str(self.get("xray_path", "") or "").strip()
        if not raw:
            return None
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()

    @property
    def regions(self) -> list[dict[str, str]]:
        regions = self.get("regions", [])
        if not isinstance(regions, list):
            return []
        return [r for r in regions if isinstance(r, dict) and r.get("code")]

    def region_name(self, code: str) -> str:
        for item in self.regions:
            if str(item.get("code", "")).upper() == code.upper():
                return str(item.get("name") or code)
        return code.upper()
