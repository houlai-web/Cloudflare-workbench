"""从 FOFA 下载文件（txt/csv/json/html）中提取 host:port。"""

from __future__ import annotations

import csv
import html
import ipaddress
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


# IPv4:port / host:port / [IPv6]:port
_IPV4_PORT_RE = re.compile(r"(?<![0-9.])((?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})(?!\d)")
_HOSTNAME_PORT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"\.)+[A-Za-z]{2,63})(?![A-Za-z0-9_.-]):(\d{1,5})(?!\d)"
)
_IPV6_PORT_RE = re.compile(r"\[([0-9A-Fa-f:]{2,45})\]:(\d{1,5})")
_BARE_IP_RE = re.compile(r"(?<![0-9.])((?:\d{1,3}\.){3}\d{1,3})(?![0-9.])")
# 无方括号的 IPv6（必须含 ::，避免把 12:34:56 这类时间戳当成地址）
_BARE_IPV6_RE = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f:]{2,45})(?![0-9A-Fa-f:])")
_VLESS_URL_RE = re.compile(r"vless://[^\s\"'<>`]+", re.IGNORECASE)

# 纯 IP 误抓防护：紧邻这些字符时，1.2.3.4 多半是版本号/路径/参数的一部分
_BAD_BEFORE = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-@%\\")
_BAD_AFTER = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-@%")

KIND_PLAIN = "明文"
KIND_IP_FILLED = "纯IP补端口"
KIND_VLESS = "vless链接"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    country: str = ""
    source: str = ""
    # 来源类型：明文 / 纯IP补端口 / vless链接
    kind: str = KIND_PLAIN

    @property
    def key(self) -> str:
        return f"{self.host.lower().rstrip('/')}:{self.port}"

    def __str__(self) -> str:
        if ":" in self.host and not self.host.startswith("["):
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


@dataclass
class ExtractStats:
    matched: int = 0
    duplicate: int = 0
    invalid: int = 0
    private_or_local: int = 0
    port_filtered: int = 0
    truncated: int = 0
    # 缺少端口、按 extract.default_port 补齐的条数（纯 IP / vless 链接）
    ip_without_port: int = 0
    # 识别到的 vless:// 链接条数
    vless_links: int = 0
    # 来源文件带国家 / 批次补齐 / 未带国家的端点数
    with_country: int = 0
    country_from_batch: int = 0
    without_country: int = 0

    def summary(self) -> str:
        extra = ""
        if self.ip_without_port:
            extra += f"，补默认端口 {self.ip_without_port} 条"
        if self.vless_links:
            extra += f"，vless 链接 {self.vless_links} 条"
        if self.with_country or self.country_from_batch or self.without_country:
            extra += f"，有国家 {self.with_country} 条"
            if self.country_from_batch:
                extra += f"，批次补齐 {self.country_from_batch} 条"
            extra += f"，无国家 {self.without_country} 条"
        return (
            f"候选 {self.matched} 条，去重 {self.duplicate} 条，"
            f"无效 {self.invalid} 条，私有/本地 {self.private_or_local} 条，"
            f"端口过滤 {self.port_filtered} 条"
            + (f"，超上限丢弃 {self.truncated} 条" if self.truncated else "")
            + extra
        )


@dataclass
class ExtractResult:
    endpoints: list[Endpoint] = field(default_factory=list)
    stats: ExtractStats = field(default_factory=ExtractStats)
    files: list[str] = field(default_factory=list)




