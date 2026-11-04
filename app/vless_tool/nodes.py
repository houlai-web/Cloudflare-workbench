"""解析示例 VLESS 链接并批量替换地址/端口生成新节点。"""

from __future__ import annotations

import ipaddress
import random
import re
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from .extractor import Endpoint


SUPPORTED_NETWORKS = {"tcp", "ws", "grpc", "http", "h2", "kcp", "quic", "xhttp"}
SUPPORTED_SECURITY = {"none", "tls", "reality"}


class NodeGenerationError(ValueError):
    """示例链接不合法或缺少参数时抛出，message 直接面向用户。"""


@dataclass
class VlessTemplate:
    link: str
    uuid: str
    address: str
    port: int
    query: str
    path: str
    params: dict[str, str]
    userinfo: str
    original_name: str

    @property
    def encryption(self) -> str:
        return self.params.get("encryption", "")

    @property
    def network(self) -> str:
        return self.params.get("type", "")

    @property
    def security(self) -> str:
        return self.params.get("security", "")


@dataclass
class VlessNode:
    address: str
    port: int
    link: str
    name: str
    region: str
    index: int
    template: VlessTemplate
    name_template: str = "{region}-{index:03d}-{latency}"
    latency_placeholder: str = "待测"
    # 该节点使用的示例模板序号（从 1 开始），便于核对顺序/随机分配
    template_index: int = 1

    @property
    def endpoint_key(self) -> str:
        return f"{self.address.lower()}:{self.port}"

    def name_with(self, latency_label: str) -> str:
        return _safe_format(
            self.name_template,
            region=self.region,
            index=self.index,
            latency=latency_label,
            address=self.address,
            port=self.port,
        )


