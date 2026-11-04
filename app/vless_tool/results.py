"""结果展示、CSV/JSON 保存与 vless 链接导出。"""

from __future__ import annotations

import csv
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .extractor import country_display
from .nodes import VlessNode, rename_link
from .xray import NodeResult

# 失败原因中文对照表：key 与 xray.py `_classify_error` 实际返回的 error_type 一致。
# 界面、CSV、JSON 都基于这张表，原始码保留在 reason_code 里便于排查。
ERROR_REASON_CN: dict[str, str] = {
    "kernel_start_failed": "内核启动失败",
    "kernel_exited": "内核进程退出",
    "network_broken": "网络中断",
    "connection_timeout": "连接超时",
    "connection_failed": "目标连接失败",
    "connection_refused": "连接被拒绝",
    "tls_failed": "TLS 握手或证书校验失败",
    "tls_handshake_failed": "TLS 握手失败",
    "dns_failed": "域名解析失败",
    "protocol_error": "协议错误或服务端返回异常",
    "proxy_connection_failed": "本地代理连接失败",
    "proxy_error": "代理返回异常 HTTP 状态",
    "speed_failed": "测速失败",
    "no_response": "无响应",
    "invalid_config": "节点配置无效",
    "internal_error": "内部错误",
    "unknown_failed": "未知错误",
    "": "",
}


def reason_cn(code: str) -> str:
    """把 error_type 翻成中文；未知码原样返回，避免丢失排查信息。"""
    key = (code or "").strip()
    if key in ERROR_REASON_CN:
        return ERROR_REASON_CN[key]
    return "其他错误（" + key + "）" if key else ""


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    fill = max(0, width - _width(text))
    if align == "right":
        return " " * fill + text
    if align == "center":
        left = fill // 2
        return " " * left + text + " " * (fill - left)
    return text + " " * fill


def format_table(results: list[NodeResult], limit: int | None = None, start_index: int = 1) -> str:
    """输出含中文列宽对齐的表格。"""
    if not results:
        return "（暂无结果）"
    headers = ["序号", "地址", "地区", "延迟(ms)", "速度(Mbps)", "状态", "错误分类"]
    rows: list[list[str]] = []
    for offset, r in enumerate(results):
        if limit is not None and offset >= limit:
            break
        latency = f"{r.latency_ms:.0f}" if r.latency_ms is not None else "-"
        speed = f"{r.speed_mbps:.2f}" if r.speed_mbps is not None else "未测"
        rows.append(
            [
                str(start_index + offset),
                f"{r.node.address}:{r.node.port}",
                r.node.region,
                latency,
                speed,
                r.status,
                r.error_type or "-",
            ]
        )
    widths = [len(headers[i]) for i in range(len(headers))]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    line = "  ".join(_pad(headers[i], widths[i], "center") for i in range(len(headers)))
    sep = "-+-".join("-" * widths[i] for i in range(len(headers)))
    lines = [line, sep]
    for row in rows:
        lines.append(
            "  ".join(
                [
                    _pad(row[0], widths[0], "right"),
                    _pad(row[1], widths[1]),
                    _pad(row[2], widths[2]),
                    _pad(row[3], widths[3], "right"),
                    _pad(row[4], widths[4], "right"),
                    _pad(row[5], widths[5]),
                    _pad(row[6], widths[6]),
                ]
            )
        )
    if limit is not None and len(results) > limit:
        lines.append(f"... 另有 {len(results) - limit} 条未显示")
    return "\n".join(lines)


def sorted_results(results: list[NodeResult], mode: str = "latency") -> list[NodeResult]:
    return sorted(results, key=lambda r: r.sort_key(mode))