_COUNTRY_CN: dict[str, str] = {
    "AU": "澳大利亚", "JP": "日本", "US": "美国", "HK": "香港", "TW": "台湾",
    "SG": "新加坡", "KR": "韩国", "DE": "德国", "NL": "荷兰", "GB": "英国",
    "CA": "加拿大", "FR": "法国", "IT": "意大利", "ES": "西班牙", "RU": "俄罗斯",
    "IN": "印度", "BR": "巴西", "TR": "土耳其", "TH": "泰国", "VN": "越南",
    "MY": "马来西亚", "PH": "菲律宾", "ID": "印度尼西亚", "AE": "阿联酋",
    "SA": "沙特阿拉伯", "IL": "以色列", "CH": "瑞士", "SE": "瑞典", "NO": "挪威",
    "FI": "芬兰", "DK": "丹麦", "PL": "波兰", "CZ": "捷克", "AT": "奥地利",
    "BE": "比利时", "IE": "爱尔兰", "PT": "葡萄牙", "GR": "希腊", "UA": "乌克兰",
    "KZ": "哈萨克斯坦", "NZ": "新西兰", "ZA": "南非", "EG": "埃及", "MX": "墨西哥",
    "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚", "PE": "秘鲁", "MO": "澳门",
}
_COUNTRY_ALIAS: dict[str, str] = {"UK": "GB", "EL": "GR", "USA": "US"}
_CN_TO_CODE: dict[str, str] = {name: code for code, name in _COUNTRY_CN.items()}
_CN_TO_CODE.update({"澳洲": "AU", "中国香港": "HK", "中国台湾": "TW", "中国澳门": "MO"})

def _norm_key(name: Any) -> str:
    text = str(name or "").lstrip("\ufeff").strip().lower()
    for ch in (" ", "\t", "\r", "\n", "_", "-", "－", "（", "）", "(", ")", ":", ".", "/"):
        text = text.replace(ch, "")
    return text

_IP_COLUMNS = ("ip", "ipaddress", "ipaddr", "ipv4", "ip地址")
_HOST_COLUMNS = (
    "ip", "ipaddress", "ipaddr", "ipv4", "ip地址",
    "host", "hostname", "domain", "address", "host地址", "域名", "网址", "url",
)
_PORT_COLUMNS = ("port", "端口", "端口号")
_COUNTRY_COLUMNS = (
    "countryname", "country", "国家名称", "国家名", "国家",
    "countrycode", "cc", "iso", "isocode", "国家代码", "countrycn",
    "地区", "区域", "地区名称", "region", "regionname",
    "geo", "geolocation", "geography", "location", "位置", "所在地",
)
_EN_TO_CODE: dict[str, str] = {
    "AUSTRALIA": "AU", "JAPAN": "JP", "UNITED STATES": "US", "USA": "US",
    "UNITED STATES OF AMERICA": "US",
    "HONG KONG": "HK", "HONGKONG": "HK", "HONG KONG SAR": "HK",
    "TAIWAN": "TW", "CHINA TAIWAN": "TW", "TAIWAN, CHINA": "TW",
    "SINGAPORE": "SG", "SOUTH KOREA": "KR", "KOREA": "KR",
    "REPUBLIC OF KOREA": "KR", "GERMANY": "DE", "NETHERLANDS": "NL",
    "THE NETHERLANDS": "NL", "UNITED KINGDOM": "GB", "UK": "GB",
    "GREAT BRITAIN": "GB", "CANADA": "CA", "FRANCE": "FR", "ITALY": "IT",
    "SPAIN": "ES", "RUSSIA": "RU", "RUSSIAN FEDERATION": "RU", "INDIA": "IN",
    "BRAZIL": "BR", "TURKEY": "TR", "THAILAND": "TH", "VIETNAM": "VN",
    "MALAYSIA": "MY", "PHILIPPINES": "PH", "INDONESIA": "ID",
    "UNITED ARAB EMIRATES": "AE", "UAE": "AE", "SAUDI ARABIA": "SA",
    "ISRAEL": "IL", "SWITZERLAND": "CH", "SWEDEN": "SE", "NORWAY": "NO",
    "FINLAND": "FI", "DENMARK": "DK", "POLAND": "PL", "CZECHIA": "CZ",
    "CZECH REPUBLIC": "CZ", "AUSTRIA": "AT", "BELGIUM": "BE", "IRELAND": "IE",
    "PORTUGAL": "PT", "GREECE": "GR", "UKRAINE": "UA", "KAZAKHSTAN": "KZ",
    "NEW ZEALAND": "NZ", "SOUTH AFRICA": "ZA", "EGYPT": "EG", "MEXICO": "MX",
    "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE",
    "MACAU": "MO", "MACAO": "MO", "MACAU SAR": "MO",
}
_FLAG_BASE = 0x1F1E6



class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._skip_depth += 1
        # title/表格里的文本也可能包含 host:port
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _read_text(path: Path) -> str:
    """UTF-8/GBK 兼容读取；失败时抛 ValueError。"""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码（已尝试 UTF-8/GB18030）：{path}")


