"""FOFA 查询语句生成、网页/文本粘贴导入、原始结果落盘（不含任何凭据或 API 采集）。"""

from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


class FofaError(RuntimeError):
    """FOFA 语句生成/导入失败；message 面向用户，包含可行的兜底建议。"""


def build_query(template: str, code: str, port: str = "", protocol: str = "vless") -> str:
    """按配置模板生成 FOFA 查询语句，并打印前的值由调用方决定。"""
    try:
        query = template.format(code=code.upper(), port=port, protocol=protocol)
    except KeyError as exc:
        raise FofaError(f"查询模板包含未知占位符：{exc}（可用 {{code}}/{{port}}/{{protocol}}）") from exc
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        raise FofaError("FOFA 查询语句为空，请检查 fofa.query_template")
    if "{port}" in template and not port:
        raise FofaError("当前查询模板需要端口，请在菜单中输入端口，或改用不含 {port} 的模板")
    return query


# ---------------------------------------------------------------------------
# 「FOFA 搜索」查询构造（第 1 步，CLI 与 Web 共用）
# 语法口径以用户提供的《FOFA语法示例.txt》为准：
#   server=="cloudflare" && <地区或国家> && port="443" && header="Forbidden"
#     && (asn=="..." || ...)                       # 线路（可省略）
#     && (asn!="13335" && ... 共 19 条)            # 固定排除 ASN
# 要点：
#   * 香港 HK / 台湾 TW / 澳门 MO 是中国的地方行政区 → region="HK"…
#   * 美国 US / 韩国 KR / 日本 JP 等主权国家      → country="US"…
#     每项在下面映射表里显式标注 field，禁止在别处用「猜」的方式判断。
#   * 「全部(不限ASN)」= 不拼 asn== 部分，其余条件不变。
# ---------------------------------------------------------------------------

FOFA_SEARCH_BASE_CLAUSE = 'server=="cloudflare"'
FOFA_SEARCH_URL = "https://fofa.info/result"
FOFA_HEADER_CLAUSE = 'header="Forbidden"'

# 固定排除的 19 个 ASN（Cloudflare 自有 + 已知二房东），与示例 txt 完全一致
FOFA_EXCLUDED_ASNS: tuple[str, ...] = (
    "13335", "209242", "394397", "34102", "395869", "398324", "10135",
    "201106", "197540", "62904", "131629", "132892", "202623", "203898",
    "210776", "212237", "212238", "212239", "212240",
)

# 第二列「网络 / 线路」：ASN 组合严格照抄示例 txt
FOFA_LINE_OPTIONS: list[dict[str, Any]] = [
    {"value": "mobile", "label": "移动线路", "flag": "📶",
     "asns": ("45102", "37963", "45090"),
     "hint": "ASN 45102 / 37963 / 45090"},
    {"value": "unicom", "label": "联通线路", "flag": "📶",
     "asns": ("132203", "396982", "31898"),
     "hint": "ASN 132203 / 396982 / 31898"},
    {"value": "telecom", "label": "电信线路", "flag": "📶",
     "asns": ("45102", "55990", "37963"),
     "hint": "ASN 45102 / 55990 / 37963"},
    {"value": "tier1", "label": "一线机房", "flag": "🏢",
     "asns": ("45102", "132203", "45090", "37963"),
     "hint": "ASN 45102 / 132203 / 45090 / 37963"},
    {"value": "tier2", "label": "二线机房", "flag": "🏬",
     "asns": ("55990", "396982", "31898"),
     "hint": "ASN 55990 / 396982 / 31898"},
    {"value": "overseas", "label": "海外VPS", "flag": "🌍",
     "asns": ("25820", "16276", "16509", "20473", "24940", "63949", "14061", "36352"),
     "hint": "ASN 25820 / 16276 / 16509 / 20473 / 24940 / 63949 / 14061 / 36352"},
    {"value": "any", "label": "全部(不限ASN)", "flag": "♾️",
     "asns": (), "hint": "不拼 asn== 条件，其余条件不变"},
]

