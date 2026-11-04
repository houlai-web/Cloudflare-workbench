"""可复用的业务流程服务层：CLI 与 Web UI 共用同一套逻辑。

这里只做“流程编排”，具体解析、生成、测试、导出仍调用
extractor / nodes / xray / results 等既有模块，不重复实现业务规则。
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .extractor import Endpoint, ExtractStats, extract_file, save_endpoints
from .fofa import FofaError, import_text, import_local_file
from .nodes import (
    ASSIGN_MODES,
    NodeGenerationError,
    VlessNode,
    generate_nodes,
    normalize_samples,
    parse_samples,
)
from .results import (
    export_endpoints_txt,
    export_hosts_txt,
    export_links,
    host_country_line,
    export_node_links,
    reason_cn,
    result_link,
    save_results,
    sort_for_autosave,
    sorted_for_export,
    sorted_results,
)
from .xray import (
    NodeResult,
    XrayTester,
    XrayTestError,
    cleanup_all_processes,
    resolve_xray_executable,
)

SUPPORTED_EXTENSIONS = {".txt", ".csv", ".json", ".html", ".htm"}
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class ServiceError(RuntimeError):
    """面向用户的流程错误，web 层直接展示 message。"""


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def save_upload(config: Config, filename: str, data: bytes) -> Path:
    """保存上传文件到 work_dir（过程副本），并写来源元数据。"""
    name = Path(filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ServiceError(
            f"不支持的文件类型：{suffix or '无扩展名'}；请上传 txt/csv/json/html"
        )
    if not data:
        raise ServiceError("上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError("文件超过 32 MB，请拆分后再上传")
    work_dir = config.resolve("work_dir")
    work_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:40] or "upload"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_dir = work_dir / f"upload_{stamp}_{safe}"
    task_dir.mkdir(parents=True, exist_ok=True)
    dest = task_dir / name
    dest.write_bytes(data)
    meta = {
        "source": "web_upload",
        "query": "Web 上传",
        "original_file": name,
        "size": len(data),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (task_dir / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def import_paste_text(config: Config, text: str, code: str = "") -> Path:
    if not (text or "").strip():
        raise ServiceError("粘贴内容为空")
    try:
        return import_text(config, text, code=code, note="Web 粘贴导入")
    except FofaError as exc:
        raise ServiceError(str(exc)) from exc


def import_local_path(config: Config, raw_path: str, code: str = "") -> Path:
    path = Path((raw_path or "").strip().strip('"')).expanduser()
    if not path.is_absolute():
        path = config.base_dir / path
    if suffix := path.suffix.lower():
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ServiceError(f"不支持的文件类型：{suffix}")
    try:
        return import_local_file(config, path, code=code)
    except FofaError as exc:
        raise ServiceError(str(exc)) from exc


def list_download_files(config: Config) -> list[dict[str, Any]]:
    directory = config.resolve("download_dir")
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in directory.iterdir():
        if not path.is_file() or path.name.endswith((".meta.json", ".raw.json")):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# 提取（CLI 与 Web 共用的合并/去重逻辑）
# ---------------------------------------------------------------------------


@dataclass
class ExtractOutcome:
    endpoints: list[Endpoint] = field(default_factory=list)
    stats: ExtractStats = field(default_factory=ExtractStats)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    saved_txt: str = ""
    saved_json: str = ""


def extract_many(
    config: Config,
    paths: list[Path],
    country: str = "",
    logger: logging.Logger | None = None,
    default_port: int | None = None,
    batch_country: str = "",
) -> ExtractOutcome:
    """逐个文件提取后合并去重；单个文件失败不影响其它文件。"""
    outcome = ExtractOutcome()
    seen: set[str] = set()
    for path in paths:
        try:
            result = extract_file(
                path, config, country=country, default_port=default_port,
                batch_country=batch_country,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{path.name} 解析失败：{exc}"
            outcome.warnings.append(message)
            if logger:
                logger.warning(message)
            continue
        outcome.files.append(str(path))
        stats = outcome.stats
        stats.matched += result.stats.matched
        stats.invalid += result.stats.invalid
        stats.private_or_local += result.stats.private_or_local
        stats.port_filtered += result.stats.port_filtered
        stats.truncated += result.stats.truncated
        stats.duplicate += result.stats.duplicate
        stats.ip_without_port += result.stats.ip_without_port
        stats.vless_links += result.stats.vless_links
        for endpoint in result.endpoints:
            if endpoint.key in seen:
                stats.duplicate += 1
                continue
            seen.add(endpoint.key)
            outcome.endpoints.append(endpoint)
    outcome.stats.with_country = sum(
        1 for ep in outcome.endpoints if ep.country
    ) - outcome.stats.country_from_batch
    outcome.stats.without_country = (
        len(outcome.endpoints)
        - outcome.stats.with_country
        - outcome.stats.country_from_batch
    )
    return outcome


def extract_and_save(
    config: Config,
    paths: list[Path],
    country: str = "",
    label: str = "",
    logger: logging.Logger | None = None,
    default_port: int | None = None,
    batch_country: str = "",
) -> ExtractOutcome:
    outcome = extract_many(
        config, paths, country=country, logger=logger, default_port=default_port,
        batch_country=batch_country,
    )
    if outcome.endpoints:
        txt_path, json_path = save_endpoints(
            outcome.endpoints, config.resolve("work_dir"), label=label
        )
        outcome.saved_txt = str(txt_path)
        outcome.saved_json = str(json_path)
    return outcome


# ---------------------------------------------------------------------------
# 生成与导出
# ---------------------------------------------------------------------------


MAX_SAMPLES = 10


def generate_nodes_from_text(
    config: Config,
    endpoints: list[Endpoint],
    sample: str,
    region: str,
    limit: int,
    name_template: str = "",
    samples: list[str] | None = None,
    mode: str = "random",
) -> list[VlessNode]:
    """多示例 + 分配模式；sample 单示例字段继续兼容（CLI 菜单 3 与旧 config）。"""
    if not endpoints:
        raise ServiceError("没有可用端点，请先完成资产提取")
    links = normalize_samples(sample, samples)
    if not links:
        fallback = str(config.get("generate.sample_vless", "")).strip()
        links = [fallback] if fallback else []
    if not links:
        raise ServiceError("请填写示例 vless:// 链接")
    if len(links) > MAX_SAMPLES:
        raise ServiceError(f"示例链接最多 {MAX_SAMPLES} 条，当前 {len(links)} 条")
    if mode not in ASSIGN_MODES:
        raise ServiceError(f"分配模式不合法：{mode}")
    strict = bool(config.get("generate.strict_uuid", True))
    try:
        parse_samples(links, strict_uuid=strict)
    except NodeGenerationError as exc:
        raise ServiceError(str(exc)) from exc
    limit = max(1, min(int(limit or 1), len(endpoints)))
    try:
        nodes = generate_nodes(
            endpoints,
            links[0],
            region or "NODE",
            limit=limit,
            name_template=name_template or str(config.get("generate.name_template", "")),
            latency_placeholder=str(config.get("generate.latency_placeholder", "待测")),
            strict_uuid=strict,
            samples=links,
            mode=mode,
        )
    except NodeGenerationError as exc:
        raise ServiceError(f"生成失败：{exc}") from exc
    return nodes


def sample_template_counts(nodes: list[VlessNode], total_templates: int) -> list[int]:
    """统计每条示例被用到多少次，长度固定为模板总数。"""
    counts = [0] * max(1, total_templates)
    for node in nodes:
        idx = node.template_index - 1
        if 0 <= idx < len(counts):
            counts[idx] += 1
    return counts


def export_links_file(config: Config, results: list[NodeResult], kind: str) -> Path:
    """导出 txt：all 沿用页面当前排序；latency/speed 固定按自身语义排序。"""
    export_dir = config.resolve("export_dir")
    try:
        if kind == "all":
            return export_links(results, export_dir, label="全部节点", mode=None)
        if kind == "latency":
            # 固定延迟升序（失败/无延迟垫底，稳定排序）
            return export_links(results, export_dir, label="按延迟排序", mode="latency")
        if kind == "speed":
            # 固定速度降序（无速度/失败垫底，稳定排序）
            return export_links(results, export_dir, label="按速度排序", mode="speed")
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    raise ServiceError(f"未知导出类型：{kind}")


def preferred_results(
    results: list[NodeResult], sort_field: str = "latency", sort_dir: str = ""
) -> list[NodeResult]:
    """优选节点：真实测试成功且有延迟数据，按页面当前排序。"""
    ordered = sorted_for_export(results, sort_field, sort_dir)
    return [r for r in ordered if r.latency_ms is not None]


def export_results_files(
    config: Config, results: list[NodeResult], usage: str = "测试结果"
) -> tuple[Path, Path]:
    try:
        return save_results(
            results,
            config.resolve("export_dir"),
            csv_encoding=str(config.get("output.csv_encoding", "utf-8-sig")),
            usage=usage,
        )
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 测试任务（流式事件 + 可停止）
# ---------------------------------------------------------------------------


@dataclass
class TestJob:
    job_id: str
    total: int
    mode: str
    running: bool = True
    stopping: bool = False
    done: int = 0
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    results: list[NodeResult] = field(default_factory=list)
    # SSE 事件只追加不消费：多个连接 / 断线重连都可按 seq 续传，不会漏事件。
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    cond: threading.Condition = field(default_factory=threading.Condition)
    timing: dict[str, Any] = field(default_factory=dict)
    exports: list[dict[str, Any]] = field(default_factory=list)
    export_error: str = ""
    ok_count: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "total": self.total,
            "done": self.done,
            "seq": self.seq,
            "running": self.running,
            "stopping": self.stopping,
            "mode": self.mode,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timing": self.timing,
            "exports": list(self.exports),
            "export_error": self.export_error,
            "ok_count": self.ok_count,
        }


def _result_payload(result: NodeResult, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "address": result.node.address,
        "port": result.node.port,
        "name": result.node.name,
        "region": result.node.region,
        "link": result.node.link,
        "latency_ms": result.latency_ms,
        "speed_mbps": result.speed_mbps,
        "downloaded_mb": round(result.downloaded_mb, 3),
        "status": result.status,
        "error_type": result.error_type,
        "reason_cn": reason_cn(result.error_type),
        "detail": result.detail,
        "tested_at": result.tested_at,
    }


class Workbench:
    """一次会话内的业务状态：导入 → 提取 → 生成 → 测试 → 导出。"""

    def __init__(self, config: Config, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("vless_tool.web")
        self.lock = threading.RLock()
        self.endpoints: list[Endpoint] = []
        self.nodes: list[VlessNode] = []
        self.results: list[NodeResult] = []
        self.files: list[str] = []
        self.stats = ExtractStats()
        self.region = ""
        self.region_code = ""
        self.job: TestJob | None = None
        self._job_seq = 0
        self._last_mode: str | None = None  # 最近一次真实测试的测法
        self.exports: list[dict[str, Any]] = []
        # 最近一次发布成功的链接，供 app/tools/verify_link.py --last 使用
        self.last_publish: dict[str, Any] | None = None

    # ---------------------------------------------------------------- 状态
    def summary(self) -> dict[str, Any]:
        with self.lock:
            job = self.job.snapshot() if self.job else None
            return {
                "endpoints": len(self.endpoints),
                "nodes": len(self.nodes),
                "results": len(self.results),
                "files": self.files,
                "region": self.region,
                "region_code": self.region_code,
                "stats": self.stats.__dict__,
                "job": job,
                # 有测试任务时以实际测法为准，否则回退到配置默认值
                "test_mode": self._last_mode
                or str(self.config.get("test.test_mode", "latency_speed")),
                "xray": self._xray_info(),
                "exports": self.exports[-10:],
                "last_publish": self.last_publish,
                # 发布弹窗三个输入框的默认值（页面仅作初值，改动在发布时写回配置）
                "publish_defaults": {
                    "domain": str(self.config.get("publish.domain", "") or ""),
                    "token": str(self.config.get("publish.token", "") or ""),
                    "filename": default_publish_filename(self.config),
                },
            }

    def _xray_info(self) -> dict[str, Any]:
        try:
            exe = resolve_xray_executable(self.config, self.logger)
        except XrayTestError as exc:
            return {"available": False, "path": "", "message": str(exc)}
        return {"available": True, "path": str(exe), "message": ""}

    # ---------------------------------------------------------------- 导入
    def add_files(self, paths: list[Path]) -> None:
        with self.lock:
            for path in paths:
                if str(path) not in self.files:
                    self.files.append(str(path))

    def extract(
        self,
        paths: list[Path],
        country: str = "",
        label: str = "",
        default_port: int | None = None,
        batch_country: str = "",
    ) -> dict[str, Any]:
        paths = [Path(p) for p in paths]
        outcome = extract_and_save(
            self.config,
            paths,
            country=country,
            label=label,
            logger=self.logger,
            default_port=default_port,
            batch_country=batch_country,
        )
        with self.lock:
            self.endpoints = outcome.endpoints
            self.stats = outcome.stats
            self.files = outcome.files
            if country:
                self.region_code = country.upper()
                self.region = self.config.region_name(country) or country.upper()
            return {
                "count": len(outcome.endpoints),
                "stats": outcome.stats.__dict__,
                "files": outcome.files,
                "warnings": outcome.warnings,
                "saved_txt": outcome.saved_txt,
                "saved_json": outcome.saved_json,
            }

    # ---------------------------------------------------------------- 生成
    def generate(
        self,
        sample: str,
        region: str,
        limit: int,
        name_template: str = "",
        samples: list[str] | None = None,
        mode: str = "random",
    ) -> dict[str, Any]:
        with self.lock:
            endpoints = list(self.endpoints)
        links = normalize_samples(sample, samples)
        nodes = generate_nodes_from_text(
            self.config, endpoints, sample, region, limit, name_template, links, mode
        )
        with self.lock:
            self.nodes = nodes
            self.results = []
            self.region = region or self.region
            counts = sample_template_counts(nodes, len(links))
            return {
                "count": len(nodes),
                "region": self.region,
                "mode": mode,
                "samples": len(links),
                "template_counts": counts,
                "preview": [
                    {
                        "name": n.name,
                        "link": n.link,
                        "template_index": n.template_index,
                    }
                    for n in nodes[:8]
                ],
            }

    # ---------------------------------------------------------------- 测试
    def start_test(
        self,
        indices: list[int] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.job and self.job.running:
                raise ServiceError("已有测试任务在运行，请先停止或等待完成")
            if not self.nodes:
                raise ServiceError("还没有生成节点，请先完成节点生成")
            targets = self.nodes if not indices else [
                self.nodes[i - 1] for i in indices if 1 <= i <= len(self.nodes)
            ]
            if not targets:
                raise ServiceError("没有选中要测试的节点")
            runtime = self._runtime_config(options or {})
            test_mode = str(runtime.get("test.test_mode", "latency_speed")).lower()
            if test_mode not in {"latency", "latency_speed"}:
                raise ServiceError("测试模式只能是 latency 或 latency_speed")
            self._job_seq += 1
            job = TestJob(
                job_id=f"job{self._job_seq}_{int(time.time())}",
                total=len(targets),
                mode=test_mode,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.job = job
            self._last_mode = test_mode
            self.results = []
        thread = threading.Thread(
            target=self._run_job,
            args=(job, targets, runtime),
            name=f"web-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return job.snapshot()

    def _runtime_config(self, options: dict[str, Any]) -> Config:
        data = copy.deepcopy(self.config.data)
        test = data.setdefault("test", {})
        for key in (
            "concurrency",
            "latency_concurrency",
            "speed_concurrency",
            "kernel_batch",
            "latency_timeout",
            "poll_interval",
            "startup_timeout",
            "latency_samples",
            "latency_sample_mode",
            "request_timeout",
            "latency_url",
            "speed_url",
            "speed_max_bytes",
            "speed_max_seconds",
            "speed_timeout",
            "test_mode",
        ):
            if key in options and options[key] not in (None, ""):
                test[key] = options[key]
        return Config(data, self.config.path)

    def _push(self, job: TestJob, event: dict[str, Any]) -> None:
        with job.cond:
            job.seq += 1
            job.events.append({**event, "seq": job.seq})
            # 只保留最近 1 万条，避免长任务无限占内存（1000 节点也足够）
            if len(job.events) > 10_000:
                del job.events[:5_000]
            job.cond.notify_all()

    def _run_job(self, job: TestJob, nodes: list[VlessNode], runtime: Config) -> None:
        total = len(nodes)
        try:
            self._run_real(job, nodes, runtime, total)
        except XrayTestError as exc:
            job.error = str(exc)
            self._push(job, {"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Web 测试任务异常")
            job.error = f"测试异常：{exc}"
            self._push(job, {"type": "error", "message": job.error})
        finally:
            cleanup_all_processes()
            with self.lock:
                job.running = False
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                job.ok_count = sum(1 for item in job.results if item.latency_ms is not None)
                if not job.error:
                    self.results = sorted_results(list(job.results), "latency")
            # 结果写盘：只有真实写成功才把路径回传给页面，避免「提示已写入但目录为空」
            if job.results and job.ok_count:
                try:
                    csv_path, json_path = export_results_files(
                        self.config, sort_for_autosave(job.results, job.mode)
                    )
                    job.exports = [
                        {"kind": "csv", "path": str(csv_path), "lines": _count_lines(csv_path)},
                        {"kind": "json", "path": str(json_path), "lines": 1},
                    ]
                    self._remember(job.exports)
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("测试结果写盘失败")
                    job.export_error = f"结果写盘失败：{exc}"
            elif not job.results:
                job.export_error = "未产生可导出结果：0 个节点"
            else:
                job.export_error = f"未产生可导出结果：{len(job.results)} 个节点全部失败"
            self.logger.info(
                "job_done job=%s mode=%s done=%s/%s ok=%s exports=%s error=%s",
                job.job_id,
                job.mode,
                job.done,
                total,
                job.ok_count,
                ",".join(item["name"] for item in job.exports) or "-",
                job.error or job.export_error or "-",
            )
            self._push(
                job,
                {
                    "type": "done",
                    "stopped": job.stopping,
                    "error": job.error,
                    "index": job.done,
                    "done": job.done,
                    "total": total,
                    "ok_count": job.ok_count,
                    "exports": job.exports,
                    "export_error": job.export_error,
                },
            )

    def _run_real(
        self, job: TestJob, nodes: list[VlessNode], runtime: Config, total: int
    ) -> None:
        tester = XrayTester(runtime, self.logger)
        done = 0

        def progress(_d: int, _t: int, result: NodeResult) -> None:
            nonlocal done
            if job.stopping:
                return
            done += 1
            job.done = done
            job.results.append(result)
            self._push(
                job,
                {
                    "type": "progress",
                    "index": done,
                    "done": done,
                    "total": total,
                    "item": _result_payload(result, result.node.index),
                },
            )

        def update(result: NodeResult) -> None:
            # 仅延迟模式由 progress 计数；延迟+测速模式下每个节点在测速完成/失败时
            # 恰好回调一次 update，这里自增 done，页面序号才会 [1/N] → [N/N] 递增。
            if job.stopping:
                return
            with self.lock:
                found = False
                for pos, existing in enumerate(job.results):
                    if existing.node.index == result.node.index:
                        job.results[pos] = result
                        found = True
                        break
                if not found:
                    # 延迟+测速模式延迟阶段没有 progress 事件，这里首次落库
                    job.results.append(result)
                final_done = min(total, job.done + 1)
                job.done = final_done
            self._push(
                job,
                {
                    "type": "update",
                    "index": final_done,
                    "done": final_done,
                    "total": total,
                    "item": _result_payload(result, result.node.index),
                },
            )

        tester.test_nodes(
            nodes,
            progress=progress,
            update=update,
            should_stop=lambda: job.stopping,
        )
        job.timing = dict(tester.last_timing_summary)
        if job.stopping and done < total:
            self._push(job, {"type": "log", "message": f"已停止，剩余 {total - done} 个节点未测试"})

    def stop_test(self) -> dict[str, Any]:
        with self.lock:
            job = self.job
            if not job or not job.running:
                raise ServiceError("当前没有正在运行的测试任务")
            job.stopping = True
        cleanup_all_processes()
        self.logger.info("Web 请求停止测试 %s", job.job_id)
        return job.snapshot()

    def job_events_since(
        self, job_id: str, after: int = 0, timeout: float = 0.5
    ) -> tuple[TestJob | None, list[dict[str, Any]]]:
        """返回 (job, seq > after 的事件)；无新事件时最多阻塞 timeout 秒。"""
        job = self.job
        if not job or job.job_id != job_id:
            return None, []
        with job.cond:
            if after >= job.seq:
                job.cond.wait(timeout)
            return job, [event for event in job.events if event["seq"] > after]

    # ---------------------------------------------------------------- 结果
    def result_rows(self, sort: str = "latency", keyword: str = "", only_ok: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            rows = list(self.results)
        if only_ok:
            rows = [r for r in rows if r.ok]
        if keyword:
            low = keyword.lower()
            rows = [
                r
                for r in rows
                if low in r.node.address.lower() or low in r.node.name.lower()
            ]
        mode = "speed" if sort == "speed" else "latency"
        rows = sorted_results(rows, mode)
        return [_result_payload(r, r.node.index) for r in rows]

    def export(
        self,
        kind: str,
        *,
        sort_field: str = "latency",
        sort_dir: str = "",
        indices: list[Any] | None = None,
    ) -> dict[str, Any]:
        """导出入口：文件顺序一律沿用结果表当前排序（sort_field + sort_dir）。

        - share：优选节点 vless 链接文本，只返回字符串供前端写剪贴板，不落文件；
        - api：优选节点 TXT（`IP:端口#国旗-国家`）；
        - selected：按勾选的行导出 TXT，不过滤失败节点。
        """
        # 仅延迟模式没有速度数据：与页面「按速度」禁用保持一致，导出侧也拒绝
        if self._last_mode == "latency" and kind == "speed":
            raise ServiceError(
                "当前为仅延迟模式，未产生速度数据；"
                "请改用「按延迟 txt」或「全部导出 CSV+JSON」"
            )
        with self.lock:
            results = list(self.results)
            nodes = list(self.nodes)
        ordered = sorted_for_export(results, sort_field, sort_dir)
        preferred = [r for r in ordered if r.latency_ms is not None]
        export_dir = self.config.resolve("export_dir")

        if kind == "share":
            if not preferred:
                raise ServiceError(
                    "当前没有优选节点（需真实测试成功且有延迟数据），无法复制分享链接"
                )
            text = "\n".join(result_link(r) for r in preferred)
            return {
                "kind": "share",
                "items": [],
                "count": len(preferred),
                "text": text,
            }

        if kind == "api":
            if not preferred:
                raise ServiceError("当前没有优选节点（需真实测试成功且有延迟数据），无法导出 API")
            try:
                path, lines, missing = export_hosts_txt(
                    preferred, export_dir, usage="优选API"
                )
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            item: dict[str, Any] = {"kind": kind, "path": str(path), "lines": lines}
            if missing:
                item["note"] = f"{missing} 条缺少国家信息，已只输出 host:port"
            self._remember([item])
            return {
                "items": [item],
                "count": lines,
                "missing_country": missing,
            }

        if kind == "selected":
            wanted = {int(i) for i in (indices or []) if str(i).strip().lstrip("-").isdigit()}
            if not wanted:
                raise ServiceError("未勾选任何节点")
            picked = [r for r in ordered if r.node.index in wanted]
            if not picked:
                raise ServiceError("未勾选任何节点")
            try:
                path = export_links(picked, export_dir, label="选中节点", mode=None)
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            lines = _count_lines(path)
            failed = sum(1 for r in picked if r.latency_ms is None)
            item = {
                "kind": kind,
                "path": str(path),
                "lines": lines,
                "selected": len(picked),
                "failed": failed,
            }
            self._remember([item])
            return {
                "items": [item],
                "count": lines,
                "selected": len(picked),
                "failed": failed,
            }

        if kind == "untested":
            if not nodes:
                raise ServiceError("没有可导出的节点")
            path = export_node_links(nodes, export_dir, label="未测试链接")
            lines = _count_lines(path)
            item = {"kind": kind, "path": str(path), "lines": lines}
            self._remember([item])
            return {"items": [item], "count": lines}
        if kind == "csv_json":
            if not preferred:
                raise ServiceError(
                    "当前没有优选节点（需真实测试成功且有延迟数据），无法导出 CSV/JSON"
                )
            csv_path, json_path = export_results_files(
                self.config, preferred, usage="优选节点"
            )
            items = [
                {"kind": "csv", "path": str(csv_path), "lines": _count_lines(csv_path)},
                {"kind": "json", "path": str(json_path), "lines": 1},
            ]
            self._remember(items)
            return {"items": items}
        if kind == "all":
            if not preferred:
                raise ServiceError("当前没有优选节点（需真实测试成功且有延迟数据），无法导出 TXT")
            path = export_links(preferred, export_dir, label="优选节点", mode=None)
        else:
            if not results:
                raise ServiceError("没有测试结果可导出")
            path = export_links_file(self.config, ordered, kind)
        lines = _count_lines(path)
        item = {"kind": kind, "path": str(path), "lines": lines}
        self._remember([item])
        return {"items": [item], "count": lines}

    def publish(
        self,
        *,
        source: str = "generated",
        filename: str = "",
        file_name: str = "",
        sort_field: str = "latency",
        sort_dir: str = "",
        domain: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        """发布：上传内容到 CF 文本主机，行为对齐 上传测速结果到CF.bat。

        两种来源：
        - generated（默认）：优选节点按结果页当前排序生成 `IP:端口#国旗-国家`（缺国家只写 IP:端口），
          内容格式可用 publish.content_mode=vless 切回分享链接；
        - file：原样读取 downloads 下的已有文件上传，不做任何内容转换。

        行数上限 publish.max_lines 默认 0=不限（CF.bat 里 65 行是硬编码，这里不再沿用）。
        """
        if not bool(self.config.get("publish.enabled", True)):
            raise ServiceError("发布功能已关闭（config.json → publish.enabled=false）")
        filename = validate_publish_filename(filename, self.config)
        base_url = normalize_publish_domain(domain, self.config)
        token = normalize_publish_token(token, self.config)
        if source == "file":
            file_name = (file_name or "").strip()
            if not file_name:
                raise ServiceError("请选择 downloads 中要发布的文件")
            if file_name != Path(file_name).name:
                raise ServiceError("文件名不合法：只能传 downloads 目录下的文件名，不能带路径")
            target = (self.config.resolve("download_dir") / file_name).resolve()
            download_dir = self.config.resolve("download_dir").resolve()
            if target.parent != download_dir or not target.is_file():
                raise ServiceError(f"downloads 中找不到文件：{file_name}")
            raw = target.read_bytes()
            if not raw.strip():
                raise ServiceError(f"文件内容为空，无法发布：{file_name}")
            content = raw
            count = len([line for line in raw.decode("utf-8", "replace").splitlines() if line.strip()])
            mode = "file"
            truncated = False
        else:
            with self.lock:
                results = list(self.results)
            ordered = sorted_for_export(results, sort_field, sort_dir)
            preferred = [r for r in ordered if r.latency_ms is not None]
            if not preferred:
                raise ServiceError("当前没有优选节点（需真实测试成功且有延迟数据），无法发布")
            mode = str(self.config.get("publish.content_mode", "ip_port_country") or "").strip()
            if mode != "vless":
                mode = "ip_port_country"
            lines = [
                result_link(r) if mode == "vless" else host_country_line(r)
                for r in preferred
            ]
            try:
                max_lines = int(self.config.get("publish.max_lines", 0) or 0)
            except (TypeError, ValueError):
                max_lines = 0
            if max_lines > 0:
                truncated = len(lines) > max_lines
                lines = lines[:max_lines]
            else:
                truncated = False
            payload = build_publish_payload(lines, max_lines)
            content = payload.encode("utf-8")
            count = len(lines)
            file_name = ""
        upload_url, masked = build_publish_url(
            self.config, filename, content, domain=domain, token=token
        )
        link = build_publish_link(self.config, filename, domain=domain, token=token)
        timeout = _publish_timeout(self.config)
        status, body = _publish_get(upload_url, timeout)
        summary = _response_summary(body)
        self.logger.info(
            "发布完成：%s 来源=%s 状态=%s 行数=%s", masked, mode, status, count
        )
        saved, config_path = self._save_publish_settings(base_url, token, filename)
        last_publish = {
            "url": link,
            "filename": filename,
            "count": count,
            "status": status,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.lock:
            self.last_publish = last_publish
        return {
            "count": count,
            "bytes": len(content),
            "status": status,
            "response": summary,
            "filename": filename,
            "source": "file" if source == "file" else "generated",
            "content_mode": mode,
            "file": file_name,
            "truncated": truncated,
            # 规范化后的域名与 token，供页面回填
            "domain": base_url,
            "token": token,
            # 三项参数已写回配置文件（失败时 saved=False，不影响本次上传）
            "saved": saved,
            "config_path": config_path,
            # 上传用完整 URL（含 b64）只用于本轮上传，不供页面展示/复制
            "upload_url": upload_url,
            # 回显/复制的短链接：文件名 + token，不含 b64
            "link": link,
            "target": masked,
            # 只证明请求已发出并拿到 HTTP 响应，不代表远端一定已入库生效
            "verified": False,
        }

    def _save_publish_settings(
        self, base_url: str, token: str, filename: str
    ) -> tuple[bool, str]:
        """发布成功后把域名/token/默认文件名写回配置文件，便于下次打开弹窗复用。

        文件名若与配置展开后的默认值相同（即用户没手动改过），保存原始占位符
        （如 `{date}.txt`），这样次日仍是当天日期；只有手改过的字面值才写死。
        """
        host = base_url.split("://", 1)[-1]
        config_path = str(self.config.path)
        raw_default = str(
            self.config.get("publish.filename_default")
            or self.config.get("publish.default_filename")
            or DEFAULT_PUBLISH_FILENAME
        ).strip() or DEFAULT_PUBLISH_FILENAME
        persist_name = (
            raw_default if filename == default_publish_filename(self.config) else filename
        )
        try:
            publish_cfg = self.config.data.setdefault("publish", {})
            publish_cfg["domain"] = host
            publish_cfg["token"] = token
            publish_cfg["filename_default"] = persist_name
            self.config.save()
        except OSError as exc:
            self.logger.warning("发布参数写回配置失败：%s（%s）", config_path, exc)
            return False, config_path
        return True, config_path

    def export_endpoints(self, kind: str) -> dict[str, Any]:
        """导出端点清单 txt：ip / ip_port / ip_port_country。"""
        if kind not in {"ip", "ip_port", "ip_port_country"}:
            raise ServiceError(f"未知端点导出类型：{kind or '空'}")
        with self.lock:
            endpoints = list(self.endpoints)
        if not endpoints:
            raise ServiceError("没有可导出的端点，请先完成资产提取")
        try:
            path, lines, missing = export_endpoints_txt(
                endpoints, self.config.resolve("export_dir"), kind
            )
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        item: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "lines": lines,
            "missing_country": missing,
        }
        if kind == "ip_port_country" and missing:
            item["note"] = f"{missing} 条缺少国家信息，已只输出 host:port"
        self._remember([item])
        return {
            "items": [item],
            "count": lines,
            "missing_country": missing,
        }

    def _remember(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            item["name"] = Path(item["path"]).name
            item["download"] = f"/api/download/{item['name']}"
            item["created_at"] = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            self.exports.extend(items)


def _count_lines(path: Path) -> int:
    try:
        return len([line for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()])
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 发布（上传到 CF 文本主机，行为对齐 上传测速结果到CF.bat）
# -----------------------------------------------------------------------------


DEFAULT_PUBLISH_FILENAME = "{date}.txt"
_PUBLISH_NAME_RE = re.compile(r"^[\w.\-]+$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$")


def _publish_placeholders(value: str) -> str:
    """替换文件名里的日期占位符：{date}=YYYYMMDD，{timestamp}=YYYYMMDD_HHMMSS。"""
    now = datetime.now()
    return value.replace("{date}", now.strftime("%Y%m%d")).replace(
        "{timestamp}", now.strftime("%Y%m%d_%H%M%S")
    )


def default_publish_filename(config: Config) -> str:
    """默认文件名：publish.filename_default（缺省 {date}.txt → 当天 YYYYMMDD.txt）。"""
    pattern = config.get("publish.filename_default")
    if not pattern:
        # 兼容旧字段名
        pattern = config.get("publish.default_filename")
    pattern = str(pattern or DEFAULT_PUBLISH_FILENAME).strip() or DEFAULT_PUBLISH_FILENAME
    return _publish_placeholders(pattern)


def validate_publish_filename(raw: str, config: Config) -> str:
    """校验上传文件名：决定远端 URL 路径，只允许安全字符，非法直接中文报错。"""
    name = str(raw or "").strip()
    if not name:
        name = default_publish_filename(config)
    name = _publish_placeholders(name)
    if len(name) > 120:
        raise ServiceError("文件名过长（最多 120 个字符）")
    if not name:
        raise ServiceError("请填写上传文件名")
    if name.startswith(".") or name.endswith(".") or ".." in name:
        raise ServiceError("文件名不能以点开头或结尾，也不能包含 ..")
    if not _PUBLISH_NAME_RE.fullmatch(name):
        raise ServiceError(
            "文件名含非法字符：只能包含中文、字母、数字、下划线、点和连字符（不能含空格、/、\\、?、& 等）"
        )
    return name


def normalize_publish_domain(raw: str, config: Config) -> str:
    """把域名规范成 https://host（去尾部 /），空值回落到配置里的 publish.domain。"""
    value = str(raw or "").strip() or str(config.get("publish.domain", "") or "").strip()
    if not value:
        raise ServiceError("请填写发布域名（如 txt.example.com）")
    if any(ch in value for ch in " <>\t\r\n\"'"):
        raise ServiceError("域名含非法字符：不能包含空格、尖括号或引号")
    if "://" in value:
        scheme, value = value.split("://", 1)
        if scheme.lower() != "https":
            raise ServiceError("发布链接固定使用 https://，请去掉 http:// 前缀")
    value = value.rstrip("/")
    if "/" in value or "?" in value or "#" in value:
        raise ServiceError("域名不能包含路径、查询串或 # 片段")
    if not _DOMAIN_RE.fullmatch(value):
        raise ServiceError(f"域名格式不正确：{value}（示例：txt.example.com）")
    if ":" in value:
        port = value.rsplit(":", 1)[1]
        if not 1 <= int(port) <= 65535:
            raise ServiceError(f"域名端口不合法：{port}")
    return "https://" + value