def _html_to_text(raw: str) -> str:
    parser = _HTMLTextParser()
    try:
        parser.feed(raw)
        return html.unescape("".join(parser.parts))
    except Exception:
        # 极端损坏的 HTML 也要尽量提取
        return re.sub(r"<[^>]+>", " ", html.unescape(raw))


def _flatten_json(value: Any, records: list[Any] | None = None) -> list[Any]:
    """收集 JSON 中的字符串、二元组、字典，供统一扫描。"""
    records = records if records is not None else []
    if isinstance(value, dict):
        # 优先把 ip/host + port 组合记录下来
        host = value.get("host") or value.get("ip") or value.get("address")
        port = value.get("port")
        paired = host is not None and port is not None
        if paired:
            records.append(f"{host}:{port}")
        for key, item in value.items():
            if isinstance(item, str):
                # 已合成 host:port 时跳过单独的主机/端口字段，避免被当成「纯 IP」重复补端口
                if paired and str(key).lower() in {"host", "ip", "address", "port"}:
                    continue
                records.append(item)
            elif isinstance(item, (dict, list)):
                _flatten_json(item, records)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                records.append(item)
            elif isinstance(item, (dict, list)):
                _flatten_json(item, records)
    return records


def _json_documents(raw: str) -> list[Any]:
    text = raw.lstrip("\ufeff")
    decoder = json.JSONDecoder()
    docs: list[Any] = []
    pos, size, line = 0, len(text), 1
    while pos < size:
        while pos < size and text[pos] in '\t\n\r ,;':
            if text[pos] == "\n":
                line += 1
            pos += 1
        if pos >= size:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            snippet = text[pos : pos + 80].replace("\r", " ").replace("\n", " ")
            raise ValueError(
                f"第 {line} 行 JSON 解析失败（第 {exc.colno} 列）：{snippet}"
            ) from None
        docs.append(obj)
        line += text[pos:end].count("\n")
        pos = end
    if not docs:
        raise ValueError("JSON 内容为空")
    return docs


def _json_rows(raw: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pending_fields: list[str] | None = None

    def push(mapping: Any, names: list[str] | None = None) -> None:
        if isinstance(mapping, dict):
            flat = {
                _norm_key(key): "" if value is None else str(value).strip()
                for key, value in mapping.items()
                if not isinstance(value, (dict, list))
            }
            if flat:
                rows.append(flat)
        elif isinstance(mapping, list) and names:
            rows.append(
                {
                    _norm_key(names[i]): (
                        "" if mapping[i] is None else str(mapping[i]).strip()
                    )
                    for i in range(min(len(names), len(mapping)))
                }
            )

    for doc in _json_documents(raw):
        if isinstance(doc, dict):
            fields = doc.get("fields")
            if isinstance(fields, list) and fields:
                pending_fields = [str(name) for name in fields]
            results = doc.get("results")
            if isinstance(results, list):
                for item in results:
                    push(item, pending_fields)
            else:
                push(doc)
        elif isinstance(doc, list):
            for item in doc:
                push(item, pending_fields)
    return rows


def _json_records(raw: str) -> list[str]:
    records: list[str] = []
    for row in _json_rows(raw):
            host = _record_host(row)
            if not host:
                continue
            port = row.get("port")
            if port not in (None, ""):
                records.append(f"{host}:{port}")
            else:
                records.append(host)
            for key, value in row.items():
                if key in _HOST_COLUMNS or key in _PORT_COLUMNS or not value:
                    continue
                records.append(value)
    return records
def _csv_records(raw: str) -> list[str]:
    import csv as _csv
    records: list[str] = []
    reader = _csv.reader(raw.splitlines())
    rows = list(reader)
    if not rows:
        return records
    header = [_norm_key(cell) for cell in rows[0]]
    ip_idx = next((i for i, name in enumerate(header) if name in _IP_COLUMNS), None)
    host_idx = next(
        (i for i, name in enumerate(header) if name in _HOST_COLUMNS and name not in _IP_COLUMNS),
        None,
    )
    port_idx = next((i for i, name in enumerate(header) if name in _PORT_COLUMNS), None)
    pair_idx = ip_idx if ip_idx is not None else host_idx
    start = 1 if (ip_idx is not None or host_idx is not None or port_idx is not None) else 0
    for row in rows[start:]:
        used: set[int] = set()
        if pair_idx is not None and pair_idx < len(row):
            raw_host = str(row[pair_idx]).strip()
            host = _host_key(raw_host) or raw_host
            used.add(pair_idx)
            if port_idx is not None and port_idx < len(row):
                records.append(f"{host}:{str(row[port_idx]).strip()}")
                used.add(port_idx)
            else:
                records.append(host)
        if ip_idx is not None:
            used.add(ip_idx)
        if host_idx is not None:
            used.add(host_idx)
        for idx, cell in enumerate(row):
            if idx in used:
                continue
            records.append(str(cell))
    return records
def _flag(code: str) -> str:
    """两位国家代码 → 国旗 emoji（区域指示符），非法代码返回空串。"""
    code = _COUNTRY_ALIAS.get(code.upper(), code.upper())
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(_FLAG_BASE + ord(ch) - ord("A")) for ch in code)