# 第一列「地区 / 国家」：(代码, 中文名, 国旗, 字段)
# field="region" 仅用于中国的地方行政区；其余主权国家一律 country。
_FOFA_PLACES: tuple[tuple[str, str, str, str], ...] = (
    ("", "不限地区/国家", "🌐", ""),
    ("HK", "中国香港", "🇭🇰", "region"),
    ("TW", "中国台湾", "🇹🇼", "region"),
    ("MO", "中国澳门", "🇲🇴", "region"),
    ("SG", "新加坡", "🇸🇬", "country"),
    ("JP", "日本", "🇯🇵", "country"),
    ("KR", "韩国", "🇰🇷", "country"),
    ("US", "美国", "🇺🇸", "country"),
    ("MY", "马来西亚", "🇲🇾", "country"),
    ("TH", "泰国", "🇹🇭", "country"),
    ("VN", "越南", "🇻🇳", "country"),
    ("ID", "印度尼西亚", "🇮🇩", "country"),
    ("PH", "菲律宾", "🇵🇭", "country"),
    ("IN", "印度", "🇮🇳", "country"),
    ("AU", "澳大利亚", "🇦🇺", "country"),
    ("DE", "德国", "🇩🇪", "country"),
    ("GB", "英国", "🇬🇧", "country"),
    ("FR", "法国", "🇫🇷", "country"),
    ("NL", "荷兰", "🇳🇱", "country"),
    ("ES", "西班牙", "🇪🇸", "country"),
    ("IT", "意大利", "🇮🇹", "country"),
    ("CA", "加拿大", "🇨🇦", "country"),
    ("BR", "巴西", "🇧🇷", "country"),
    ("ZA", "南非", "🇿🇦", "country"),
)

FOFA_PLACE_OPTIONS: list[dict[str, str]] = [
    {
        "value": code,
        "label": name,
        "flag": flag,
        "field": field,  # "" | "region" | "country"
        "display": (f"{flag} {name}" if flag else name),
    }
    for code, name, flag, field in _FOFA_PLACES
]

# 6 个标准端口，用于「非标端口」的反向排除
FOFA_STANDARD_PORTS: tuple[str, ...] = ("443", "2096", "8443", "2053", "2083", "2087")

FOFA_PORT_OPTIONS: list[dict[str, str]] = [
    {"value": "standard", "label": "全部端口", "flag": "♾️"},
    {"value": "443", "label": "443", "flag": "🔒"},
    {"value": "2096", "label": "2096", "flag": "🔒"},
    {"value": "8443", "label": "8443", "flag": "🔒"},
    {"value": "2053", "label": "2053", "flag": "🔒"},
    {"value": "2083", "label": "2083", "flag": "🔒"},
    {"value": "2087", "label": "2087", "flag": "🔒"},
    {"value": "nonstandard", "label": "非标端口", "flag": "🔀"},
]

# 默认：地区=中国香港、线路=全部(不限ASN)、端口=443
FOFA_DEFAULTS = {"place": "HK", "line": "any", "port": "443"}


def _option_label(options: list[dict[str, Any]], value: str, field: str) -> str:
    for item in options:
        if item["value"] == value:
            return item["label"]
    raise FofaError(
        f"{field} 取值不合法：{value or '(空)'}；可选："
        + "、".join(item["value"] or "(不限)" for item in options)
    )


def _asn_include_clause(asns: tuple[str, ...]) -> str:
    if not asns:
        return ""
    return "(" + " || ".join(f'asn=="{item}"' for item in asns) + ")"


def _asn_exclude_clause() -> str:
    return "(" + " && ".join(f'asn!="{item}"' for item in FOFA_EXCLUDED_ASNS) + ")"