def sorted_for_export(
    results: list[NodeResult], field: str = "latency", direction: str = ""
) -> list[NodeResult]:
    """按结果表当前排序导出（字段 + 方向）；失败/缺值节点统一垫底，保持原相对顺序。

    direction 留空时取页面默认：延迟升序、速度降序。
    """
    if field != "speed":
        field = "latency"
    if direction not in {"asc", "desc"}:
        direction = "desc" if field == "speed" else "asc"
    if field == "speed":

        def key(result: NodeResult) -> tuple[int, float]:
            if result.latency_ms is None or result.speed_mbps is None:
                return (1, 0.0)
            return (0, -result.speed_mbps if direction == "desc" else result.speed_mbps)

    else:

        def key(result: NodeResult) -> tuple[int, float]:
            if result.latency_ms is None:
                return (1, 0.0)
            return (0, result.latency_ms if direction == "asc" else -result.latency_ms)

    return sorted(results, key=key)


def sort_for_autosave(results: list[NodeResult], mode: str = "latency_speed") -> list[NodeResult]:
    """测试结束自动存盘专用排序（CSV 与 JSON 共用同一列表，顺序天然一致）。

    - latency_speed：按 speed_mbps 降序；成功但没有速度的排在有速度之后；
    - latency：按 latency_ms 升序；
    - 两种模式下测试失败（无延迟数据）的节点统一殿后，且失败节点内部保持原有测试顺序；
    - 使用 sorted（稳定），数值相同或都为空的保持原有相对顺序。
    """
    if mode == "latency_speed":

        def key(result: NodeResult) -> tuple[int, float]:
            if result.latency_ms is None:
                return (2, 0.0)
            if result.speed_mbps is None:
                return (1, 0.0)
            return (0, -result.speed_mbps)

    else:

        def key(result: NodeResult) -> tuple[int, float]:
            if result.latency_ms is None:
                return (1, 0.0)
            return (0, result.latency_ms)

    return sorted(results, key=key)


def _latency_label(result: NodeResult) -> str:
    if result.latency_ms is None:
        return "失败"
    if result.speed_mbps is not None:
        return f"{result.latency_ms:.0f}ms-{result.speed_mbps:.1f}Mbps"
    return f"{result.latency_ms:.0f}ms"


def result_link(result: NodeResult) -> str:
    """导出时把节点名更新为包含实测延迟/速度的最终名称。"""
    name = result.node.name_with(_latency_label(result))
    return rename_link(result.node.link, name)


def result_name(result: NodeResult) -> str:
    return result.node.name_with(_latency_label(result))


def _safe_usage(usage: str) -> str:
    """导出文件名用途：允许中文与字母数字，过滤 Windows 非法字符。"""
    safe = "".join(ch for ch in (usage or "") if ch.isalnum() or ch in "-_")
    return safe or "导出"