def _validate_address(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    labels = value.split(".")
    if len(labels) < 2:
        return False
    if any(not label or len(label) > 63 for label in labels):
        return False
    if not re.fullmatch(r"[A-Za-z]{2,63}", labels[-1]):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", lb) for lb in labels)


def parse_vless_link(raw: str, strict_uuid: bool = True) -> VlessTemplate:
    """解析并校验示例链接，尽量一次报告所有缺失/错误项。"""
    raw = raw.strip()
    if raw.startswith("vless://") is False:
        raise NodeGenerationError("示例链接必须以 vless:// 开头")
    parts = urlsplit(raw)
    if parts.scheme.lower() != "vless":
        raise NodeGenerationError("示例链接协议不是 vless://")

    params = {str(k).lower(): str(v) for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    errors: list[str] = []

    uuid_value = unquote(parts.username or "")
    if not uuid_value:
        errors.append("缺少用户 UUID（vless://UUID@地址:端口）")
    elif strict_uuid:
        try:
            parsed_uuid = uuid_lib.UUID(uuid_value)
            if str(parsed_uuid) != uuid_value.lower():
                raise ValueError
        except ValueError:
            errors.append(f"UUID 格式不合法：{uuid_value}（需为标准 8-4-4-4-12 格式）")

    address = parts.hostname or ""
    if not address:
        errors.append("缺少服务器地址（hostname）")
    elif not _validate_address(address):
        errors.append(f"服务器地址不合法：{address}")

    if parts.port is None:
        errors.append("缺少服务器端口")
        port = 0
    else:
        port = int(parts.port)
        if port < 1 or port > 65535:
            errors.append(f"端口超出范围：{port}")

    for key, label in (
        ("encryption", "encryption（VLESS 必须为 none）"),
        ("type", "type（传输类型，如 ws/grpc/tcp）"),
        ("security", "security（安全类型，如 tls/reality/none）"),
    ):
        if key not in params or not params[key]:
            errors.append(f"缺少必填参数 {label}")
    if params.get("encryption") and params["encryption"].lower() != "none":
        errors.append(f"VLESS 的 encryption 必须为 none，当前为 {params['encryption']}")
    network = params.get("type", "").lower()
    if network and network not in SUPPORTED_NETWORKS:
        errors.append(
            f"暂不支持 type={network}，支持：{', '.join(sorted(SUPPORTED_NETWORKS))}"
        )
    security = params.get("security", "").lower()
    if security and security not in SUPPORTED_SECURITY:
        errors.append(
            f"暂不支持 security={security}，支持：{', '.join(sorted(SUPPORTED_SECURITY))}"
        )
    if security == "reality":
        if not params.get("pbk"):
            errors.append("security=reality 缺少 pbk（公钥）")
        if not params.get("sni") and not params.get("host"):
            errors.append("security=reality 缺少 sni 或 host")

    if errors:
        raise NodeGenerationError(
            "示例 VLESS 链接不完整：\n  - " + "\n  - ".join(errors)
            + "\n请补充后重试；配置 generate.sample_vless 也可写入完整示例。"
        )

    # 保留原始 userinfo（通常是 UUID），重建时只替换 host:port
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"

    return VlessTemplate(
        link=raw,
        uuid=uuid_value,
        address=address,
        port=port,
        query=parts.query,
        path=parts.path or "",
        params=params,
        userinfo=userinfo,
        original_name=unquote(parts.fragment or ""),
    )


def _format_host_for_authority(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def replace_endpoint(link: str, new_host: str, new_port: int) -> str:
    """保留原链接所有参数，仅替换 netloc 中的主机与端口。"""
    parts = urlsplit(link)
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    netloc = f"{userinfo}{_format_host_for_authority(new_host)}:{new_port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def rename_link(link: str, name: str) -> str:
    """替换链接的 # 备注名，不改变任何协议参数。"""
    base = link.split("#", 1)[0]
    return f"{base}#{quote(name, safe='')}"


def _safe_format(template: str, **values: Any) -> str:
    """允许模板引用未知占位符时给出清晰错误。"""
    try:
        name = template.format(**values)
    except KeyError as exc:
        raise NodeGenerationError(
            f"节点名称包含未知占位符：{exc}（可用 region/index/latency/address/port）"
        ) from exc
    except (ValueError, IndexError) as exc:
        raise NodeGenerationError(f"节点名称格式错误：{exc}") from exc
    name = re.sub(r"\s+", "-", name.strip())
    return name or f"{values.get('region', 'NODE')}-{values.get('index', 0):03d}"


ASSIGN_MODES = ("random", "sequential")


def normalize_samples(sample_link: str = "", samples: list[str] | None = None) -> list[str]:
    """合并旧单示例字段与多示例列表，去空白并保持输入顺序。"""
    raw = list(samples or [])
    if sample_link and sample_link.strip():
        raw.insert(0, sample_link)
    cleaned: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def parse_samples(
    samples: list[str], strict_uuid: bool = True
) -> list[VlessTemplate]:
    """逐条校验示例，错误信息带序号，方便前端标红对应行。"""
    if not samples:
        raise NodeGenerationError("请至少填写 1 条示例 vless:// 链接")
    parsed: list[VlessTemplate] = []
    for idx, link in enumerate(samples, start=1):
        try:
            parsed.append(parse_vless_link(link, strict_uuid=strict_uuid))
        except NodeGenerationError as exc:
            message = str(exc).replace("\n", " ").strip()
            raise NodeGenerationError(f"第 {idx} 条示例校验失败：{message}") from exc
    return parsed


def _template_plan(total: int, count: int, mode: str, rng: random.Random) -> list[int]:
    """分配计划：顺序循环按 1,2,3,1,2,3…；随机按洗牌分桶保证每条模板都被用到。"""
    if count < 1:
        return []
    if mode == "sequential":
        return [idx % count for idx in range(total)]
    plan: list[int] = []
    while len(plan) < total:
        bucket = list(range(count))
        rng.shuffle(bucket)
        plan.extend(bucket)
    return plan[:total]


def generate_nodes(
    endpoints: list[Endpoint],
    sample_link: str,
    region: str,
    *,
    limit: int = 50,
    name_template: str = "{region}-{index:03d}-{latency}",
    latency_placeholder: str = "待测",
    strict_uuid: bool = True,
    samples: list[str] | None = None,
    mode: str = "random",
    seed: int | None = None,
) -> list[VlessNode]:
    """按端点批量生成节点；保留示例的协议参数与路径。

    多示例：samples 非空时按 mode 给每个端点分配一条模板（只替换地址/端口）；
    sample_link 仍然可用，作为兼容旧配置/CLI 的单示例入口。
    """
    if not endpoints:
        raise NodeGenerationError("端点列表为空，请先执行第 2 步提取 IP:端口")
    if mode not in ASSIGN_MODES:
        raise NodeGenerationError(
            f"分配模式不合法：{mode}；可选 random（随机分配）/ sequential（顺序循环）"
        )
    # samples 已由调用方合并/去空时优先使用，避免把 sample_link 重复计入模板
    if samples:
        links = [str(item).strip() for item in samples if str(item or "").strip()]
    else:
        links = [sample_link.strip()] if sample_link and sample_link.strip() else []
    if not links:
        raise NodeGenerationError("请至少填写 1 条示例 vless:// 链接")
    templates = parse_samples(links, strict_uuid=strict_uuid)
    if limit <= 0:
        limit = len(endpoints)
    plan = _template_plan(limit, len(templates), mode, random.Random(seed))
    nodes: list[VlessNode] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if len(nodes) >= limit:
            break
        host = endpoint.host.strip().strip("[]").rstrip(".")
        if not _validate_address(host):
            continue
        key = f"{host.lower()}:{endpoint.port}"
        if key in seen:
            continue
        seen.add(key)
        index = len(nodes) + 1
        template_index = plan[index - 1] if len(nodes) < len(plan) else 0
        template = templates[template_index]
        node_region = endpoint.country or region
        name = _safe_format(
            name_template,
            region=node_region,
            index=index,
            latency=latency_placeholder,
            address=host,
            port=endpoint.port,
        )
        link = rename_link(replace_endpoint(template.link, host, endpoint.port), name)
        nodes.append(
            VlessNode(
                address=host,
                port=endpoint.port,
                link=link,
                name=name,
                region=node_region,
                index=index,
                template=template,
                name_template=name_template,
                latency_placeholder=latency_placeholder,
                template_index=template_index + 1,
            )
        )
    return nodes


def build_xray_stream_settings(node: VlessNode, template: VlessTemplate | None = None) -> dict[str, Any]:
    """把 vless 分享参数映射为 Xray streamSettings。"""
    tpl = template or node.template
    p = tpl.params
    network = p.get("type", "tcp").lower()
    security = p.get("security", "none").lower()
    host = p.get("host", "")
    sni = p.get("sni") or host or node.address
    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "tls":
        tls_settings: dict[str, Any] = {"serverName": sni}
        if p.get("fp"):
            tls_settings["fingerprint"] = p["fp"]
        if p.get("alpn"):
            tls_settings["alpn"] = [x.strip() for x in p["alpn"].split(",") if x.strip()]
        if p.get("allowInsecure") in {"1", "true", "yes"}:
            tls_settings["allowInsecure"] = True
        stream["tlsSettings"] = tls_settings
    elif security == "reality":
        reality: dict[str, Any] = {
            "serverName": sni,
            "publicKey": p.get("pbk", ""),
            "shortId": p.get("sid", ""),
        }
        if p.get("fp"):
            reality["fingerprint"] = p["fp"]
        if p.get("spx"):
            reality["spiderX"] = p["spx"]
        stream["realitySettings"] = reality

    if network == "ws":
        ws: dict[str, Any] = {"path": p.get("path") or "/"}
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        grpc: dict[str, Any] = {"serviceName": p.get("serviceName") or p.get("path") or ""}
        if p.get("mode"):
            grpc["multiMode"] = p["mode"].lower() == "multi"
        stream["grpcSettings"] = grpc
    elif network in {"http", "h2"}:
        http_settings: dict[str, Any] = {"path": p.get("path") or "/"}
        if host:
            http_settings["host"] = [x.strip() for x in host.split(",") if x.strip()]
        stream["httpSettings"] = http_settings
    elif network == "xhttp":
        xhttp: dict[str, Any] = {"path": p.get("path") or "/"}
        if host:
            xhttp["host"] = host
        if p.get("mode"):
            xhttp["mode"] = p["mode"]
        stream["xhttpSettings"] = xhttp
    elif network == "kcp":
        kcp: dict[str, Any] = {}
        if p.get("seed"):
            kcp["seed"] = p["seed"]
        if p.get("headerType"):
            kcp["header"] = {"type": p["headerType"]}
        stream["kcpSettings"] = kcp
    elif network == "quic":
        quic: dict[str, Any] = {}
        if p.get("quicSecurity"):
            quic["security"] = p["quicSecurity"]
        if p.get("key"):
            quic["key"] = p["key"]
        stream["quicSettings"] = quic
    return stream


def build_vless_outbound(node: VlessNode, tag: str) -> dict[str, Any]:
    """单个 VLESS 出站（多节点共享内核时每个节点一个，tag 各不相同）。"""
    tpl = node.template
    user: dict[str, Any] = {
        "id": tpl.uuid,
        "encryption": "none",
    }
    if tpl.params.get("flow"):
        user["flow"] = tpl.params["flow"]
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": node.address,
                    "port": node.port,
                    "users": [user],
                }
            ]
        },
        "streamSettings": build_xray_stream_settings(node),
    }