def qbase64_encode(query: str) -> str:
    """FOFA 搜索链接使用 URL-safe base64，且去掉结尾的 =。"""
    encoded = base64.urlsafe_b64encode(query.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def qbase64_decode(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise FofaError("qbase64 为空")
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FofaError(f"qbase64 不是合法的 URL-safe base64：{exc}") from exc


def fofa_search_url(query: str) -> str:
    """构造 fofa.info 搜索链接。

    用**标准 base64（保留 = 填充）**再 `urllib.parse.quote` 全量编码，
    避免 url-safe 无填充版本在 fofa.info 客户端偶发「参数错误」的时序问题；
    `qbase64_encode` 仍返回 url-safe 无填充版本，供展示/比较使用。
    """
    std = base64.b64encode(query.encode("utf-8")).decode("ascii")
    return f"{FOFA_SEARCH_URL}?qbase64={quote(std, safe='')}"


def build_search_query(
    line: str = "any", place: str = "", port: str = "443"
) -> dict[str, Any]:
    """按「地区/国家 · 线路 · 端口」拼装 FOFA 查询语句与搜索链接。

    返回 query / url / qbase64 / semantic 等；非法取值抛 FofaError。
    """
    line = (line or "any").strip().lower()
    line_label = _option_label(FOFA_LINE_OPTIONS, line, "线路")
    line_item = next(item for item in FOFA_LINE_OPTIONS if item["value"] == line)
    line_clause = _asn_include_clause(line_item["asns"])

    port_value = (port or "443").strip().lower()
    port_label = _option_label(FOFA_PORT_OPTIONS, port_value, "端口")
    if port_value == "standard":
        port_clause = ""
        port_desc = "全部端口（不限）"
    elif port_value == "nonstandard":
        port_clause = "(" + " && ".join(
            f'port!="{item}"' for item in FOFA_STANDARD_PORTS
        ) + ")"
        port_desc = "非标端口（排除 " + "、".join(FOFA_STANDARD_PORTS) + "）"
    elif port_value.isdigit() and 1 <= int(port_value) <= 65535:
        port_clause = f'port="{port_value}"'
        port_desc = f"端口 {port_value}"
    else:
        raise FofaError(f"端口取值不合法：{port_value}")

    place_value = (place or "").strip().upper()
    place_item = next(
        (item for item in FOFA_PLACE_OPTIONS if item["value"] == place_value), None
    )
    if place_item is None:
        raise FofaError(
            f"地区/国家 取值不合法：{place_value or '(空)'}；可选："
            + "、".join(item["value"] or "(不限)" for item in FOFA_PLACE_OPTIONS)
        )
    place_field = place_item["field"]
    place_clause = (
        f'{place_field}="{place_value}"' if place_field and place_value else ""
    )
    if place_value:
        place_desc = (
            f"{'地区' if place_field == 'region' else '国家'} "
            f"{place_item['label']}（{place_value}，{place_field}={place_value}）"
        )
    else:
        place_desc = "不限地区/国家"

    clauses = [FOFA_SEARCH_BASE_CLAUSE]
    if place_clause:
        clauses.append(place_clause)
    if port_clause:
        clauses.append(port_clause)
    clauses.append(FOFA_HEADER_CLAUSE)
    if line_clause:
        clauses.append(line_clause)
    clauses.append(_asn_exclude_clause())
    query = " && ".join(clauses)

    return {
        "query": query,
        "url": fofa_search_url(query),
        "qbase64": qbase64_encode(query),
        "line": line,
        "line_label": line_label,
        "line_clause": line_clause,
        # 兼容旧字段名
        "network": line,
        "network_label": line_label,
        "network_clause": line_clause,
        "place": place_value,
        "region": place_value,
        "region_label": place_item["label"],
        "region_field": place_field,
        "region_flag": place_item["flag"],
        "port": port_value,
        "port_label": port_label,
        "semantic": (
            f"范围 Cloudflare 资产（server=cloudflare） · {place_desc}"
            f" · 线路 {line_label} · {port_desc} · 响应头 Forbidden"
        ),
        "clauses": clauses,
    }


def fofa_options() -> dict[str, Any]:
    """前端下拉所需的选项集合与默认值（全部来自后端常量）。"""
    return {
        "places": FOFA_PLACE_OPTIONS,
        "lines": FOFA_LINE_OPTIONS,
        "ports": FOFA_PORT_OPTIONS,
        "defaults": dict(FOFA_DEFAULTS),
        "base_clause": FOFA_SEARCH_BASE_CLAUSE,
        "header_clause": FOFA_HEADER_CLAUSE,
        "excluded_asns": list(FOFA_EXCLUDED_ASNS),
        # 兼容旧字段名
        "networks": FOFA_LINE_OPTIONS,
        "regions": FOFA_PLACE_OPTIONS,
    }


def import_text(
    config: Any,
    text: str,
    code: str = "",
    note: str = "粘贴导入",
    suffix: str = ".txt",
) -> Path:
    """把网页/文本结果保存到 work_dir，并写来源元数据。"""
    if not text.strip():
        raise FofaError("导入内容为空")
    work_dir = config.resolve("work_dir")
    work_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_code = re.sub(r"[^A-Za-z0-9_-]+", "_", code or "CUSTOM")
    path = work_dir / f"fofa_paste_{stamp}_{safe_code}{suffix}"
    path.write_text(text, encoding="utf-8")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta = {
        "source": "manual_paste",
        "query": "手动粘贴/网页结果（无自动查询语句）",
        "country_code": code.upper(),
        "note": note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[导入] 已保存：{path}")
    return path


def import_local_file(config: Any, src: Path, code: str = "") -> Path:
    """把用户本地保存的 FOFA 网页/文件复制进 work_dir，保留原文件。"""
    if not src.exists() or not src.is_file():
        raise FofaError(f"文件不存在：{src}")
    work_dir = config.resolve("work_dir")
    work_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_code = re.sub(r"[^A-Za-z0-9_-]+", "_", code or "CUSTOM")
    dest = work_dir / f"fofa_local_{stamp}_{safe_code}{src.suffix.lower() or '.txt'}"
    shutil.copy2(src, dest)
    meta_path = dest.with_suffix(dest.suffix + ".meta.json")
    meta = {
        "source": "local_file",
        "query": "本地保存的 FOFA 页面/结果文件（无自动查询语句）",
        "country_code": code.upper(),
        "original_file": str(src),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[导入] 已复制到：{dest}")
    return dest
