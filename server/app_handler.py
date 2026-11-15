# -*- coding: utf-8 -*-
r"""
本地 HTTP 服务：把 Engine 的状态和事件暴露成 JSON / SSE，顺便托管 web\ 下的页面。

只用标准库。前端换了（哪怕换成别的框架）这里不用改，后端换了前端也不用改，
两边只认 /api/* 这几个接口。

这个模块只负责「服务」，不负责「启动」；开窗口是 main.py 的事。
"""
import json
import mimetypes
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(HERE)
    sys.path.insert(0, HERE)
WEB = os.path.join(BASE, "web")

import engine as E  # noqa: E402

ENGINE = E.Engine()


def _guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    server_version = "SanjiaoLocal/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                     # 别把访问日志刷满控制台

    # -- 工具 ------------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json({"ok": False, "err": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", _guess_mime(path))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except Exception:
            return {}

    # -- 路由 ------------------------------------------------------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/api/state":
            self._json({"ok": True, **ENGINE.snapshot()})
            return
        if path == "/api/events":
            self._sse()
            return

        if path == "/" or path == "":
            path = "/index.html"
        # 只允许 web\ 里的文件，挡掉 ..\..\ 之类
        safe = os.path.normpath(os.path.join(WEB, path.lstrip("/\\")))
        if not safe.startswith(WEB + os.sep) or not os.path.isfile(safe):
            self._json({"ok": False, "err": "not found"}, 404)
            return
        self._file(safe)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        payload = self._body()
        try:
            if path == "/api/start":
                args = payload.get("args")
                if args is not None and not isinstance(args, list):
                    args = None
                out = ENGINE.start(args or ["--live"])
            elif path == "/api/stop":
                out = ENGINE.stop()
            elif path == "/api/probe":
                out = ENGINE.probe()
            elif path == "/api/calibrate":
                out = ENGINE.calibrate()
            elif path == "/api/reload":
                out = ENGINE.reload_config()
            elif path == "/api/pin":
                out = ENGINE.set_pin(bool(payload.get("on")))
            elif path == "/api/repaint":
                out = ENGINE.force_repaint()
            else:
                self._json({"ok": False, "err": "unknown api"}, 404)
                return
        except Exception as e:
            self._json({"ok": False, "err": str(e)}, 500)
            return
        self._json(out)

    # -- SSE -------------------------------------------------------------
    def _sse(self):
        q = ENGINE.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last = time.time()
            while True:
                sent = False
                while q:
                    kind, payload = q.popleft()
                    chunk = ("event: %s\ndata: %s\n\n"
                             % (kind, json.dumps(payload, ensure_ascii=False)))
                    self.wfile.write(chunk.encode("utf-8"))
                    sent = True
                if sent:
                    self.wfile.flush()
                    last = time.time()
                elif time.time() - last > 12:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last = time.time()
                time.sleep(0.08)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            ENGINE.unsubscribe(q)


def pick_port(start=8765, tries=40):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("找不到可用端口")