def _unique_export_paths(export_dir: Path, usage: str, suffixes: list[str]) -> list[Path]:
    """生成 `中文用途_YYYYMMDD_HHMMSS.ext`；同秒冲突时追加 _2、_3。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    attempt = 0
    while True:
        stem = f"{_safe_usage(usage)}_{stamp}"
        if attempt:
            stem = f"{stem}_{attempt + 1}"
        paths = [export_dir / f"{stem}{suffix}" for suffix in suffixes]
        if not any(path.exists() for path in paths):
            return paths
        attempt += 1


def save_results(
    results: list[NodeResult],
    export_dir: Path,
    csv_encoding: str = "utf-8-sig",
    usage: str = "测试结果",
) -> tuple[Path, Path]:
    if not results:
        raise ValueError("没有可保存的测试结果")
    csv_path, json_path = _unique_export_paths(export_dir, usage, [".csv", ".json"])

    fields = [
        "address",
        "port",
        "region",
        "name",
        "latency_ms",
        "speed_mbps",
        "speed_mb_s",
        "downloaded_mb",
        "status",
        "error_type",
        "reason_cn",
        "attempts",
        "detail",
        "tested_at",
        "vless",
    ]
    with csv_path.open("w", encoding=csv_encoding, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for result in results:
            r = result
            writer.writerow(
                {
                    "address": r.node.address,
                    "port": r.node.port,
                    "region": r.node.region,
                    "name": result_name(r),
                    "latency_ms": r.latency_ms if r.latency_ms is not None else "",
                    "speed_mbps": r.speed_mbps if r.speed_mbps is not None else "",
                    "speed_mb_s": round(r.speed_mb_s, 3) if r.speed_mb_s is not None else "",
                    "downloaded_mb": round(r.downloaded_mb, 3),
                    "status": r.status,
                    "error_type": r.error_type,
                    "reason_cn": reason_cn(r.error_type),
                    "attempts": r.attempts,
                    "detail": r.detail,
                    "tested_at": r.tested_at,
                    "vless": result_link(r),
                }
            )

    payload: dict[str, Any] = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(results),
        "results": [
            {
                "address": r.node.address,
                "port": r.node.port,
                "region": r.node.region,
                "name": result_name(r),
                "latency_ms": r.latency_ms,
                "latency_samples_ms": r.latency_samples,
                "speed_mbps": r.speed_mbps,
                "speed_mb_s": round(r.speed_mb_s, 3) if r.speed_mb_s is not None else None,
                "downloaded_bytes": r.downloaded_bytes,
                "status": r.status,
                "reason_code": r.error_type,
                "reason_cn": reason_cn(r.error_type),
                "attempts": r.attempts,
                "detail": r.detail,
                "tested_at": r.tested_at,
                "vless": result_link(r),
            }
            for r in results
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


ENDPOINT_TXT_USAGE: dict[str, str] = {
    "ip": "端点IP",
    "ip_port": "端点IP端口",
    "ip_port_country": "端点IP端口国家",
}


def export_endpoints_txt(
    endpoints: list[Any], export_dir: Path, kind: str
) -> tuple[Path, int, int]:
    """导出端点清单 txt（一行一条）。返回（路径, 行数, 缺国家条数）。"""
    usage = ENDPOINT_TXT_USAGE.get(kind)
    if not usage:
        raise ValueError(f"未知导出类型：{kind}")
    if not endpoints:
        raise ValueError("没有可导出的端点")
    lines: list[str] = []
    missing_country = 0
    for ep in endpoints:
        if kind == "ip":
            lines.append(ep.host)
            continue
        line = str(ep)
        if kind == "ip_port_country":
            if ep.country:
                line = f"{line}#{ep.country}"
            else:
                missing_country += 1
        lines.append(line)
    (path,) = _unique_export_paths(export_dir, usage, [".txt"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, len(lines), missing_country


def export_links(
    results: list[NodeResult],
    export_dir: Path,
    *,
    label: str = "nodes",
    mode: str | None = "latency",
) -> Path:
    if not results:
        raise ValueError("没有可导出的节点")
    (path,) = _unique_export_paths(export_dir, label, [".txt"])
    # mode=None 表示沿用调用方给定的顺序（页面当前排序）
    selected = list(results) if mode is None else sorted_results(results, mode)
    lines = [result_link(r) for r in selected]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def host_country_line(result: NodeResult) -> str:
    """单条优选节点文本：`IP:端口#国旗-国家`；缺国家信息时退化为 `IP:端口`。"""
    host_port = f"{result.node.address}:{result.node.port}"
    country = country_display(result.node.region)
    return f"{host_port}#{country}" if country else host_port


def export_hosts_txt(
    results: list[NodeResult],
    export_dir: Path,
    *,
    usage: str = "优选API",
) -> tuple[Path, int, int]:
    """优选节点 TXT：`IP:端口#🇦🇺-澳大利亚`；缺国家信息时只输出 `IP:端口`。

    返回（路径, 行数, 缺国家条数）。顺序完全沿用传入列表。
    """
    if not results:
        raise ValueError("没有可导出的节点")
    lines: list[str] = []
    missing_country = 0
    for result in results:
        if not country_display(result.node.region):
            missing_country += 1
        lines.append(host_country_line(result))
    (path,) = _unique_export_paths(export_dir, usage, [".txt"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, len(lines), missing_country


def export_node_links(
    nodes: list[VlessNode],
    export_dir: Path,
    *,
    label: str = "nodes",
) -> Path:
    """未测试节点的原始 vless 链接导出（供 v2rayN 导入）。"""
    if not nodes:
        raise ValueError("没有可导出的节点")
    (path,) = _unique_export_paths(export_dir, label, [".txt"])
    path.write_text("\n".join(node.link for node in nodes) + "\n", encoding="utf-8")
    return path
