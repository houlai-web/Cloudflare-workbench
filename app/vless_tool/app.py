"""中文命令行菜单与五步业务流程。"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import Config
from .extractor import (
    Endpoint,
    ExtractResult,
    ExtractStats,
    extract_file,
    save_endpoints,
)
from .fofa import FofaError, fofa_search_url, import_local_file, import_text, qbase64_encode
from .logging_setup import get_logger
from .nodes import (
    NodeGenerationError,
    VlessNode,
    generate_nodes,
    parse_vless_link,
)
from .results import (
    export_links,
    export_node_links,
    format_table,
    save_results,
    sort_for_autosave,
    sorted_results,
)
from .services import extract_and_save
from .xray import (
    NodeResult,
    XrayTester,
    XrayTestError,
    cleanup_all_processes,
    resolve_xray_executable,
)


SUPPORTED_EXTENSIONS = {".txt", ".csv", ".json", ".html", ".htm"}


@dataclass
class AppState:
    endpoints: list[Endpoint] = field(default_factory=list)
    nodes: list[VlessNode] = field(default_factory=list)
    results: list[NodeResult] = field(default_factory=list)
    region_code: str = ""
    region_name: str = ""
    extraction_files: list[str] = field(default_factory=list)


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt}" + (f" [{default}]" if default else "")
    try:
        value = input(f"{shown}: ").strip()
    except EOFError:
        raise KeyboardInterrupt
    return value or default


def _ask_int(prompt: str, default: int, minimum: int, maximum: int) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(f"请输入 {minimum}-{maximum} 之间的整数。")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"请输入 {minimum}-{maximum} 之间的整数。")


def _confirm(prompt: str, default: bool = True) -> bool:
    raw = _ask(prompt + (" [Y/n]" if default else " [y/N]"), "").lower()
    if not raw:
        return default
    return raw in {"y", "yes", "是", "1"}


def _meta_for_file(path: Path) -> dict[str, Any]:
    candidates = [
        Path(str(path) + ".meta.json"),
        path.with_suffix(".meta.json"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return {}


def _region_for_file(path: Path, config: Config) -> str:
    meta = _meta_for_file(path)
    code = str(meta.get("country_code", "") or "").strip().upper()
    if code:
        return config.region_name(code)
    return ""


def _download_files(config: Config) -> list[Path]:
    directory = config.resolve("download_dir")
    if not directory.exists():
        return []
    files = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.name.endswith(".meta.json") or path.name.endswith(".raw.json"):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _parse_selection(raw: str, length: int) -> list[int]:
    """解析 1,3 或 1-5 形式的序号选择，返回 0 基索引。"""
    selected: set[int] = set()
    for part in re.split(r"[,，;；\s]+", raw.strip()):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for idx in range(start, end + 1):
                if 1 <= idx <= length:
                    selected.add(idx - 1)
        else:
            try:
                idx = int(part)
            except ValueError:
                continue
            if 1 <= idx <= length:
                selected.add(idx - 1)
    return sorted(selected)


def _status_line(state: AppState) -> str:
    region = state.region_name or state.region_code or "未选择"
    return (
        f"当前状态：端点 {len(state.endpoints)} 个 | 节点 {len(state.nodes)} 个 | "
        f"测试结果 {len(state.results)} 条 | 地区 {region}"
    )


class Application:
    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger("menu")
        self.state = AppState()

    # ------------------------------------------------------------------ 菜单
    def run(self) -> None:
        self._banner()
        while True:
            print("\n" + "=" * 64)
            print("主菜单")
            print("=" * 64)
            print(_status_line(self.state))
            print("1. 生成 FOFA 查询语句并打开浏览器（无需凭据）")
            print("2. 选择下载文件并提取 IP:端口（支持手动导入）")
            print("3. 根据示例 vless:// 批量生成节点")
            print("4. 使用 Xray 内核测试真延迟与下载速度")
            print("5. 查看结果、导出 v2rayN / 重新测试")
            print("0. 退出")
            choice = _ask("请选择", "0")
            try:
                if choice == "1":
                    self._fofa_menu()
                elif choice == "2":
                    self._extract_flow()
                elif choice == "3":
                    self._generate_flow()
                elif choice == "4":
                    self._test_flow()
                elif choice == "5":
                    self._results_flow()
                elif choice in {"0", "q", "Q", "exit"}:
                    print("已退出。")
                    break
                else:
                    print("无效选项，请重新选择。")
            except KeyboardInterrupt:
                print("\n检测到中断，正在清理 Xray 子进程……")
                cleanup_all_processes()
                print("已清理，退出。")
                break
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("主菜单操作异常：%s", exc)
                print(f"\n[错误] 操作失败：{exc}")
                print("详细信息已写入 logs/run.log。")

    @staticmethod
    def _banner() -> None:
        print("=" * 64)
        print(" v2rayN VLESS 节点批量生成 / Xray 真延迟与测速工具")
        print(f" v{__version__}  |  仅用于你有权测试的目标与数据源")
        print("=" * 64)

    # --------------------------------------------------------------- 第 1 步
    def _fofa_menu(self) -> None:
        while True:
            print("\n------- 第 1 步：生成 FOFA 查询语句 -------")
            print("1. 内置地区生成查询语句（复制/打开浏览器）")
            print("2. 自定义国家代码生成查询语句")
            print("3. 手动粘贴网页/API 结果（无需凭据）")
            print("4. 导入本地保存的 FOFA 页面/文件")
            print("0. 返回主菜单")
            choice = _ask("请选择", "0")
            if choice == "1":
                self._statement_with_region()
            elif choice == "2":
                self._statement_custom_region()
            elif choice == "3":
                self._paste_import()
            elif choice == "4":
                self._local_import()
            elif choice == "0":
                return
            else:
                print("无效选项。")

    def _select_builtin_region(self) -> tuple[str, str] | None:
        regions = self.config.regions
        if not regions:
            print("配置中的 regions 为空，请使用自定义国家代码。")
            return None
        print("\n内置地区：")
        for idx, region in enumerate(regions, start=1):
            print(f"  {idx}. {region.get('name')} ({str(region.get('code', '')).upper()})")
        choice = _ask("选择地区序号（0=返回）", "0")
        if choice == "0":
            return None
        try:
            idx = int(choice)
        except ValueError:
            print("无效序号。")
            return None
        if not 1 <= idx <= len(regions):
            print("序号超出范围。")
            return None
        item = regions[idx - 1]
        code = str(item.get("code", "")).upper()
        return code, str(item.get("name") or code)

    def _statement_with_region(self) -> None:
        selected = self._select_builtin_region()
        if not selected:
            return
        code, name = selected
        self._show_statement(code, name)

    def _statement_custom_region(self) -> None:
        code = _ask("输入国家/地区代码（如 JP/US/HK，可自定义）", "")
        if not code:
            print("国家代码不能为空。")
            return
        code = code.upper()
        name = _ask("用于节点命名的地区名称", code)
        self._show_statement(code, name, custom_query=True)

    def _show_statement(self, code: str, name: str, custom_query: bool = False) -> None:
        port = _ask("可选：附加端口条件（留空表示不限端口）", "")
        template = str(self.config.get("fofa.query_template", ""))
        try:
            if custom_query:
                query = _ask("输入完整 FOFA 查询语句", build_query_safe(template, code, port))
            else:
                query = build_query_safe(template, code, port)
        except FofaError as exc:
            print(f"[配置错误] {exc}")
            return
        if not query.strip():
            print("查询语句为空。")
            return
        url = fofa_search_url(query)
        self.state.region_code = code
        self.state.region_name = name
        print(f"\n[查询语句] {query}")
        print(f"[搜索链接] {url}")
        print(f"[qbase64] {qbase64_encode(query)}")
        if _confirm("是否在默认浏览器打开该搜索链接？", True):
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                print(f"[提示] 打开浏览器失败：{exc}，请手动复制上面的链接。")
        print("提示：浏览器中搜索完成后，用「3. 手动粘贴」或「4. 导入本地文件」把结果导入。")

    def _paste_import(self, code: str = "") -> None:
        if not code:
            code = _ask("可选：国家代码（用于节点命名，可留空）", "").upper()
        print("\n请粘贴 FOFA 网页/API 结果（可包含 IP:端口、CSV、JSON 或 HTML）。")
        print("粘贴完成后，在新行单独输入 END 并回车：")
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "END":
                break
            lines.append(line)
        text = "\n".join(lines)
        try:
            path = import_text(self.config, text, code=code, note="手动粘贴")
            self.state.region_code = code
            if code:
                self.state.region_name = self.config.region_name(code)
            print(f"[完成] 已导入 {len(lines)} 行。下一步：菜单 2 提取 IP:端口。")
            self.logger.info("手动粘贴导入文件：%s", path)
        except FofaError as exc:
            print(f"[导入失败] {exc}")

    def _local_import(self) -> None:
        raw = _ask("输入本地 HTML/TXT/CSV/JSON 文件完整路径", "")
        if not raw:
            return
        path = Path(raw).expanduser()
        code = _ask("可选：国家代码（用于节点命名，可留空）", "").upper()
        try:
            dest = import_local_file(self.config, path, code=code)
            if code:
                self.state.region_code = code
                self.state.region_name = self.config.region_name(code)
            print(f"[完成] 已导入：{dest.name}。下一步：菜单 2 提取 IP:端口。")
        except FofaError as exc:
            print(f"[导入失败] {exc}")

    # --------------------------------------------------------------- 第 2 步
    def _extract_flow(self) -> None:
        print("\n------- 第 2 步：选择下载文件并提取 IP:端口 -------")
        files = _download_files(self.config)
        if not files:
            print("下载目录中没有可解析文件。")
            print("请先执行第 1 步，或选择手动导入（输入 m 指定任意文件路径）。")
        else:
            print(f"下载目录：{self.config.resolve('download_dir')}\n")
            for idx, path in enumerate(files, start=1):
                meta = _meta_for_file(path)
                query = str(meta.get("query", "") or "")
                size_kb = path.stat().st_size / 1024
                extra = f" | 来源查询: {query[:50]}" if query else ""
                print(f"  {idx}. {path.name}  ({size_kb:.1f} KB){extra}")

        raw = _ask("选择文件序号（支持 1,3 / 1-5；m=手动输入路径；0=返回）", "0")
        if raw == "0":
            return
        paths: list[Path] = []
        if raw.lower() == "m":
            manual = _ask("输入文件完整路径", "")
            if not manual:
                return
            path = Path(manual.strip('"').expanduser())
            if not path.exists() or not path.is_file():
                print(f"文件不存在：{path}")
                return
            paths = [path]
        else:
            indices = _parse_selection(raw, len(files))
            if not indices:
                print("未选择有效文件。")
                return
            paths = [files[i] for i in indices]

        country_values: set[str] = set()
        for path in paths:
            country = _region_for_file(path, self.config)
            if country:
                country_values.add(country)
        try:
            outcome = extract_and_save(
                self.config,
                paths,
                country=next(iter(country_values)) if len(country_values) == 1 else "",
                logger=self.logger,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n[提取失败] {exc}")
            return
        for warning in outcome.warnings:
            print(f"[跳过] {warning}")

        endpoints = outcome.endpoints
        stats = outcome.stats
        files_used = outcome.files
        if not endpoints:
            print("没有提取到有效 host:port。可能原因：")
            print("  - 文件格式不在支持范围（txt/csv/json/html）")
            print("  - 端口被 extract.port_blocklist 过滤")
            print("  - 私有/本地地址被排除（可调整 extract.exclude_private_ip）")
            return

        max_items = int(self.config.get("extract.max_items", 0) or 0)
        if max_items and len(endpoints) > max_items:
            endpoints = endpoints[:max_items]
        self.state.endpoints = endpoints
        self.state.extraction_files = files_used
        if len(country_values) == 1:
            self.state.region_name = next(iter(country_values))
            self.state.region_code = ""
        elif not self.state.region_name:
            self.state.region_name = ""
        print(f"\n[提取完成] 去重后有效节点：{len(endpoints)} 条")
        print(f"[统计] {stats.summary()}")
        preview = max(1, int(self.config.get("extract.preview", 10)))
        print(f"\n前 {min(preview, len(endpoints))} 条预览：")
        for ep in endpoints[:preview]:
            print("  " + str(ep) + (f"  [{ep.country}]" if ep.country else ""))
        if outcome.saved_txt and outcome.saved_json:
            print(f"\n[保存] 中间文件：{outcome.saved_txt}")
            print(f"[保存] 元数据：{outcome.saved_json}")
        print("下一步：菜单 3 生成 VLESS 节点。")

    # --------------------------------------------------------------- 第 3 步
    def _read_sample_link(self) -> str:
        default = str(self.config.get("generate.sample_vless", "") or "").strip()
        if default:
            print(f"当前配置示例：{default[:100]}{'...' if len(default) > 100 else ''}")
        raw = _ask("输入示例 vless:// 链接（回车使用配置；@路径 读取文件）", "")
        if raw.startswith("@"):
            path = Path(raw[1:].strip()).expanduser()
            if not path.exists():
                raise NodeGenerationError(f"示例文件不存在：{path}")
            for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if line.strip().startswith("vless://"):
                    return line.strip()
            raise NodeGenerationError(f"文件中没有 vless:// 链接：{path}")
        if raw.startswith("vless://"):
            return raw
        if not default:
            raise NodeGenerationError(
                "没有示例链接。请粘贴一条完整的 vless:// 链接，"
                "或在 config.json 的 generate.sample_vless 中配置。"
            )
        return default

    def _generate_flow(self) -> None:
        print("\n------- 第 3 步：根据示例批量生成 VLESS 节点 -------")
        if not self.state.endpoints:
            print("当前没有已提取的 IP:端口，请先执行第 2 步。")
            return
        print(f"可用端点：{len(self.state.endpoints)} 个")
        try:
            sample = self._read_sample_link()
            # 先单独校验，错误时立即给出缺少的参数
            parse_vless_link(
                sample,
                strict_uuid=bool(self.config.get("generate.strict_uuid", True)),
            )
        except NodeGenerationError as exc:
            print(f"\n[示例校验失败]\n{exc}")
            return

        default_limit = min(
            len(self.state.endpoints),
            int(self.config.get("generate.max_nodes", 50) or len(self.state.endpoints)),
        )
        limit = _ask_int("生成数量上限", max(1, default_limit), 1, len(self.state.endpoints))
        default_region = self.state.region_name or self.state.region_code or "NODE"
        region = _ask("无来源地区信息时使用的节点地区名称", default_region)
        if not region:
            region = "NODE"
        template = str(self.config.get("generate.name_template", "{region}-{index:03d}-{latency}"))
        placeholder = str(self.config.get("generate.latency_placeholder", "待测"))
        try:
            nodes = generate_nodes(
                self.state.endpoints,
                sample,
                region,
                limit=limit,
                name_template=template,
                latency_placeholder=placeholder,
                strict_uuid=bool(self.config.get("generate.strict_uuid", True)),
            )
        except NodeGenerationError as exc:
            print(f"\n[生成失败]\n{exc}")
            return
        if not nodes:
            print("没有生成任何节点。")
            return
        self.state.nodes = nodes
        self.state.results = []
        self.state.region_name = region
        print(f"\n[生成完成] 共 {len(nodes)} 个节点；协议参数已保留，仅替换地址与端口。")
        print("前 3 条预览：")
        for node in nodes[:3]:
            print(f"  {node.name}: {node.link}")
        print("\n下一步：菜单 4 使用 Xray 测试。若暂无内核，可先在菜单 5 导出未测试链接。")

    # --------------------------------------------------------------- 第 4 步
    def _test_flow(self) -> None:
        print("\n------- 第 4 步：Xray 真延迟与下载速度测试 -------")
        if not self.state.nodes:
            print("当前没有已生成节点，请先执行第 3 步。")
            return
        try:
            exe = resolve_xray_executable(self.config)
        except XrayTestError as exc:
            print(f"\n[无法测试] {exc}")
            print("\n准备内核的方法：")
            print("  1. 从 Xray-core 官方 Release 下载 Windows 64 位压缩包；")
            print("  2. 解压到无中文/空格更稳妥的目录（本工具也支持含空格路径）；")
            print("  3. 把 xray.exe 的完整路径填入 config.json 的 xray_path；")
            print("  4. 也可把 v2rayN 自带的 xray.exe 路径填进去。")
            print("在此之前，可在菜单 5 导出未测试的 vless 链接。")
            return

        test_cfg = self.config
        print(f"内核：{exe}")
        print(
            f"并发：{test_cfg.get('test.concurrency')} | 启动超时：{test_cfg.get('test.startup_timeout')}s | "
            f"延迟取样：{test_cfg.get('test.latency_samples')} 次"
        )
        print(f"延迟目标：{test_cfg.get('test.latency_url')}")
        print(
            f"测速目标：{test_cfg.get('test.speed_url')} | "
            f"上限 {test_cfg.get('test.speed_max_bytes')} 字节 / {test_cfg.get('test.speed_max_seconds')} 秒"
        )
        if not _confirm(f"开始测试 {len(self.state.nodes)} 个节点？", True):
            return

        print("\n测试中，请稍候（每完成一个节点会打印一行）……")
        progress_lock = threading.Lock()
        started = time.time()

        def progress(done: int, total: int, result: NodeResult) -> None:
            with progress_lock:
                latency = f"{result.latency_ms:.0f}ms" if result.latency_ms is not None else "-"
                speed = f"{result.speed_mbps:.2f}Mbps" if result.speed_mbps is not None else "-"
                print(
                    f"[{done}/{total}] {result.node.address}:{result.node.port} "
                    f"延迟={latency} 速度={speed} 状态={result.status}"
                    + (f" ({result.error_type})" if result.error_type else "")
                )

        try:
            tester = XrayTester(self.config, self.logger)
            results = tester.test_nodes(self.state.nodes, progress=progress)
        except KeyboardInterrupt:
            cleanup_all_processes()
            print("\n[中断] 已清理所有 Xray 子进程。")
            return
        except XrayTestError as exc:
            print(f"\n[测试失败] {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("测试流程异常")
            print(f"\n[测试异常] {exc}")
            cleanup_all_processes()
            return

        self.state.results = sorted_results(results, "latency")
        elapsed = time.time() - started
        print(f"\n[测试完成] 用时 {elapsed:.1f} 秒。按延迟排序：")
        print(format_table(self.state.results))
        self._save_results(
            silent=False,
            mode=str(self.config.get("test.test_mode", "latency_speed") or "latency_speed"),
        )
        print("\n可继续在菜单 5 中导出 v2rayN 链接或重新测试。")

    def _save_results(
        self, silent: bool = False, mode: str | None = None
    ) -> tuple[Path, Path] | None:
        if not self.state.results:
            if not silent:
                print("没有测试结果可保存。")
            return None
        try:
            saved = (
                sort_for_autosave(self.state.results, mode)
                if mode
                else self.state.results
            )
            csv_path, json_path = save_results(
                saved,
                self.config.resolve("export_dir"),
                csv_encoding=str(self.config.get("output.csv_encoding", "utf-8-sig")),
            )
            if not silent:
                print(f"[保存] CSV：{csv_path}")
                print(f"[保存] JSON：{json_path}")
            self.logger.info("结果已保存：%s / %s", csv_path, json_path)
            return csv_path, json_path
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("保存结果失败")
            if not silent:
                print(f"[保存失败] {exc}")
            return None

    # --------------------------------------------------------------- 第 5 步
    def _results_flow(self) -> None:
        while True:
            print("\n------- 第 5 步：结果输出与后续选择 -------")
            if not self.state.results:
                print("当前还没有测试结果。")
                if not self.state.nodes:
                    print("也没有未测试节点；请先完成第 2、3 步。")
                    return
                print(f"已有 {len(self.state.nodes)} 个未测试节点。")
                print("1. 导出未测试节点的纯 vless 链接 txt")
                print("2. 返回主菜单")
                choice = _ask("请选择", "0")
                if choice == "1":
                    self._export_nodes()
                elif choice in {"0", "2"}:
                    return
                continue

            self.state.results = sorted_results(self.state.results, "latency")
            print(format_table(self.state.results))
            print("\n1. 保存完整结果为 CSV + JSON")
            print("2. 导出全部 vless 链接 txt（供 v2rayN 导入）")
            print("3. 按速度排序导出 vless 链接")
            print("4. 按延迟排序导出 vless 链接")
            print("5. 重新测试选中项")
            print("6. 查看失败原因详情")
            print("0. 返回主菜单")
            choice = _ask("请选择", "0")
            if choice == "0":
                return
            if choice == "1":
                self._save_results()
            elif choice == "2":
                self._export_results("all", "latency")
            elif choice == "3":
                self._export_results("all", "speed")
            elif choice == "4":
                self._export_results("all", "latency")
            elif choice == "5":
                self._retest_selected()
            elif choice == "6":
                self._show_failures()
            else:
                print("无效选项。")

    def _export_nodes(self) -> None:
        try:
            path = export_node_links(
                self.state.nodes,
                self.config.resolve("export_dir"),
                label="vless_untested",
            )
            print(f"[导出] {len(self.state.nodes)} 条未测试链接：{path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[导出失败] {exc}")

    def _export_results(self, which: str, mode: str) -> None:
        if not self.state.results:
            print("没有测试结果可导出。")
            return
        try:
            ordered = sorted_results(self.state.results, mode)
            label = "vless_all" if which == "all" else "vless_selected"
            path = export_links(ordered, self.config.resolve("export_dir"), label=label, mode=mode)
            good = sum(1 for r in ordered if r.latency_ms is not None)
            print(f"[导出] 共 {len(ordered)} 条（可用 {good} 条），文件名：{path.name}")
            print(f"        {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[导出失败] {exc}")

    def _show_failures(self) -> None:
        failures = [r for r in self.state.results if r.latency_ms is None or r.error_type]
        if not failures:
            print("没有失败或测速异常的节点。")
            return
        print(f"\n失败/异常节点共 {len(failures)} 条：")
        for result in failures:
            print(
                f"  {result.node.address}:{result.node.port} | {result.status} | "
                f"{result.error_type or '-'} | {result.detail or '无详情'}"
            )

    def _retest_selected(self) -> None:
        if not self.state.results:
            print("没有可重新测试的结果。")
            return
        try:
            resolve_xray_executable(self.config)
        except XrayTestError as exc:
            print(f"[无法测试] {exc}")
            return
        displayed = sorted_results(self.state.results, "latency")
        print("当前顺序（按延迟）：")
        print(format_table(displayed))
        raw = _ask("输入要重测的序号（如 1,3 或 2-4；0=取消）", "0")
        if raw == "0":
            return
        indices = _parse_selection(raw, len(displayed))
        if not indices:
            print("未选择有效序号。")
            return
        selected_nodes = [displayed[i].node for i in indices]
        print(f"将重新测试 {len(selected_nodes)} 个节点……")
        progress_lock = threading.Lock()

        def progress(done: int, total: int, result: NodeResult) -> None:
            with progress_lock:
                latency = f"{result.latency_ms:.0f}ms" if result.latency_ms is not None else "-"
                print(f"[{done}/{total}] {result.node.address}:{result.node.port} {result.status} {latency}")

        try:
            tester = XrayTester(self.config, self.logger)
            new_results = tester.test_nodes(selected_nodes, progress=progress)
        except KeyboardInterrupt:
            cleanup_all_processes()
            print("已中断并清理 Xray 子进程。")
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("重测失败")
            print(f"[重测失败] {exc}")
            cleanup_all_processes()
            return

        replace_map = {r.node.endpoint_key: r for r in new_results}
        merged: list[NodeResult] = []
        for result in self.state.results:
            merged.append(replace_map.get(result.node.endpoint_key, result))
        self.state.results = sorted_results(merged, "latency")
        print("\n[重测完成] 最新结果：")
        print(format_table(self.state.results))
        self._save_results()

    # ---------------------------------------------------------- 辅助函数
    def export_current(self) -> None:
        """供外部调用（保底）。"""
        if self.state.results:
            self._export_results("all", "latency")
        elif self.state.nodes:
            self._export_nodes()


def build_query_safe(template: str, code: str, port: str = "") -> str:
    from .fofa import build_query

    return build_query(template, code, port=port)