def country_display(value: Any) -> str:
    """国家字段值 → 展示值（🇦🇺-澳大利亚）。认不出时返回空串，不猜。"""
    text = str(value or "").strip().strip("\"'").strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "unknown"}:
        return ""
    code = _COUNTRY_ALIAS.get(text.upper(), text.upper())
    if code in _COUNTRY_CN:
        return f"{_flag(code)}-{_COUNTRY_CN[code]}"
    if text in _CN_TO_CODE:
        code = _CN_TO_CODE[text]
        return f"{_flag(code)}-{_COUNTRY_CN[code]}"
    # 已经是「🇦🇺-澳大利亚」这种展示值，原样保留
    if "-" in text and ord(text[0]) >= _FLAG_BASE:
        return text
    upper = text.upper()
    if upper in _EN_TO_CODE:
        code = _EN_TO_CODE[upper]
        return f"{_flag(code)}-{_COUNTRY_CN.get(code, code)}"
    upper = text.upper()
    if upper in _EN_TO_CODE:
        code = _EN_TO_CODE[upper]
        return f"{_flag(code)}-{_COUNTRY_CN.get(code, code)}"
    # 已是中文国家名但不在映射表里：直接用，不加国旗
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    return ""




def _host_key(value: Any) -> str:
    text = str(value or "").strip().strip("\"").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            return text[1:end].strip().lower()
    elif text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
        text = text.rsplit(":", 1)[0]
    return text.strip().lower().rstrip(".")


def _record_host(item: dict[str, Any]) -> str:
    for key in _IP_COLUMNS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    for key in _HOST_COLUMNS:
        value = item.get(key)
        if value not in (None, ""):
            return _host_key(value)
    return ""