def normalize_publish_token(raw: str, config: Config) -> str:
    """token 只做纯字符串校验：非空，禁止空格与 & ? /。"""
    token = str(raw or "").strip() or str(config.get("publish.token", "") or "").strip()
    if not token:
        raise ServiceError("请填写发布 token（不能为空）")
    if any(ch.isspace() for ch in token):
        raise ServiceError("token 不能包含空格")
    for ch in "&?/":
        if ch in token:
            raise ServiceError(f"token 不能包含 {ch}")
    return token


def build_publish_payload(lines: list[str], max_lines: int = 0) -> str:
    """用 Windows 换行（\\r\\n）拼接，不带末尾换行；max_lines<=0 表示不截断。

    CF.bat 用 PowerShell 读取前 65 行、以 [Environment]::NewLine 拼接后整体 Base64；
    Windows 下 NewLine 即 \\r\\n。65 行是该脚本里的硬编码限制，这里默认不再沿用。
    """
    selected = lines[:max_lines] if max_lines > 0 else list(lines)
    return "\r\n".join(selected)


def build_publish_link(
    config: Config, filename: str, domain: str = "", token: str = ""
) -> str:
    """拼出回显/复制用的短链接：https://<域名>/<URL编码文件名>?token=<token>。

    `https://`、`/`、`?token=` 固定；域名/文件名/token 来自页面覆盖或配置文件。
    不含 b64：远端已按文件名存下内容，读取只需要文件名 + token。
    """
    base_url = normalize_publish_domain(domain, config)
    token = normalize_publish_token(token, config)
    path = str(filename or "").strip().lstrip("/")
    if not path:
        raise ServiceError("请填写上传文件名")
    return f"{base_url}/{urllib.parse.quote(path, safe='')}?{urllib.parse.urlencode({'token': token})}"


