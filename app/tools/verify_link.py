"""验证已发布链接是否可访问（纯标准库，无第三方依赖）。

用法:
  python app\\tools\\verify_link.py <完整链接>
  python app\\tools\\verify_link.py --last [--port 8765]
  python app\\tools\\verify_link.py <完整链接> --expect-format ip_port_country --expect-file downloads\\x.txt --json

退出码: 0 = 全部检查通过；1 = 任一检查失败或网络/HTTP 错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_PORT = 8765
MAX_PREVIEW_LINES = 5
MAX_LINE_CHARS = 120

HOST_PORT_RE = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[0-9A-Za-z._-]+):(\d{1,5})(?:#[^\s#]+)?$"
)


def mask_link(url: str, show_token: bool) -> str:
    """默认只保留 token 后 4 位，避免完整密钥出现在终端/JSON 输出。"""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = []
    for key, value in query:
        if key == "token" and not show_token:
            value = "***" + (value[-4:] if len(value) > 4 else "")
        elif key == "b64" and not show_token:
            value = "***"
        masked.append((key, value))
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(masked, safe="*"))
    )


def classify_error(exc: BaseException, timeout: float, host: str) -> str:
    """把网络异常翻成可读中文原因。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}".strip()
    if isinstance(exc, ssl.SSLError):
        return f"TLS 证书/握手失败：{exc}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"连接超时（超过 {timeout:g} 秒）"
    if isinstance(exc, socket.gaierror):
        return f"域名解析失败：{host}"
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
    text = str(reason)
    if "getaddrinfo" in text or "Name or service not known" in text:
        return f"域名解析失败：{host}"
    if "Connection refused" in text or "actively refused" in text:
        return f"连接被拒绝（目标端口未监听）：{host}"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"连接超时（超过 {timeout:g} 秒）"
    return f"网络请求失败：{text}"