class _HTMLTableParser(HTMLParser):
    """收集 HTML 表格行（tr/td/th），用于读取 host + country 列。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if not self._skip and self._cell is not None:
            self._cell.append(data)


def _csv_table_rows(raw: str) -> list[dict[str, str]]:
    rows = list(csv.reader(raw.splitlines()))
    if len(rows) < 2:
        return []
    header = [_norm_key(cell) for cell in rows[0]]
    return [
        {header[i]: str(row[i]).strip() for i in range(min(len(header), len(row)))}
        for row in rows[1:]
        if any(str(cell).strip() for cell in row)
    ]


def _iter_json_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_json_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_dicts(item)


def _json_table_rows(raw: str) -> list[dict[str, str]]:
    data = json.loads(raw)
    rows: list[dict[str, str]] = []
    if isinstance(data, dict):
        fields = data.get("fields")
        results = data.get("results")
        if isinstance(fields, list) and isinstance(results, list):
            names = [str(name).strip().lower() for name in fields]
            for item in results:
                if isinstance(item, list):
                    rows.append(
                        {names[i]: str(item[i]).strip() for i in range(min(len(names), len(item)))}
                    )
    for obj in _iter_json_dicts(data):
        rows.append(
            {
                str(key).strip().lower(): ("" if val is None else str(val).strip())
                for key, val in obj.items()
                if not isinstance(val, (dict, list))
            }
        )
    return rows


def _html_table_rows(raw: str) -> list[dict[str, str]]:
    parser = _HTMLTableParser()
    try:
        parser.feed(raw)
    except Exception:
        return []
    if len(parser.rows) < 2:
        return []
    header = [_norm_key(cell) for cell in parser.rows[0]]
    if not any(name in _COUNTRY_COLUMNS for name in header) or not any(
        name in _HOST_COLUMNS for name in header
    ):
        return []
    return [
        {header[i]: row[i] for i in range(min(len(header), len(row)))}
        for row in parser.rows[1:]
    ]


def _source_country_map(raw: str, suffix: str) -> dict[str, str]:
    try:
        if suffix == ".csv":
            rows = _csv_table_rows(raw)
        elif suffix == ".json":
            rows = _json_rows(raw)
        elif suffix in {".html", ".htm"}:
            rows = _html_table_rows(raw)
        else:
            return {}
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for row in rows:
        host = _record_host(row)
        if not host:
            continue
        country = next(
            (country_display(row[key]) for key in _COUNTRY_COLUMNS if row.get(key)),
            "",
        )
        if not country:
            continue
        mapping[host.lower().rstrip(".")] = country
    return mapping


def _apply_batch_country(
    endpoints: list[Endpoint], batch_country: str
) -> tuple[list[Endpoint], int]:
    display = country_display(batch_country) if batch_country else ""
    if not display:
        return endpoints, 0
    filled = 0
    new_eps: list[Endpoint] = []
    for ep in endpoints:
        if ep.country:
            new_eps.append(ep)
        else:
            filled += 1
            new_eps.append(replace(ep, country=display))
    return new_eps, filled


def _apply_source_countries(
    endpoints: list[Endpoint], raw: str, suffix: str
) -> list[Endpoint]:
    mapping = _source_country_map(raw, suffix)
    if not mapping:
        return endpoints
    return [
        ep
        if ep.country
        else replace(ep, country=mapping.get(ep.host.lower().rstrip("."), ""))
        for ep in endpoints
    ]


def _candidate_pairs(text: str) -> Iterable[tuple[str, int]]:
    """明文 host:port（含 [IPv6]:port）。"""
    for match in _IPV6_PORT_RE.finditer(text):
        yield match.group(1), int(match.group(2))
    for match in _IPV4_PORT_RE.finditer(text):
        yield match.group(1), int(match.group(2))
    for match in _HOSTNAME_PORT_RE.finditer(text):
        yield match.group(1), int(match.group(2))


def _parse_vless_token(token: str) -> tuple[str, int | None] | None:
    """解析 vless://<uuid>@<host>[:port][?...][#name]，返回 (host, port|None)。"""
    body = token
    lower = body.lower()
    if lower.startswith("vless://"):
        body = body[len("vless://") :]
    body = body.split("#", 1)[0].split("?", 1)[0]
    if "@" not in body:
        return None
    host_part = body.rsplit("@", 1)[1].strip().split("/", 1)[0]
    if not host_part:
        return None
    if host_part.startswith("["):
        end = host_part.find("]")
        if end < 0:
            return None
        host = host_part[1:end].strip()
        rest = host_part[end + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ""
    elif ":" in host_part:
        host, port_text = host_part.rsplit(":", 1)
        host = host.strip()
    else:
        host, port_text = host_part.strip(), ""
    host = host.rstrip(".").rstrip("/")
    if not host:
        return None
    port: int | None = None
    port_text = port_text.strip()
    if port_text.isdigit():
        port = int(port_text)
    return host, port


def _bare_ip_context_ok(text: str, start: int, end: int) -> bool:
    """拒绝版本号、URL 路径片段、参数值等上下文里的裸 IP。"""
    if start > 0:
        prev = text[start - 1]
        if prev == "/":
            # 只有协议分隔符 // 后面的 IP 才视为真实主机，单斜杠视为 URL 路径
            if start < 2 or text[start - 2] != "/":
                return False
        elif prev in _BAD_BEFORE:
            return False
    if end < len(text):
        nxt = text[end]
        if nxt == ":":
            # 带端口的交给 host:port / [IPv6]:port 规则，避免重复计数
            return False
        if nxt == "]" and end + 2 < len(text):
            # [IPv6]:port 同样交给明文规则
            if text[end + 1] == ":" and text[end + 2].isdigit():
                return False
        if nxt in _BAD_AFTER:
            return False
    return True


@lru_cache(maxsize=65536)
def _is_private_or_local(host: str) -> bool:
    lower = host.lower().strip(".")
    if lower in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    if lower.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


@lru_cache(maxsize=65536)
def _valid_host(host: str) -> bool:
    host = host.strip().strip("[]").rstrip(".")
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if any(not label or len(label) > 63 for label in labels):
        return False
    if not re.fullmatch(r"[A-Za-z]{2,63}", labels[-1]):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", lb) for lb in labels)


def extract_from_text(
    text: str,
    *,
    source: str = "",
    country: str = "",
    port_allowlist: list[int] | None = None,
    port_blocklist: list[int] | None = None,
    exclude_private_ip: bool = True,
    max_items: int = 0,
    default_port: int = 443,
    seen: set[str] | None = None,
    stats: ExtractStats | None = None,
) -> list[Endpoint]:
    """从任意文本中提取合法端点。

    支持三种来源：明文 host:port、缺少端口的纯 IP（补 default_port）、
    vless:// 链接（解析其中的 host 与 port）。纯文本解析，不访问网络。
    """
    seen = seen if seen is not None else set()
    stats = stats if stats is not None else ExtractStats()
    allow = {int(p) for p in (port_allowlist or [])}
    block = {int(p) for p in (port_blocklist or [])}
    endpoints: list[Endpoint] = []

    try:
        default_port = int(default_port)
    except (TypeError, ValueError):
        default_port = 443
    if not 1 <= default_port <= 65535:
        default_port = 443

    def add(host: str, port: int | None, kind: str) -> None:
        host = host.strip().strip("[]").rstrip(".").rstrip("/")
        if not host or not _valid_host(host):
            stats.invalid += 1
            return
        filled = port is None or port == 0
        if filled:
            port = default_port
        if port < 1 or port > 65535:
            stats.invalid += 1
            return
        if allow and port not in allow:
            stats.port_filtered += 1
            return
        if port in block:
            stats.port_filtered += 1
            return
        if exclude_private_ip and _is_private_or_local(host):
            stats.private_or_local += 1
            return
        key = f"{host.lower().rstrip('/')}:{port}"
        if key in seen:
            stats.duplicate += 1
            return
        if max_items and len(seen) >= max_items:
            stats.truncated += 1
            return
        seen.add(key)
        if filled:
            stats.ip_without_port += 1
        endpoints.append(
            Endpoint(host=host, port=port, country=country, source=source, kind=kind)
        )

    # 1) 明文 host:port（含 [IPv6]:port）优先：同一 host:port 同时出现在链接里时，
    #    以显式写明端口的「明文」为准。
    vless_tokens: list[str] = []

    def _mask_vless(match: re.Match[str]) -> str:
        vless_tokens.append(match.group(0))
        return " " * len(match.group(0))

    masked = _VLESS_URL_RE.sub(_mask_vless, text)
    for host, port in _candidate_pairs(masked):
        stats.matched += 1
        add(host, port, KIND_PLAIN)

    # 2) vless:// 链接：从原文解析，链接片段已从明文扫描中抹掉
    for token in vless_tokens:
        stats.matched += 1
        parsed = _parse_vless_token(token)
        if parsed is None:
            stats.invalid += 1
            continue
        stats.vless_links += 1
        host, port = parsed
        add(host, port, KIND_VLESS)

    # 3) 纯 IPv4（无端口）：补默认端口；排除版本号/URL 路径等误抓
    for match in _BARE_IP_RE.finditer(masked):
        host = match.group(1)
        start, end = match.span(1)
        if not _bare_ip_context_ok(masked, start, end):
            continue
        stats.matched += 1
        try:
            ipaddress.ip_address(host)
        except ValueError:
            stats.invalid += 1
            continue
        add(host, None, KIND_IP_FILLED)

    # 4) 纯 IPv6（无端口，必须带 ::，排除 12:34:56 时间戳）。
    #    没有 "::" 的文本直接跳过，避免对大文件做无意义的整段扫描。
    if "::" not in masked:
        return endpoints
    for match in _BARE_IPV6_RE.finditer(masked):
        candidate = match.group(1)
        if "::" not in candidate or not any(ch.isdigit() for ch in candidate):
            continue
        start, end = match.span(1)
        if not _bare_ip_context_ok(masked, start, end):
            continue
        stats.matched += 1
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            stats.invalid += 1
            continue
        if ip.version != 6:
            stats.invalid += 1
            continue
        add(candidate, None, KIND_IP_FILLED)

    return endpoints


def extract_file(
    path: Path, config: Any, country: str = "", default_port: int | None = None,
    batch_country: str = "",
) -> ExtractResult:
    """读取单个文件并提取。自动识别 txt/csv/json/html。"""
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    raw = _read_text(path)

    if suffix in {".json"}:
        try:
            records = _json_records(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc
        text = "\n".join(records)
    elif suffix in {".html", ".htm"}:
        text = _html_to_text(raw)
    elif suffix in {".csv"}:
        text = "\n".join(_csv_records(raw))
    else:
        # txt 可能包含 CSV 行，按空格/换行统一扫描即可
        text = raw

    allow = config.get("extract.port_allowlist", [])
    block = config.get("extract.port_blocklist", [])
    exclude_private = bool(config.get("extract.exclude_private_ip", True))
    max_items = int(config.get("extract.max_items", 0) or 0)
    if default_port is None:
        default_port = int(config.get("extract.default_port", 443) or 443)
    stats = ExtractStats()
    # 国家列只认来源文件里的字段；地区代码（country 参数）不再写进端点，避免猜国家
    endpoints = extract_from_text(
        text,
        source=str(path),
        country="",
        port_allowlist=allow,
        port_blocklist=block,
        exclude_private_ip=exclude_private,
        max_items=max_items,
        default_port=default_port,
        stats=stats,
    )
    if suffix in {".csv", ".json", ".html", ".htm"}:
        endpoints = _apply_source_countries(endpoints, raw, suffix)
    filled = 0
    if batch_country:
        endpoints, filled = _apply_batch_country(endpoints, batch_country)
    stats.with_country = sum(1 for ep in endpoints if ep.country) - filled
    stats.country_from_batch = filled
    stats.without_country = len(endpoints) - stats.with_country - filled
    return ExtractResult(endpoints=endpoints, stats=stats, files=[str(path)])


def extract_files(
    paths: list[Path], config: Any, country: str = "", default_port: int | None = None,
    batch_country: str = "",
) -> ExtractResult:
    """多文件合并提取，跨文件去重。"""
    result = ExtractResult(files=[str(p) for p in paths])
    seen: set[str] = set()
    for path in paths:
        try:
            file_result = extract_file(
                path, config, country=country, default_port=default_port,
                batch_country=batch_country,
            )
        except Exception as exc:
            result.stats.invalid += 1
            result.stats.summary()  # 保持统计对象已初始化
            raise RuntimeError(f"解析 {path.name} 失败：{exc}") from exc
        for ep in file_result.endpoints:
            if ep.key in seen:
                result.stats.duplicate += 1
                continue
            seen.add(ep.key)
            result.endpoints.append(ep)
        result.stats.matched += file_result.stats.matched
        result.stats.invalid += file_result.stats.invalid
        result.stats.private_or_local += file_result.stats.private_or_local
        result.stats.port_filtered += file_result.stats.port_filtered
        result.stats.truncated += file_result.stats.truncated
        result.stats.ip_without_port += file_result.stats.ip_without_port
        result.stats.vless_links += file_result.stats.vless_links
    result.stats.with_country = sum(1 for ep in result.endpoints if ep.country)
    result.stats.without_country = len(result.endpoints) - result.stats.with_country
    return result


def save_endpoints(endpoints: list[Endpoint], download_dir: Path, label: str = "") -> tuple[Path, Path]:
    """保存中间文件：txt 与带元数据的 JSON。返回两个路径。"""
    download_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")
    name = f"extracted_{stamp}" + (f"_{safe_label}" if safe_label else "")
    txt_path = download_dir / f"{name}.txt"
    json_path = download_dir / f"{name}.json"
    txt_path.write_text("\n".join(str(ep) for ep in endpoints) + "\n", encoding="utf-8")
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(endpoints),
        "endpoints": [
            {
                "host": ep.host,
                "port": ep.port,
                "country": ep.country,
                "source": ep.source,
                "kind": ep.kind,
            }
            for ep in endpoints
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return txt_path, json_path