def build_publish_url(
    config: Config,
    filename: str,
    content: str | bytes,
    domain: str = "",
    token: str = "",
) -> tuple[str, str]:
    """拼出上传用的完整 GET 地址，返回（真实 URL，脱敏 URL）。

    真实请求形如：https://<域名>/<文件名>?token=<token>&b64=<base64>
    - <filename> 即 CF.bat 的 %~nx1（上传文件名），决定远端 URL 路径；
    - b64 是内容的 UTF-8 标准 Base64；查询值统一做百分号编码，
      避免 base64 里的 + / = 在查询串中被误读（CF.bat 未编码，属已知隐患）。
    注意：这个 URL 只用于上传，不要回显给用户（用 build_publish_link 的短链接）。
    """
    base_url = normalize_publish_domain(domain, config)
    token = normalize_publish_token(token, config)
    path = str(filename or "").strip().lstrip("/")
    if not path:
        raise ServiceError("请填写上传文件名")
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    b64 = base64.b64encode(raw).decode("ascii")
    safe_path = urllib.parse.quote(path, safe="")
    query = urllib.parse.urlencode({"token": token, "b64": b64})
    url = f"{base_url}/{safe_path}?{query}"
    masked = f"{base_url}/{safe_path}?token=***&b64=***"
    return url, masked