def fetch(url: str, timeout: float) -> dict[str, Any]:
    """GET 目标链接，返回状态/头/原始字节/耗时，不抛异常。"""
    host = urllib.parse.urlparse(url).hostname or ""
    started = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "verify_link/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = (time.monotonic() - started) * 1000
            return {
                "status": resp.getcode(),
                "reason": resp.reason,
                "content_type": resp.headers.get("Content-Type", ""),
                "charset": resp.headers.get_content_charset() or "utf-8",
                "raw": raw,
                "elapsed_ms": round(elapsed, 1),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        elapsed = (time.monotonic() - started) * 1000
        return {
            "status": exc.code,
            "reason": exc.reason,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "charset": (exc.headers.get_content_charset() if exc.headers else None) or "utf-8",
            "raw": raw,
            "elapsed_ms": round(elapsed, 1),
            "error": classify_error(exc, timeout, host),
        }
    except BaseException as exc:  # noqa: BLE001 - 所有网络异常统一转中文
        elapsed = (time.monotonic() - started) * 1000
        return {
            "status": None,
            "reason": "",
            "content_type": "",
            "charset": "utf-8",
            "raw": b"",
            "elapsed_ms": round(elapsed, 1),
            "error": classify_error(exc, timeout, host),
        }


def load_last_link(port: int, timeout: float, show_token: bool) -> tuple[str, str]:
    """读取本地服务 /api/state 中的最近一次发布链接。"""
    endpoint = f"http://127.0.0.1:{port}/api/state"
    result = fetch(endpoint, timeout)
    if result["error"]:
        return "", f"无法读取本地服务 {endpoint}：{result['error']}"
    if result["status"] != 200:
        return "", f"本地服务 {endpoint} 返回 HTTP {result['status']}"
    try:
        payload = json.loads(result["raw"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return "", f"本地服务 /api/state 返回的不是有效 JSON：{exc}"
    data = payload.get("data") or {}
    last = data.get("last_publish") or {}
    url = str(last.get("url") or "").strip()
    if not url:
        return "", (
            "服务端 /api/state 里没有最近一次发布链接；"
            "请先在工作台发布一次，或直接把完整链接作为参数传入"
        )
    return url, ""


def preview_lines(lines: list[str], limit: int = MAX_PREVIEW_LINES) -> list[str]:
    out = []
    for idx, line in enumerate(lines[:limit], 1):
        text = line.rstrip("\r")
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "…"
        out.append(f"{idx}. {text}")
    return out


def format_line_errors(lines: list[str]) -> list[str]:
    errors = []
    for idx, line in enumerate(lines, 1):
        text = line.rstrip("\r")
        if not text.strip():
            continue
        match = HOST_PORT_RE.match(text)
        if not match or not (1 <= int(match.group(1)) <= 65535):
            errors.append(f"第 {idx} 行: {text[:MAX_LINE_CHARS]}")
    return errors


def compare_with_file(raw: bytes, expected_path: str) -> tuple[bool, str]:
    try:
        with open(expected_path, "rb") as handle:
            expected = handle.read()
    except OSError as exc:
        return False, f"读取期望文件失败：{exc}"
    if raw == expected:
        return True, f"与 {expected_path} 逐字节一致（{len(raw)} 字节）"
    raw_lines = raw.decode("utf-8", "replace").splitlines()
    exp_lines = expected.decode("utf-8", "replace").splitlines()
    for idx, (got, want) in enumerate(zip(raw_lines, exp_lines), 1):
        if got != want:
            return False, (
                f"与 {expected_path} 不一致：第 {idx} 行不同"
                f"（远端「{got[:60]}」，期望「{want[:60]}」）"
            )
    return False, (
        f"与 {expected_path} 不一致：远端 {len(raw_lines)} 行 / "
        f"期望 {len(exp_lines)} 行"
    )


def build_report(url: str, show_token: bool, args: argparse.Namespace) -> dict[str, Any]:
    result = fetch(url, args.timeout)
    raw = result["raw"]
    try:
        text = raw.decode(result["charset"], "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    if result["error"]:
        if result["status"] is None:
            add("网络请求", False, result["error"])
        else:
            add("HTTP 状态", False, f"HTTP {result['status']} {result['reason']}".strip())
    else:
        add("HTTP 状态", 200 <= (result["status"] or 0) < 300,
            f"HTTP {result['status']} {result['reason']}".strip())

    non_empty = bool(raw.strip())
    add("内容非空", non_empty, f"{len(raw)} 字节，{len(lines)} 行" if non_empty else "内容为空")

    invalid: list[str] = []
    if args.expect_format == "ip_port_country":
        invalid = format_line_errors(lines)
        add(
            "格式 ip_port_country",
            non_empty and not invalid,
            "全部行符合 IP或域名:端口[#国旗-国家]" if not invalid else
            f"{len(invalid)} 行不符合：" + "；".join(invalid[:5]),
        )

    if args.expect_file:
        if non_empty:
            ok, detail = compare_with_file(raw, args.expect_file)
        else:
            ok, detail = False, "远端内容为空，无法与期望文件比对"
        add("与期望文件一致", ok, detail)

    return {
        "link": mask_link(url, show_token),
        "status": result["status"],
        "reason": result["reason"],
        "elapsed_ms": result["elapsed_ms"],
        "content_type": result["content_type"],
        "bytes": len(raw),
        "lines": len(lines),
        "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
        "first_lines": preview_lines(lines),
        "checks": checks,
        "ok": all(item["ok"] for item in checks) and bool(checks),
    }


def print_human(report: dict[str, Any]) -> None:
    print(f"链接：{report['link']}")
    status = "无响应" if report["status"] is None else f"HTTP {report['status']} {report['reason']}".strip()
    print(f"状态：{status}  耗时：{report['elapsed_ms'] / 1000:.2f}s  "
          f"Content-Type：{report['content_type'] or '-'}")
    print(f"内容：{report['bytes']} 字节 / {report['lines']} 行")
    if report["first_lines"]:
        print("前 5 行：")
        for line in report["first_lines"]:
            print(f"  {line}")
    print("检查：")
    for item in report["checks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        print(f"  [{mark}] {item['name']}：{item['detail']}")
    print("结果：" + ("全部通过" if report["ok"] else "存在失败项"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证已发布链接是否可访问")
    parser.add_argument("link", nargs="?", help="完整链接（含 token/b64）")
    parser.add_argument("--last", action="store_true", help="从本地服务 /api/state 取最近一次发布链接")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"本地服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--expect-file", help="远端内容需与该文件逐字节一致")
    parser.add_argument("--expect-format", choices=["ip_port_country"],
                        help="校验每行为 IP或域名:端口 或 IP:端口#国旗-国家")
    parser.add_argument("--timeout", type=float, default=15, help="请求超时秒数（默认 15）")
    parser.add_argument("--show-token", action="store_true", help="输出完整 token（默认只显示后 4 位）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    url = (args.link or "").strip()
    if args.last:
        url, error = load_last_link(args.port, args.timeout, args.show_token)
        if error:
            if args.json:
                print(json.dumps({"ok": False, "error": error, "checks": []}, ensure_ascii=False, indent=2))
            else:
                print(error, file=sys.stderr)
            return 1
    if not url:
        parser.print_usage(sys.stderr)
        print("错误：请传入完整链接，或使用 --last 读取最近一次发布链接", file=sys.stderr)
        return 1

    report = build_report(url, args.show_token, args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