def build_xray_config(node: VlessNode, socks_port: int, http_port: int, log_path: str) -> dict[str, Any]:
    """生成单节点测试配置：本地 socks + http 入站，VLESS 出站。"""
    outbound = build_vless_outbound(node, "proxy")
    return {
        "log": {
            "loglevel": "warning",
            "error": log_path,
            "access": "none",
        },
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": False},
            },
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": http_port,
                "protocol": "http",
                "settings": {},
                "sniffing": {"enabled": False},
            },
        ],
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["socks-in", "http-in"], "outboundTag": "proxy"}
            ],
        },
    }


def build_xray_pool_config(
    entries: list[tuple[VlessNode, int]],
    log_path: str,
    inbound: str = "http",
) -> dict[str, Any]:
    """多节点共享内核配置：一个内核进程，N 个本地入站 + N 个 VLESS 出站。

    每个入站只路由到自己的出站，节点之间互不影响；这样并发测延迟时
    不再需要「每节点一个内核进程」，启动开销只付一次。
    """
    if inbound not in {"http", "socks"}:
        raise NodeGenerationError("test_inbound 只能是 http 或 socks")
    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for idx, (node, port) in enumerate(entries, start=1):
        in_tag = f"in{idx}"
        out_tag = f"out{idx}"
        if inbound == "socks":
            inbound_cfg = {
                "tag": in_tag,
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
                "sniffing": {"enabled": False},
            }
        else:
            inbound_cfg = {
                "tag": in_tag,
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {},
                "sniffing": {"enabled": False},
            }
        inbounds.append(inbound_cfg)
        outbounds.append(build_vless_outbound(node, out_tag))
        rules.append({"type": "field", "inboundTag": [in_tag], "outboundTag": out_tag})
    outbounds.append({"tag": "direct", "protocol": "freedom"})
    return {
        "log": {"loglevel": "warning", "error": log_path, "access": "none"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
