#!/usr/bin/env python
"""项目入口：python main.py 进入中文菜单；--web 启动本地 Web UI；--selftest 运行全链路自检。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vless_tool.app import Application
from vless_tool.config import Config, ConfigError, find_config_path
from vless_tool.logging_setup import setup_logging
from vless_tool.selftest import SelfTestFailure, run_selftest
from vless_tool.xray import cleanup_all_processes


def _config_example_candidates(config_path: Path) -> list[Path]:
    candidates = [
        config_path.with_name("config.example.json"),
        Path(__file__).resolve().parent / "config.example.json",
    ]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / "config.example.json")
    return candidates


def _ensure_config(config_path: Path) -> Path | None:
    """config.json 缺失时自动生成；成功返回配置文件路径，失败返回 None。"""
    if config_path.exists():
        return config_path
    for candidate in _config_example_candidates(config_path):
        if not candidate.is_file():
            continue
        import shutil

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, config_path)
        except OSError as exc:
            print(f"[初始化] 自动生成 config.json 失败：{exc}")
            print(f"[初始化] 请手动把 {candidate.name} 复制为 {config_path}")
            return None
        print(f"[初始化] 已生成默认配置：{config_path}")
        return config_path
    print("[启动失败] 未找到 config.json，且包内/目录中没有 config.example.json，无法自动生成。")
    print("请手动创建 config.json（参考 config.example.json）。")
    return None


def _prepare_console() -> None:
    """Windows 控制台尽量使用 UTF-8，避免中文/符号乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="v2rayN VLESS 节点批量生成与 Xray 真延迟/测速工具",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径，默认使用 main.py 同目录下的 config.json",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="自检模式：用 sample_data 与模拟内核跑通 提取→生成→测试→导出，不访问网络",
    )
    parser.add_argument(
        "--xray-check",
        action="store_true",
        help="检查 Xray 内核路径/版本，并用最小 socks 入站做一次真实启动验证后退出",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动本地 Web UI（默认仅监听 127.0.0.1:8765），CLI 功能仍然可用",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web UI 监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Web UI 端口，默认 8765")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Web UI 启动时不自动打开浏览器",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    args = build_parser().parse_args(argv)

    config_path = find_config_path(args.config)
    ensured = _ensure_config(config_path)
    if ensured is None:
        print("[启动失败] 没有可用的 config.json，且无法自动生成。")
        return 2
    config_path = ensured
    try:
        config = Config.load(config_path)
    except ConfigError as exc:
        print(f"[启动失败] {exc}")
        print("请检查 config.json 是否为合法 JSON，或使用 --config 指定其他文件。")
        return 2

    try:
        config.ensure_dirs()
        logger = setup_logging(config.resolve("log_dir"))
    except OSError as exc:
        print(f"[启动失败] 无法创建下载/导出/临时/日志目录：{exc}")
        print("请检查 config.json 中的路径是否可写。")
        return 2

    logger.info("启动 vless_tool，配置：%s", config.path)
    code = 0
    try:
        if args.selftest:
            code = run_selftest(config)
        elif args.web:
            from vless_tool.webui import run_server

            code = run_server(
                config,
                logger,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
        elif args.xray_check:
            from vless_tool.xray import probe_kernel

            probe = probe_kernel(config, logger)
            print(
                f"[Xray] 结论：版本 {probe.version.split()[1] if len(probe.version.split()) > 1 else probe.version} | "
                f"监听 {probe.listened} | 清理 {probe.cleaned}"
            )
            code = 0 if (probe.listened and probe.cleaned) else 1
        else:
            Application(config).run()
    except SelfTestFailure as exc:
        print(f"\n自检失败：{exc}")
        logger.exception("自检失败")
        code = 1
    except KeyboardInterrupt:
        print("\n已中断，正在清理 Xray 子进程……")
        code = 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("程序异常退出")
        print(f"\n[异常] {exc}")
        print(f"详细信息见日志：{config.resolve('log_dir') / 'run.log'}")
        code = 1
    finally:
        cleanup_all_processes()
        logger.info("程序结束，退出码 %s", code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
