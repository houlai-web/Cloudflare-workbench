"""本地 Web UI：Flask 单页 + JSON API + SSE 流式测试进度。

页面文件在 web/ 目录（index.html），无构建步骤；业务逻辑全部经 services.Workbench 调用既有模块。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import platform
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .config import Config
from .services import (
    MAX_UPLOAD_BYTES,
    ServiceError,
    Workbench,
    list_download_files,
    save_upload,
    import_paste_text,
    import_local_path,
)
from .extractor import Endpoint
from .fofa import FofaError, build_search_query, fofa_options
from .nodes import NodeGenerationError, normalize_samples, parse_vless_link

WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "web"
DEFAULT_THREADS = 8
_EXTERNAL_INDEX_REL = Path("app") / "web" / "index.html"


def _external_index_path() -> Path | None:
    """打包运行时可外部覆盖的 index.html：<exe目录>\\app\\web\\index.html。"""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent / _EXTERNAL_INDEX_REL


def _source_index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "index.html"


def _external_web_path(name: str) -> Path | None:
    """打包运行时可外部覆盖的页面文件：<exe目录>\\app\\web\\<name>。"""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent / "app" / "web" / name


def _resolve_nav_html() -> tuple[Path, str]:
    """导航页 nav.html：外部覆盖 → 包内副本 → 源码目录。"""
    external = _external_web_path("nav.html")
    bundled = WEB_DIR / "nav.html"
    if external is not None:
        if external.is_file():
            return external, "外部"
        if bundled.is_file():
            return bundled, "包内（外部缺失）"
    elif bundled.is_file():
        return bundled, "源码"
    return bundled, "缺失"


def _resolve_index_html() -> tuple[Path, str]:
    """按优先级返回 (index.html 路径, 来源说明)。

    1. 打包且外部存在 → <exe目录>\\app\\web\\index.html（用户可直接修改）
    2. 包内副本 → _MEIPASS\\web\\index.html
    3. 源码运行 → 仓库 app/web/index.html
    """
    external = _external_index_path()
    if external is not None:
        if external.is_file():
            return external, "外部"
        bundled = WEB_DIR / "index.html"
        if bundled.is_file():
            return bundled, "包内（外部缺失）"
    else:
        bundled = WEB_DIR / "index.html"
        if bundled.is_file():
            return bundled, "源码"
    source = _source_index_path()
    if source.is_file():
        return source, "源码"
    return WEB_DIR / "index.html", "缺失"


def _endpoint_json(endpoint: Endpoint, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "host": endpoint.host,
        "port": endpoint.port,
        "country": endpoint.country,
        "source": Path(endpoint.source).name if endpoint.source else "",
        "kind": endpoint.kind,
        "type": "IP" if _is_ip_host(endpoint.host) else "域名",
        "key": endpoint.key,
    }


def _is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _environment_info(config: Config) -> dict[str, Any]:
    """环境探测：以当前解释器 sys.executable 为准，不依赖 PATH 中的 py/py.exe。"""
    exe = Path(sys.executable)
    python = {
        "executable": str(exe),
        "available": exe.is_file(),
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(terse=True),
        "base_dir": str(config.base_dir),
    }
    return {"python": python}


def create_app(config: Config, logger: logging.Logger | None = None) -> Flask:
    logger = logger or logging.getLogger("vless_tool.web")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
    bench = Workbench(config, logger)
    app.extensions["bench"] = bench

    def fail(message: str, status: int = 400):
        return jsonify({"ok": False, "error": message}), status

    @app.errorhandler(ServiceError)
    def _service_error(exc: ServiceError):  # noqa: ANN001
        return fail(str(exc))

    @app.errorhandler(413)
    def _too_large(_exc):  # noqa: ANN001
        return fail("上传文件过大（上限 32 MB）", 413)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception):  # noqa: ANN001
        if isinstance(exc, HTTPException):
            return fail(exc.description or f"HTTP {exc.code}", exc.code or 500)
        logger.exception("Web 请求异常")
        return fail(f"服务器异常：{exc}", 500)

    # ------------------------------------------------------------- 页面
    index_path, index_source = _resolve_index_html()
    logger.info("页面来源：%s（%s）", index_path, index_source)
    if index_source == "包内（外部缺失）":
        logger.warning("外部页面文件不存在，已回落包内副本：%s", index_path)

    @app.get("/")
    def index():
        path, source = _resolve_index_html()
        if not path.is_file():
            logger.error("页面文件缺失：外部与包内 index.html 均不存在（%s）", path)
            return fail("页面文件缺失：index.html 未找到", 404)
        if source == "包内（外部缺失）":
            logger.warning("外部页面文件不存在，已回落包内副本：%s", path)
        try:
            html = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("读取页面失败（%s）：%s", path, exc)
            return fail(f"读取页面失败：{exc}", 500)
        resp = Response(html, mimetype="text/html")
        # 单文件页面，资源全部内联；禁缓存，改外部文件后刷新即生效
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/nav")
    def nav_page():
        path, source = _resolve_nav_html()
        if not path.is_file():
            logger.error("导航页缺失：nav.html 未找到（%s）", path)
            return fail("导航页缺失：nav.html 未找到", 404)
        try:
            html = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("读取导航页失败（%s）：%s", path, exc)
            return fail(f"读取导航页失败：{exc}", 500)
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    # ------------------------------------------------------------- 状态
    @app.get("/api/state")
    def state():
        payload = bench.summary()
        payload["environment"] = _environment_info(config)
        return jsonify({"ok": True, "data": payload})

    @app.get("/api/files")
    def files():
        return jsonify({"ok": True, "data": list_download_files(config)})

    # ------------------------------------------------------------- FOFA 搜索（第 1 步）
    @app.get("/api/fofa/options")
    def fofa_option_list():
        return jsonify({"ok": True, "data": fofa_options()})

    @app.post("/api/fofa/query")
    def fofa_query():
        payload = request.get_json(silent=True) or {}
        try:
            data = build_search_query(
                line=str(payload.get("line") or payload.get("network") or "any"),
                place=str(payload.get("place") or payload.get("region") or ""),
                port=str(payload.get("port") or "443"),
            )
        except FofaError as exc:
            raise ServiceError(str(exc)) from exc
        return jsonify({"ok": True, "data": data})

    # ------------------------------------------------------------- 第 2 步导入
    @app.post("/api/import/upload")
    def upload():
        upload_file = request.files.get("file")
        if not upload_file or not upload_file.filename:
            raise ServiceError("没有收到文件")
        dest = save_upload(config, upload_file.filename, upload_file.read())
        code = (request.form.get("code") or "").strip().upper()
        bench.add_files([dest])
        logger.info("Web 上传文件：%s", dest)
        return jsonify({"ok": True, "data": {"path": str(dest), "name": dest.name, "code": code}})

    @app.post("/api/import/paste")
    def paste():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        code = str(payload.get("code") or "").strip().upper()
        dest = import_paste_text(config, text, code=code)
        bench.add_files([dest])
        return jsonify({"ok": True, "data": {"path": str(dest), "name": dest.name}})

    @app.post("/api/import/local")
    def local_import():
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("path") or "")
        if not raw.strip():
            raise ServiceError("请填写文件路径")
        code = str(payload.get("code") or "").strip().upper()
        dest = import_local_path(config, raw, code=code)
        bench.add_files([dest])
        return jsonify({"ok": True, "data": {"path": str(dest), "name": dest.name}})

    # ------------------------------------------------------------- 第 2 步
    @app.post("/api/extract")
    def extract():
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths") or []
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ServiceError("请至少选择一个文件")
        paths = []
        for item in raw_paths:
            path = Path(str(item))
            if not path.is_absolute():
                path = config.base_dir / path
            if not path.exists():
                raise ServiceError(f"文件不存在：{path}")
            paths.append(path)
        code = str(payload.get("code") or "").strip().upper()
        # 默认端口仅对缺少端口的纯 IP / vless 链接生效；非法值回落到配置默认
        default_port: int | None = None
        if "default_port" in payload and payload.get("default_port") not in (None, ""):
            try:
                default_port = int(payload["default_port"])
            except (TypeError, ValueError):
                raise ServiceError("默认端口必须是 1~65535 的整数") from None
            if not 1 <= default_port <= 65535:
                raise ServiceError("默认端口必须是 1~65535 的整数")
        batch_country = str(payload.get("batch_country") or "").strip().upper()
        data = bench.extract(
            paths, country=code, label=code, default_port=default_port,
            batch_country=batch_country,
        )
        return jsonify({"ok": True, "data": data})

    @app.get("/api/endpoints")
    def endpoints():
        page = max(1, int(request.args.get("page", 1)))
        size = min(2000, max(10, int(request.args.get("size", 50))))
        bench.lock.acquire()
        try:
            items = list(bench.endpoints)
        finally:
            bench.lock.release()
        keyword = (request.args.get("q") or "").strip().lower()
        if keyword:
            items = [e for e in items if keyword in e.key or keyword in (e.country or "").lower()]
        total = len(items)
        start = (page - 1) * size
        chunk = items[start : start + size]
        return jsonify(
            {
                "ok": True,
                "data": {
                    "total": total,
                    "page": page,
                    "size": size,
                    "items": [_endpoint_json(e, start + i + 1) for i, e in enumerate(chunk)],
                },
            }
        )

    # ------------------------------------------------------------- 第 4 步生成
    @app.post("/api/samples/validate")
    def samples_validate():
        payload = request.get_json(silent=True) or {}
        raw = payload.get("samples") or []
        if not isinstance(raw, list):
            raise ServiceError("samples 必须是数组")
        links = normalize_samples(str(payload.get("sample") or ""), [str(x) for x in raw])
        strict = bool(config.get("generate.strict_uuid", True))
        rows: list[dict[str, Any]] = []
        for idx, link in enumerate(links, start=1):
            if not link.strip():
                rows.append({"index": idx, "ok": True, "error": "", "empty": True})
                continue
            try:
                parse_vless_link(link, strict_uuid=strict)
                rows.append({"index": idx, "ok": True, "error": ""})
            except NodeGenerationError as exc:
                message = str(exc).replace("\n", " ").strip()
                rows.append({"index": idx, "ok": False,
                             "error": f"第 {idx} 条示例校验失败：{message}"})
        return jsonify({"ok": True, "data": {"rows": rows, "count": len(links)}})

    @app.post("/api/generate")
    def generate():
        payload = request.get_json(silent=True) or {}
        sample = str(payload.get("sample") or "")
        raw_samples = payload.get("samples") or []
        if not isinstance(raw_samples, list):
            raise ServiceError("samples 必须是数组")
        samples = [str(x) for x in raw_samples]
        region = str(payload.get("region") or "").strip()
        try:
            limit = int(payload.get("limit") or 10)
        except (TypeError, ValueError):
            raise ServiceError("生成数量必须是整数") from None
        name_template = str(payload.get("name_template") or "")
        mode = str(payload.get("mode") or "random").strip().lower()
        data = bench.generate(sample, region, limit, name_template, samples, mode)
        return jsonify({"ok": True, "data": data})

    @app.get("/api/nodes")
    def nodes():
        bench.lock.acquire()
        try:
            items = list(bench.nodes)
        finally:
            bench.lock.release()
        return jsonify(
            {
                "ok": True,
                "data": [
                    {
                        "index": n.index,
                        "name": n.name,
                        "address": n.address,
                        "port": n.port,
                        "region": n.region,
                        "link": n.link,
                        "template_index": n.template_index,
                    }
                    for n in items
                ],
            }
        )

    # ------------------------------------------------------------- 第 4 步
    @app.post("/api/test/start")
    def test_start():
        payload = request.get_json(silent=True) or {}
        raw_indices = payload.get("indices") or []
        indices: list[int] | None = None
        if raw_indices:
            try:
                indices = [int(i) for i in raw_indices]
            except (TypeError, ValueError):
                raise ServiceError("节点序号不合法")
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise ServiceError("测试参数不合法")
        # 测试模式可在 options 或顶层传；默认 latency_speed
        test_mode = payload.get("test_mode") or options.get("test_mode")
        if test_mode is not None:
            options = {**options, "test_mode": str(test_mode)}
        # 模拟测试仅保留在 --selftest 内部，Web 接口一律走真实 Xray
        job = bench.start_test(indices=indices, options=options)
        return jsonify({"ok": True, "data": job})

    @app.post("/api/test/stop")
    def test_stop():
        return jsonify({"ok": True, "data": bench.stop_test()})

    @app.get("/api/test/stream")
    def test_stream():
        job_id = request.args.get("job_id", "")
        if not job_id:
            job = bench.job
            job_id = job.job_id if job else ""
        if not job_id:
            raise ServiceError("没有测试任务")

        try:
            after = int(request.headers.get("Last-Event-ID") or request.args.get("after") or 0)
        except ValueError:
            after = 0

        def stream():
            last = max(0, after)
            last_emit = time.time()
            reason = "client_disconnected"
            bench.logger.info("SSE 开始 job=%s after=%s", job_id, last)
            try:
                # 浏览器断线后按 2 秒重试，并自动带上 Last-Event-ID
                yield "retry: 2000\n\n"
                while True:
                    job, events = bench.job_events_since(job_id, last, timeout=0.5)
                    if job is None:
                        reason = "job_missing"
                        payload = {
                            "type": "done", "done": 0, "total": 0,
                            "stopped": False, "error": "任务不存在或已被新的测试替换",
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        return
                    if events:
                        for event in events:
                            last = int(event["seq"])
                            yield f"id: {last}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                            last_emit = time.time()
                            if event.get("type") in {"done", "error"}:
                                if event.get("type") == "error":
                                    reason = "job_error"
                                elif event.get("stopped"):
                                    reason = "job_stopped"
                                else:
                                    reason = "job_done"
                                return
                        continue
                    if not job.running:
                        # 兜底：任务已结束但 done 事件缺失（异常/新任务替换）
                        reason = "job_finished_without_done"
                        payload = {
                            "type": "done", "index": job.done,
                            "done": job.done, "total": job.total,
                            "stopped": job.stopping, "error": job.error,
                            "ok_count": job.ok_count,
                            "exports": job.exports,
                            "export_error": job.export_error,
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        return
                    # 每 10 秒一条注释心跳，避免代理 / 浏览器把长连接判为空闲
                    if time.time() - last_emit >= 10:
                        yield ": heartbeat\n\n"
                        last_emit = time.time()
            except GeneratorExit:
                reason = "client_disconnected"
                raise
            finally:
                bench.logger.info(
                    "SSE 结束 job=%s reason=%s last_id=%s", job_id, reason, last
                )

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------- 第 5 步
    @app.get("/api/results")
    def results():
        sort = request.args.get("sort", "latency")
        keyword = request.args.get("q", "")
        only_ok = request.args.get("only_ok", "0") in {"1", "true", "yes"}
        rows = bench.result_rows(sort=sort, keyword=keyword, only_ok=only_ok)
        return jsonify(
            {
                "ok": True,
                "data": {
                    "total": len(rows),
                    "rows": rows,
                    "summary": {
                        "ok": sum(1 for r in rows if r["latency_ms"] is not None),
                        "failed": sum(1 for r in rows if r["latency_ms"] is None),
                        "avg_latency": round(
                            sum(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
                            / max(1, sum(1 for r in rows if r["latency_ms"] is not None)),
                            1,
                        ) if any(r["latency_ms"] is not None for r in rows) else 0,
                    },
                },
            }
        )

    @app.post("/api/export")
    def export():
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or "")
        indices = payload.get("indices")
        if indices is not None and not isinstance(indices, list):
            raise ServiceError("indices 必须是数组")
        return jsonify(
            {
                "ok": True,
                "data": bench.export(
                    kind,
                    sort_field=str(payload.get("sort_field") or "latency"),
                    sort_dir=str(payload.get("sort_dir") or ""),
                    indices=indices,
                ),
            }
        )

    @app.post("/api/publish")
    def publish():
        payload = request.get_json(silent=True) or {}
        source = str(payload.get("source") or "generated")
        if source not in {"generated", "file"}:
            raise ServiceError("发布来源不合法（只能是 generated 或 file）")
        return jsonify(
            {
                "ok": True,
                "data": bench.publish(
                    source=source,
                    filename=str(payload.get("filename") or ""),
                    file_name=str(payload.get("file") or ""),
                    sort_field=str(payload.get("sort_field") or "latency"),
                    sort_dir=str(payload.get("sort_dir") or ""),
                    domain=str(payload.get("domain") or ""),
                    token=str(payload.get("token") or ""),
                ),
            }
        )

    @app.post("/api/export/endpoints")
    def export_endpoints():
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or "")
        return jsonify({"ok": True, "data": bench.export_endpoints(kind)})

    @app.get("/api/download/<path:name>")
    def download(name: str):
        export_dir = config.resolve("export_dir").resolve()
        target = (export_dir / Path(name).name).resolve()
        if target.parent != export_dir or not target.is_file():
            abort(404)
        return send_from_directory(export_dir, target.name, as_attachment=True)

    @app.get("/api/xray")
    def xray_info():
        return jsonify({"ok": True, "data": bench._xray_info()})  # noqa: SLF001

    return app


def _port_in_use(host: str, port: int) -> bool:
    """尝试真实 bind，判断端口是否已被占用（不依赖 socket 复用）。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def run_server(
    config: Config,
    logger: logging.Logger,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    app = create_app(config, logger)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    url = f"http://{display_host}:{port}/"
    if _port_in_use(host, port):
        print("=" * 64)
        print(f" [端口占用] {host}:{port} 已被其他程序使用，Web UI 未启动。")
        print(f" 如果服务其实已在运行，可直接访问：{url}")
        print(f" 或改用其他端口：python main.py --web --port {port + 1}")
        print(" 也可在任务管理器结束占用该端口的 python.exe 后重试。")
        print("=" * 64)
        logger.error("Web 端口被占用：%s:%s", host, port)
        return 1
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        waitress_serve = None

    if waitress_serve is None:
        engine = "Flask 内置开发服务器（未安装 waitress，已回退）"
    else:
        engine = f"waitress {_waitress_version()}（生产级 WSGI，{DEFAULT_THREADS} 线程）"

    print("=" * 64)
    print(" Web UI 已启动")
    print(f" 请在浏览器打开：{url}")
    print(f" 配置文件：{config.path}")
    print(f" 服务引擎：{engine}")
    if waitress_serve is None:
        print(" [提示] 未找到 waitress，本次回退到 Flask 开发服务器；")
        print("        因此可能出现 'This is a development server' 警告（仅本次回退）。")
        print("        安装生产级 WSGI：python -m pip install -r requirements.txt")
    print(" 请勿直接双击 web\\index.html（file:// 无法访问本地接口）。")
    print(" 仅监听本机回环地址；按 Ctrl+C 停止。")
    print("=" * 64)
    logger.info("Web UI 启动：%s（%s）", url, engine)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        if waitress_serve is not None:
            waitress_serve(
                app,
                host=host,
                port=port,
                threads=DEFAULT_THREADS,
                channel_timeout=180,
                ident="vless_tool",
            )
        else:
            app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except OSError as exc:
        logger.exception("Web 服务启动失败")
        print(f"[启动失败] {exc}")
        return 1
    return 0


def _waitress_version() -> str:
    try:
        from importlib.metadata import version

        return version("waitress")
    except Exception:  # noqa: BLE001
        return "unknown"