def _publish_timeout(config: Config) -> float:
    try:
        value = float(config.get("publish.timeout", 15) or 15)
    except (TypeError, ValueError):
        value = 15.0
    return value if value > 0 else 15.0


def _read_response_text(resp: Any, limit: int = 4096) -> str:
    try:
        raw = resp.read(limit)
    except Exception:  # noqa: BLE001 - 读取失败时只要摘要为空即可
        return ""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _response_summary(text: str, limit: int = 200) -> str:
    """把返回内容压成一行摘要，过长截断，便于直接展示。"""
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _publish_get(url: str, timeout: float) -> tuple[int, str]:
    """发起发布 GET；失败统一转成中文 ServiceError。"""
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "VLESS-Workbench/1.0 (local)",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return int(resp.getcode() or 0), _read_response_text(resp)
    except urllib.error.HTTPError as exc:
        detail = _response_summary(_read_response_text(exc))
        head = f"发布失败：目标站点返回 HTTP {exc.code} {exc.reason}"
        message = f"{head}；返回内容：{detail}" if detail else f"{head}；返回内容为空"
        raise ServiceError(message) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ServiceError(f"发布失败：请求超时（{timeout:g} 秒内未响应）") from exc
        if isinstance(reason, ssl.SSLError):
            raise ServiceError(f"发布失败：HTTPS 证书校验失败（{reason}）") from exc
        if isinstance(reason, socket.gaierror):
            raise ServiceError(f"发布失败：域名解析失败（{reason}）") from exc
        raise ServiceError(f"发布失败：网络连接失败（{reason}）") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ServiceError(f"发布失败：请求超时（{timeout:g} 秒内未响应）") from exc
    except ssl.SSLError as exc:
        raise ServiceError(f"发布失败：HTTPS 证书校验失败（{exc}）") from exc
    except ValueError as exc:
        raise ServiceError(f"发布失败：发布地址无效（{exc}）") from exc
    except OSError as exc:
        raise ServiceError(f"发布失败：网络连接失败（{exc}）") from exc
