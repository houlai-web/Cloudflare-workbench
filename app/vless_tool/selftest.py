"""--selftest：用示例数据与模拟内核跑通 提取→生成→测试→导出 全链路。"""

from __future__ import annotations

import base64
import copy
import json
import re
import shutil
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .extractor import (
    KIND_IP_FILLED,
    KIND_PLAIN,
    KIND_VLESS,
    Endpoint,
    ExtractStats,
    extract_file,
    extract_from_text,
    save_endpoints,
)
from .fofa import (
    FOFA_EXCLUDED_ASNS,
    FofaError,
    build_search_query,
    fofa_search_url,
    qbase64_decode,
)
from .nodes import NodeGenerationError, build_xray_config, generate_nodes, parse_vless_link
from .results import ERROR_REASON_CN, export_links, host_country_line, reason_cn, save_results
from .services import (
    ServiceError,
    Workbench,
    build_publish_link,
    build_publish_payload,
    build_publish_url,
    normalize_publish_domain,
    normalize_publish_token,
    validate_publish_filename,
)
from .xray import cleanup_all_processes, simulate_results


class SelfTestFailure(AssertionError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestFailure(message)


def _run_fofa_search_checks(checks: list[str]) -> None:
    """FOFA 搜索（第 1 步）：country/region 判定、7 条线路、base64 往返、语义说明。"""
    hk = build_search_query(line="any", place="HK", port="443")
    _check('region="HK"' in hk["query"], f"香港必须用 region 字段：{hk['query']}")
    _check('country="HK"' not in hk["query"], "香港不应使用 country 字段")
    for code in ("TW", "MO"):
        item = build_search_query(place=code)
        _check(f'region="{code}"' in item["query"], f"{code} 必须用 region 字段")
    for code in ("US", "KR", "JP", "GB", "DE"):
        item = build_search_query(place=code)
        _check(f'country="{code}"' in item["query"], f"{code} 必须用 country 字段")
        _check(f'region="{code}"' not in item["query"], f"{code} 不应使用 region 字段")
    checks.append("FOFA 搜索：HK/TW/MO→region，US/KR/JP/GB/DE→country 判定正确")

    us = build_search_query(line="any", place="US", port="443")
    expected_head = 'server=="cloudflare" && country="US" && port="443" && header="Forbidden"'
    _check(us["query"].startswith(expected_head), f"US/443 语句头不符：{us['query']}")
    _check("asn==" not in us["query"], "「全部(不限ASN)」不应拼 asn== 子句")
    _check('asn!="13335"' in us["query"], "固定排除 ASN 丢失")
    _check(all(f'asn!="{code}"' in us["query"] for code in FOFA_EXCLUDED_ASNS),
           "排除 ASN 列表不完整")
    _check(us["url"] == fofa_search_url(us["query"]), "搜索链接与查询语句不一致")
    _check(us["url"].startswith("https://fofa.info/result?qbase64="), "搜索链接前缀错误")
    _check("=" not in us["qbase64"], "qbase64 不应包含 = 填充")
    _check(qbase64_decode(us["qbase64"]) == us["query"], "qbase64 解码回原语句失败")
    _check("国家 美国" in us["semantic"] and "443" in us["semantic"], "语义说明缺少国家/端口")
    checks.append(f"FOFA 搜索：US/443 → {us['query']}")

    expected_asns = {
        "mobile": ("45102", "37963", "45090"),
        "unicom": ("132203", "396982", "31898"),
        "telecom": ("45102", "55990", "37963"),
        "tier1": ("45102", "132203", "45090", "37963"),
        "tier2": ("55990", "396982", "31898"),
        "overseas": ("25820", "16276", "16509", "20473", "24940", "63949", "14061", "36352"),
    }
    for line, asns in expected_asns.items():
        item = build_search_query(line=line, place="HK", port="443")
        clause = "(" + " || ".join(f'asn=="{code}"' for code in asns) + ")"
        _check(clause in item["query"], f"线路 {line} 的 ASN 组合不符：{item['query']}")
        _check('header="Forbidden"' in item["query"], f"线路 {line} 缺少 header 条件")
    checks.append("FOFA 搜索：7 条线路中 6 条的 ASN 组合与 header 条件均正确")

    empty = build_search_query(line="any", place="", port="standard")
    _check("port=" not in empty["query"] and "region=" not in empty["query"]
           and "country=" not in empty["query"],
           f"不限地区+全部端口不应出现 port/region/country 子句：{empty['query']}")
    nonstandard = build_search_query(place="US", port="nonstandard")
    _check('port!="443"' in nonstandard["query"], "非标端口应排除标准端口")
    checks.append("FOFA 搜索：不限地区/全部端口/非标端口拼装正确")

    for bad in (
        lambda: build_search_query(line="tetris"),
        lambda: build_search_query(port="99999"),
        lambda: build_search_query(place="XX"),
    ):
        try:
            bad()
        except FofaError:
            continue
        raise SelfTestFailure("非法 FOFA 选项未报错")
    checks.append("FOFA 搜索：非法线路/端口/地区取值均给出明确错误")

    # 失败原因中文映射
    for code, word in ERROR_REASON_CN.items():
        if not code:
            continue
        _check(reason_cn(code) == word, f"失败原因映射不一致：{code} → {reason_cn(code)}")
    _check(reason_cn("") == "", "空原因应为空字符串")
    checks.append(f"失败原因中文映射：{len([c for c in ERROR_REASON_CN if c])} 项全部一致")


def _run_multi_sample_checks(checks: list[str]) -> None:
    """100 个 IP × 3 条模板：顺序循环与随机分配的模板序号、计数、参数隔离。"""
    endpoints = [
        Endpoint(host=f"198.51.{100 + idx // 250}.{idx % 250 + 1}", port=443, country="JP")
        for idx in range(100)
    ]
    samples = [
        "vless://11111111-2222-3333-4444-555555555555@example.com:443"
        "?encryption=none&security=tls&sni=ws.example.com&type=ws&host=ws.example.com"
        "&path=%2Fws&fp=chrome&alpn=h2%2Chttp%2F1.1#ws",
        "vless://22222222-3333-4444-5555-666666666666@example.com:443"
        "?encryption=none&security=tls&sni=grpc.example.com&type=grpc&serviceName=gsvc"
        "&fp=safari&alpn=h2#grpc",
        "vless://33333333-4444-5555-6666-777777777777@example.com:443"
        "?encryption=none&security=reality&sni=reality.example.com&type=tcp"
        "&flow=xtls-rprx-vision&fp=firefox&pbk=PUBKEY&sid=abcd#reality",
    ]
    seq = generate_nodes(endpoints, "", "T", limit=100, samples=samples, mode="sequential")
    _check(len(seq) == 100, f"顺序模式应生成 100 个节点，实际 {len(seq)}")
    _check([n.template_index for n in seq] == [idx % 3 + 1 for idx in range(100)],
           "顺序模式的模板序号不是严格 1,2,3,1,2,3…")
    checks.append("多示例·顺序循环：100 个节点的模板序号严格 1,2,3,1,2,3…")

    rnd = generate_nodes(endpoints, "", "T", limit=100, samples=samples, mode="random", seed=2026)
    counts = [0, 0, 0]
    for node in rnd:
        counts[node.template_index - 1] += 1
    _check(sum(counts) == 100, f"随机模式节点计数之和不等于 100：{counts}")
    _check(all(count > 0 for count in counts), f"随机模式有条模板未被使用：{counts}")
    _check(max(counts) - min(counts) <= 1, f"随机模式分布偏差过大：{counts}")
    checks.append(f"多示例·随机分配：100 个节点三条模板计数 {counts}（全覆盖且均衡）")

    def key_params(link: str) -> dict[str, str]:
        parsed = parse_vless_link(link)
        return {
            key: parsed.params.get(key, "")
            for key in ("security", "sni", "host", "path", "type", "flow", "fp", "alpn",
                        "serviceName", "pbk")
        }

    templates = [key_params(item) for item in samples]
    for node in seq + rnd:
        expected = templates[node.template_index - 1]
        actual = key_params(node.link)
        _check(actual == expected,
               f"节点 {node.index} 的协议参数与第 {node.template_index} 条模板不一致：{actual}")
        _check(f"@{node.address}:{node.port}?" in node.link, "地址/端口未被替换")
    checks.append("多示例：security/sni/host/path/type/flow/fp/alpn 与各模板一致，无串参")

    try:
        generate_nodes(endpoints, "", "T", limit=100, samples=[samples[0], "vless://bad"],
                       mode="sequential")
    except NodeGenerationError as exc:
        _check("第 2 条" in str(exc), f"多示例校验错误未指出行号：{exc}")
    else:
        raise SelfTestFailure("非法示例未被拦截")
    checks.append("多示例：逐条校验，失败信息带第 N 条序号")


def _run_export_checks(cfg: Config, nodes: list, checks: list[str]) -> None:
    """导出：分享链接只返回文本；导出API/导出选中按页面排序且不过滤失败；空勾选被拦截。"""
    bench = Workbench(cfg)
    bench.nodes = list(nodes)
    bench.results = simulate_results(list(nodes))
    ok = [r for r in bench.results if r.latency_ms is not None]
    _check(bool(ok), "导出校验需要至少 1 个成功节点")

    share = bench.export("share", sort_field="latency", sort_dir="asc")
    _check(share["count"] == len(ok), f"分享链接条数应为优选数 {len(ok)}：{share['count']}")
    _check(
        all(line.startswith("vless://") for line in share["text"].splitlines()),
        "分享链接每行都应以 vless:// 开头",
    )

    api = bench.export("api", sort_field="speed", sort_dir="desc")
    api_path = Path(api["items"][0]["path"])
    api_lines = api_path.read_text(encoding="utf-8").splitlines()
    _check(len(api_lines) == len(ok), "导出API 行数应为优选数")
    _check(all(not line.endswith("#") and "#-" not in line for line in api_lines), "导出API 出现残缺国家后缀")

    sel = bench.export("selected", indices=[r.node.index for r in bench.results])
    _check(sel["count"] == len(bench.results), "导出选中不应过滤失败行")
    _check(sel["failed"] == len(bench.results) - len(ok), "未成功条数统计错误")
    sel_lines = Path(sel["items"][0]["path"]).read_text(encoding="utf-8").splitlines()
    _check(all(x.startswith("vless://") for x in sel_lines), "导出选中应为 vless:// 分享链接")
    _check(Path(sel["items"][0]["path"]).name.startswith("选中节点_"), "导出选中文件名用途不符")
    try:
        bench.export("selected", indices=[])
    except ServiceError as exc:
        _check("未勾选任何节点" in str(exc), f"空勾选提示不清楚：{exc}")
    else:
        raise SelfTestFailure("空勾选未被拦截")

    # 「按延迟 TXT」「按速度 TXT」固定按自身语义排序，不受页面排序参数影响
    lat_a = Path(bench.export("latency", sort_field="latency", sort_dir="asc")["items"][0]["path"])
    lat_b = Path(bench.export("latency", sort_field="speed", sort_dir="desc")["items"][0]["path"])
    spd_a = Path(bench.export("speed", sort_field="latency", sort_dir="asc")["items"][0]["path"])
    spd_b = Path(bench.export("speed", sort_field="speed", sort_dir="desc")["items"][0]["path"])
    lat_text = lat_a.read_text(encoding="utf-8")
    spd_text = spd_a.read_text(encoding="utf-8")
    _check(lat_text == lat_b.read_text(encoding="utf-8"), "按延迟 TXT 不应随页面排序变化")
    _check(spd_text == spd_b.read_text(encoding="utf-8"), "按速度 TXT 不应随页面排序变化")
    _check(lat_text != spd_text, "按延迟/按速度 TXT 内容不应相同")
    checks.append(
        "导出：分享链接只返回文本；导出API按页面排序；导出选中为 vless 链接、按页面排序且不过滤失败；"
        "按延迟/按速度 TXT 固定按自身语义排序；空勾选被拦截"
    )


def _run_publish_checks(cfg: Config, results: list, checks: list[str]) -> None:
    """发布：只验内容格式、文件名校验与请求构造，不访问网络（避免自检依赖远端站点）。"""
    # 用独立副本，避免本机 config.json 的 base_url/token 影响断言
    pub_cfg = copy.deepcopy(cfg)
    pub_cfg.data["publish"].update(
        {
            "enabled": True,
            "domain": "txt.example.com",
            "token": "selftest-token",
            "content_mode": "ip_port_country",
            "max_lines": 0,
            "filename_default": "{date}.txt",
        }
    )
    work = cfg.resolve("temp_dir") / f"publish_{datetime.now().strftime('%H%M%S')}"
    work.mkdir(parents=True, exist_ok=True)
    pub_cfg.data["download_dir"] = str(work)
    try:
        # 生成内容格式：IP:端口#国旗-国家，行序沿用传入顺序（页面排序由 sorted_for_export 保证）
        first = host_country_line(results[0])
        _check(first.startswith(f"{results[0].node.address}:{results[0].node.port}#"),
               f"发布内容格式应为 IP:端口#国旗-国家：{first}")
        _check("vless://" not in first, f"默认发布内容不应是 vless：{first}")

        lines = [host_country_line(r) for r in results for _ in range(20)]  # 120 行
        payload = build_publish_payload(lines, 0)
        parts = payload.split("\r\n")
        _check(len(parts) == len(lines), f"max_lines=0 应上传全部 {len(lines)} 行，实际 {len(parts)}")
        _check("\n" not in payload.replace("\r\n", ""), "发布载荷行间应统一使用 \\r\\n")
        truncated = build_publish_payload(lines, 65).split("\r\n")
        _check(len(truncated) == 65, "显式 max_lines=65 时应只取前 65 行")

        name = validate_publish_filename("", pub_cfg)
        now = datetime.now()
        allowed = {
            (now + timedelta(days=delta)).strftime("%Y%m%d")
            for delta in (-1, 0, 1)
        }
        _check(
            name.endswith(".txt") and name[:-4] in allowed,
            f"默认文件名应为服务器本地日期 YYYYMMDD.txt（允许跨零点 ±1 天）：{name}",
        )
        for bad in ("a b.txt", "../x.txt", "a/b.txt", ".hidden", "x.", "a?b.txt", "a&b.txt"):
            try:
                validate_publish_filename(bad, pub_cfg)
            except ServiceError:
                pass
            else:
                raise SelfTestFailure(f"非法文件名未被拦截：{bad}")

        # 域名规范化：裸域名与带 https:// 前缀结果一致，非法输入中文拦截
        bare = normalize_publish_domain("example.test", pub_cfg)
        full = normalize_publish_domain("https://example.test/", pub_cfg)
        _check(bare == "https://example.test", f"裸域名应规范成 https://example.test：{bare}")
        _check(full == bare, f"带 https:// 前缀应得到同一结果：{full} ≠ {bare}")
        for bad in (
            "exa mple.test",
            "<example.test>",
            'ex"ample.test',
            "http://example.test",
            "https://example.test/Path",
        ):
            try:
                normalize_publish_domain(bad, pub_cfg)
            except ServiceError as exc:
                _check("域名" in str(exc) or "https" in str(exc), f"非法域名提示不明确：{bad} → {exc}")
            else:
                raise SelfTestFailure(f"非法域名未被拦截：{bad}")

        # token：非空、禁空格与 & ? /（空值先回落配置；配置也为空则拦截）
        _check(normalize_publish_token("tok123", pub_cfg) == "tok123", "合法 token 被改写")
        no_token_cfg = copy.deepcopy(pub_cfg)
        no_token_cfg.data["publish"]["token"] = ""
        for bad in ("", " ", "has space", "a&b", "a?b", "a/b"):
            try:
                normalize_publish_token(bad, no_token_cfg)
            except ServiceError as exc:
                _check("token" in str(exc), f"非法 token 提示不明确：{bad!r} → {exc}")
            else:
                raise SelfTestFailure(f"非法 token 未被拦截：{bad!r}")

        # 三项可配后链接逐字符一致（https:// 与 / ?token= 固定）
        precise = copy.deepcopy(pub_cfg)
        precise.data["publish"].update(
            {"domain": "example.test", "token": "tok123", "filename_default": "20261105.txt"}
        )
        exact = build_publish_link(precise, "20261105.txt", domain="https://example.test", token="tok123")
        _check(
            exact == "https://example.test/20261105.txt?token=tok123",
            f"短链接逐字符不符：{exact}",
        )

        url, masked = build_publish_url(pub_cfg, name, payload.encode("utf-8"))
        parsed = urllib.parse.urlparse(url)
        _check(parsed.scheme == "https", f"发布地址应为 https：{url[:80]}")
        _check(parsed.netloc == "txt.example.com", f"发布域名不符：{parsed.netloc}")
        _check(urllib.parse.unquote(parsed.path) == f"/{name}", f"发布路径不符：{parsed.path}")
        query = urllib.parse.parse_qs(parsed.query)
        _check(query.get("token") == ["selftest-token"], "发布 token 参数不符")
        decoded = base64.b64decode(query["b64"][0]).decode("utf-8")
        _check(decoded == payload, "发布 b64 解码后与载荷不一致")
        _check("***" in masked and "selftest-token" not in masked, "脱敏地址泄露 token")

        # 回显/复制用短链接：只有文件名 + token，不含 b64（上传 URL 仍单独返回）
        link = build_publish_link(pub_cfg, name)
        link_parsed = urllib.parse.urlparse(link)
        _check(len(link) < 120, f"短链接应小于 120 字符：{len(link)} → {link}")
        _check("b64" not in link, f"短链接不应含 b64：{link}")
        _check("token=selftest-token" in link, f"短链接应含 token 明文：{link}")
        _check(link_parsed.netloc == "txt.example.com", f"短链接域名不符：{link_parsed.netloc}")
        _check(urllib.parse.unquote(link_parsed.path) == f"/{name}", f"短链接路径不符：{link_parsed.path}")
        _check(link != url, "短链接不应与上传 URL 混用")
        cn_link = build_publish_link(pub_cfg, "已有文件.txt")
        _check("%E5%B7%B2" in cn_link, f"中文文件名应以 URL 编码形式出现：{cn_link}")
        _check(
            urllib.parse.unquote(urllib.parse.urlparse(cn_link).path) == "/已有文件.txt",
            f"中文文件名编码后应可还原：{cn_link}",
        )

        # 文件来源：逐字节原样上传（用 b64 反解验证，不发起网络请求）
        raw = "1.1.1.1:443#🇺🇸-美国\r\n2.2.2.2:8443\r\n".encode("utf-8")
        _, raw_masked = build_publish_url(pub_cfg, "已有文件.txt", raw)
        raw_query = urllib.parse.parse_qs(urllib.parse.urlparse(raw_masked).query)
        _check(raw_query.get("token") == ["***"], "文件来源 URL 构造异常")
        file_url, _ = build_publish_url(pub_cfg, "已有文件.txt", raw)
        file_b64 = urllib.parse.parse_qs(urllib.parse.urlparse(file_url).query)["b64"][0]
        _check(base64.b64decode(file_b64) == raw, "文件来源 b64 反解应与原字节完全相同")

        empty = Workbench(pub_cfg)
        try:
            empty.publish(filename=name)
        except ServiceError as exc:
            _check("没有优选节点" in str(exc), f"无结果时发布提示不清楚：{exc}")
        else:
            raise SelfTestFailure("无结果时发布未被拦截")

        # 空内容文件也必须拦截（在联网前报错）
        (work / "empty.txt").write_bytes(b"   \r\n")
        try:
            empty.publish(source="file", filename=name, file_name="empty.txt")
        except ServiceError as exc:
            _check("内容为空" in str(exc), f"空文件发布提示不清楚：{exc}")
        else:
            raise SelfTestFailure("空内容文件未被拦截")
        try:
            empty.publish(source="file", filename=name, file_name="missing.txt")
        except ServiceError as exc:
            _check("找不到文件" in str(exc), f"缺失文件发布提示不清楚：{exc}")
        else:
            raise SelfTestFailure("缺失文件未被拦截")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    checks.append(
        "发布：默认 {date}.txt（当天 YYYYMMDD.txt）、max_lines=0 不截断、CRLF 拼接、"
        "文件名非法拦截、域名规范化（裸域名/https 前缀一致）、token 校验、"
        "链接拼装逐字符一致、Base64 可还原（含文件原样字节）、token 脱敏（未联网）、"
        "回显短链接 <120 字符且不含 b64（上传 URL 单独返回）"
    )


def _run_extract_input_checks(checks: list[str]) -> None:
    """纯 IP / vless 链接 / 混合样本的提取规则与误抓防护。"""
    uuid = "11111111-2222-3333-4444-555555555555"

    def extract(text: str, default_port: int = 443):
        stats = ExtractStats()
        eps = extract_from_text(
            text,
            port_allowlist=[],
            port_blocklist=[],
            exclude_private_ip=True,
            max_items=0,
            default_port=default_port,
            seen=set(),
            stats=stats,
        )
        return eps, stats

    sample_a = "\n".join(
        [
            "1.2.3.4",
            "5.6.7.8",
            "192.168.1.1",
            "10.0.0.1",
            "127.0.0.1",
            "999.999.999.999",
            "1.2.3",
            "v1.2.3.4",
            "/api/v1/1.2.3.4/status",
            "12:34:56",
            "2606:4700:4700::1111",
            "::1",
        ]
    )
    eps_a, stats_a = extract(sample_a)
    _check(len(eps_a) == 3, f"纯 IP 样本应保留 3 条，实际 {len(eps_a)}：{eps_a}")
    _check(all(ep.port == 443 and ep.kind == KIND_IP_FILLED for ep in eps_a),
           f"纯 IP 未按 443 补端口或来源标记错误：{[(str(e), e.kind) for e in eps_a]}")
    _check(stats_a.ip_without_port == 3, f"补端口计数应为 3，实际 {stats_a.ip_without_port}")
    _check(stats_a.private_or_local == 4, f"私网/本机应过滤 4 条，实际 {stats_a.private_or_local}")
    _check(stats_a.invalid == 1, f"非法 IP 应过滤 1 条，实际 {stats_a.invalid}")
    # 候选数固定为 8：6 个 IPv4（含 1 个非法）+ 2 个 IPv6；版本号/URL 路径若被误抓会更多
    _check(stats_a.matched == 8, f"纯 IP 候选数应为 8（噪声未误抓），实际 {stats_a.matched}")
    checks.append(
        f"提取-A 纯IP：2 个 IPv4 + 1 个 IPv6 补 443（ip_without_port={stats_a.ip_without_port}），"
        f"私网 {stats_a.private_or_local} 条/非法 {stats_a.invalid} 条被过滤，"
        "版本号/URL 路径/12:34:56 时间戳均未误抓"
    )

    sample_b = "\n".join(
        [
            f"vless://{uuid}@1.2.3.4:443?security=tls&type=ws#ws",
            f"vless://{uuid}@5.6.7.8:8443?security=reality&sni=example.com#reality",
            f"vless://{uuid}@cdn.example.com:443?type=ws&security=tls#domain",
            f"vless://{uuid}@[2606:4700:4700::1111]:8443?security=tls#v6",
            f"vless://{uuid}@9.10.11.12?security=tls#noport",
            f"vless://{uuid}@8.8.4.4:0?security=tls#portzero",
        ]
    )
    eps_b, stats_b = extract(sample_b, default_port=8443)
    _check(stats_b.vless_links == 6, f"vless 链接计数应为 6，实际 {stats_b.vless_links}")
    _check(len(eps_b) == 6, f"vless 样本应提取 6 条，实际 {len(eps_b)}：{eps_b}")
    _check(all(ep.kind == KIND_VLESS for ep in eps_b), "vless 链接来源标记不是 vless链接")
    _check(stats_b.ip_without_port == 2, f"缺端口/端口 0 应补 2 条，实际 {stats_b.ip_without_port}")
    by_host = {ep.host: ep for ep in eps_b}
    _check(by_host["9.10.11.12"].port == 8443 and by_host["8.8.4.4"].port == 8443,
           "缺端口或端口 0 未按默认端口 8443 补齐")
    _check("cdn.example.com" in by_host, "vless 的域名端点被当成非法丢弃")
    v6 = next((ep for ep in eps_b if ":" in ep.host), None)
    _check(v6 is not None and v6.port == 8443, "vless 的 IPv6 端点端口错误")
    checks.append(
        f"提取-B vless：{stats_b.vless_links} 条链接全部解析为 host:port；"
        f"缺端口/端口 0 共 {stats_b.ip_without_port} 条按默认 8443 补齐；域名与 IPv6 端点保留"
    )

    sample_c = "\n".join(
        [
            "1.2.3.4:443",
            "5.6.7.8",
            "cdn.example.com:8443",
            f"vless://{uuid}@1.2.3.4:443?security=tls#dup",
            f"vless://{uuid}@9.10.11.12:443?security=tls#mix",
            "噪声：版本 v7.8.9.10，路径 /api/v1/7.8.9.10/status，时间 12:34:56。",
        ]
    )
    eps_c, stats_c = extract(sample_c)
    _check(len(eps_c) == 4, f"混合样本应提取 4 条，实际 {len(eps_c)}：{eps_c}")
    _check(stats_c.duplicate == 1, f"vless 与明文同 host:port 应去重 1 条，实际 {stats_c.duplicate}")
    _check(stats_c.vless_links == 2, f"混合样本 vless 链接计数应为 2，实际 {stats_c.vless_links}")
    _check(stats_c.ip_without_port == 1, f"混合样本补端口应为 1 条，实际 {stats_c.ip_without_port}")
    by_key = {ep.key: ep for ep in eps_c}
    _check(by_key["1.2.3.4:443"].kind == KIND_PLAIN, "明文 host:port 应优先于 vless 链接")
    _check(by_key["5.6.7.8:443"].kind == KIND_IP_FILLED, "纯 IP 补端口来源标记错误")
    _check("7.8.9.10:443" not in by_key, "混合样本中的版本号/URL 路径被误抓")
    checks.append(
        f"提取-C 混合：明文+vless+纯IP 共 {len(eps_c)} 条；同 host:port 去重 "
        f"{stats_c.duplicate} 条；vless {stats_c.vless_links} 条；噪声 7.8.9.10 未误抓"
    )

    # 默认端口边界：非法值回落 443
    eps_d, _ = extract("1.2.3.4", default_port=0)
    _check(eps_d and eps_d[0].port == 443, "default_port=0 未回落 443")
    eps_d2, _ = extract("1.2.3.4", default_port=70000)
    _check(eps_d2 and eps_d2[0].port == 443, "default_port=70000 未回落 443")
    checks.append("提取-D 默认端口：0 与 70000 越界时回落到 443")

    # 大文件：10 MB 纯文本解析不卡死，报告耗时
    big = "1.2.3.4:443\n" * (10 * 1024 * 1024 // len("1.2.3.4:443\n") + 1)
    big_stats = ExtractStats()
    started = time.perf_counter()
    big_eps = extract_from_text(big, stats=big_stats)
    elapsed = time.perf_counter() - started
    _check(len(big) >= 10 * 1024 * 1024, "大文件样本未达到 10 MB")
    _check(len(big_eps) == 1, f"10 MB 样本应只剩 1 条去重端点，实际 {len(big_eps)}")
    _check(elapsed < 30, f"10 MB 解析耗时异常：{elapsed:.2f}s")
    checks.append(
        f"提取-E 大文件：{len(big) / 1024 / 1024:.1f} MB 纯文本解析耗时 {elapsed:.2f}s，"
        f"候选 {big_stats.matched} 条 / 去重 {big_stats.duplicate} 条，未卡死"
    )


def run_selftest(config: Config) -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 产物统一写入 tmp/selftest，不污染源码目录与正式下载/导出目录
    root = config.resolve("temp_dir") / "selftest" / stamp
    downloads = root / "downloads"
    exports = root / "exports"
    temp = root / "tmp"
    for path in (downloads, exports, temp):
        path.mkdir(parents=True, exist_ok=True)

    checks: list[str] = []
    data = copy.deepcopy(config.data)
    data["download_dir"] = str(downloads)
    data["export_dir"] = str(exports)
    data["temp_dir"] = str(temp)
    cfg = Config(data, config.path)

    print("=" * 64)
    print(" 自检模式：不会访问网络，不会启动真实 Xray 内核")
    print(f" 输出目录：{root}")
    print("=" * 64)

    # sample_data 是随源码打包的固定资源，位于 app/sample_data/；
    # 不依赖 config.base_dir（避免程序根目录变化时找不到）
    sample = Path(__file__).resolve().parent.parent / "sample_data" / "fofa_sample.json"
    _check(sample.exists(), f"缺少示例数据：{sample}")
    local_sample = downloads / "selftest_fofa_sample.json"
    shutil.copy2(sample, local_sample)

    extraction = extract_file(local_sample, cfg, country="日本")
    _check(len(extraction.endpoints) == 6, f"提取数量应为 6，实际 {len(extraction.endpoints)}")
    _check(extraction.stats.private_or_local >= 1, "应过滤 127.0.0.1 等私有地址")
    _check(extraction.stats.invalid >= 1, "应过滤端口 99999 等无效项")
    checks.append("提取：6 条有效 host:port，私有地址/非法端口已过滤")
    ep_txt, ep_json = save_endpoints(extraction.endpoints, downloads, label="JP")
    _check(ep_txt.exists() and ep_json.exists(), "端点中间文件未写入")

    template = str(cfg.get("generate.sample_vless", ""))
    parsed = parse_vless_link(template)
    _check(parsed.params.get("type") == "ws", "示例 type 参数应保留为 ws")
    _check(parsed.params.get("security") == "tls", "示例 security 参数应保留为 tls")
    nodes = generate_nodes(
        extraction.endpoints,
        template,
        "日本",
        limit=6,
        name_template=str(cfg.get("generate.name_template")),
        latency_placeholder=str(cfg.get("generate.latency_placeholder", "待测")),
    )
    _check(len(nodes) == 6, f"应生成 6 个节点，实际 {len(nodes)}")
    for node in nodes:
        _check(f"@{node.address}:{node.port}?" in node.link, f"地址/端口替换失败：{node.link}")
        _check("encryption=none" in node.link, f"协议参数丢失：{node.link}")
        _check("security=tls" in node.link, f"security 参数丢失：{node.link}")
        _check("type=ws" in node.link, f"type 参数丢失：{node.link}")
        _check("path=%2Fws" in node.link, f"path 参数被改动：{node.link}")
        _check(node.link.rstrip().endswith(f"#{node.link.split('#', 1)[1]}"), "节点名缺失")
    checks.append("生成：6 个节点，仅替换地址/端口，query 参数完整保留")

    wrong = "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443?type=ws"
    try:
        parse_vless_link(wrong)
    except NodeGenerationError as exc:
        _check("encryption" in str(exc), "缺参错误提示应说明缺少 encryption")
    else:
        raise SelfTestFailure("缺参示例未报错")
    checks.append("容错：缺少 encryption/security 时给出明确错误")

    results = simulate_results(nodes)
    _check(len(results) == 6, "模拟测试结果数量不符")
    _check(sum(1 for r in results if r.latency_ms is not None) >= 1, "应至少有一个可用节点")
    _check(any(r.error_type == "connection_timeout" for r in results), "模拟超时分类未生效")
    _check(any(r.error_type == "speed_failed" for r in results), "模拟测速失败分类未生效")
    checks.append("测试（模拟内核）：延迟、速度、超时、测速失败分类均生成")

    csv_path, json_path = save_results(results, exports)
    _check(csv_path.exists() and json_path.exists(), "CSV/JSON 结果未写入")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    _check(payload["count"] == 6, "JSON 结果数量不符")
    checks.append("结果：CSV + JSON 已写入且数量正确")

    all_links = export_links(results, exports, label="vless_all", mode="latency")
    _check(len(all_links.read_text(encoding="utf-8").splitlines()) == 6, "全部链接导出行数不符")
    speed_links = export_links(results, exports, label="vless_speed", mode="speed")
    latency_links = export_links(results, exports, label="vless_latency", mode="latency")
    _check(speed_links.exists() and latency_links.exists(), "排序导出失败")
    checks.append("导出：全部/按速度/按延迟 三种 txt 均生成")

    _run_fofa_search_checks(checks)
    _run_extract_input_checks(checks)
    _run_multi_sample_checks(checks)
    _run_export_checks(cfg, nodes, checks)
    _run_publish_checks(cfg, results, checks)

    node = nodes[0]
    xray_cfg = build_xray_config(node, 21001, 21002, str(temp / "selftest_error.log"))
    parsed_cfg = json.loads(json.dumps(xray_cfg, ensure_ascii=False))
    _check(len(parsed_cfg["inbounds"]) == 2, "Xray 配置应包含 socks + http 两个入站")
    vnext = parsed_cfg["outbounds"][0]["settings"]["vnext"][0]
    _check(vnext["address"] == node.address and vnext["port"] == node.port, "Xray 出站地址/端口错误")
    stream = parsed_cfg["outbounds"][0]["streamSettings"]
    _check(stream["network"] == "ws" and stream["security"] == "tls", "Xray streamSettings 协议映射错误")
    _check(stream["wsSettings"]["path"] == "/ws", "Xray ws path 映射错误")
    _check(stream["tlsSettings"]["serverName"] == "example.com", "Xray SNI 映射错误")
    _check(stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"], "Xray ALPN 映射错误")
    checks.append("Xray 配置：本地 socks/http 入站 + VLESS/WS/TLS 出站字段正确")

    cleanup_all_processes()
    checks.append("清理：调用全局子进程清理，无真实内核启动")

    summary = {
        "passed": True,
        "checks": checks,
        "artifacts": str(root),
        "endpoint_files": [str(ep_txt), str(ep_json)],
        "result_files": [str(csv_path), str(json_path)],
        "link_files": [str(all_links), str(speed_links), str(latency_links)],
        "note": "自检使用模拟内核，不代表真实 Xray 内核或网络连通性验证。",
    }
    (root / "selftest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n自检结果：PASS")
    for idx, item in enumerate(checks, start=1):
        print(f"  [{idx}] {item}")
    print(f"\n自检产物：{root}")
    print("注意：本次自检未启动真实 Xray 内核，真实节点可用性需在本地配置后验证。")
    return 0
