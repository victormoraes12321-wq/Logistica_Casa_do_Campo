from __future__ import annotations

import os
import threading
from http.client import HTTPConnection
from urllib.parse import urlencode

from flask import Flask, Response, request

from app_core.config import load_config


class LegacyProxyRuntime:
    """WSGI-safe bridge to preserve current URLs/templates during Flask migration."""

    def __init__(self) -> None:
        cfg = load_config()
        self._cfg = cfg
        self._bind_host = "127.0.0.1"
        default_port = int(cfg.port) + 1000
        self._bind_port = int(os.environ.get("LOGISTICA_LEGACY_PROXY_PORT") or default_port)
        if self._bind_port == int(cfg.port):
            self._bind_port = int(cfg.port) + 1
        self._server = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def bind_host(self) -> str:
        return self._bind_host

    @property
    def bind_port(self) -> int:
        return self._bind_port

    def ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            # Import lazily to avoid circular imports during module loading.
            from app import create_server

            server = create_server(host=self._bind_host, port=self._bind_port)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            self._started = True

    def proxy(self) -> Response:
        self.ensure_started()
        target = request.path or "/"
        if request.query_string:
            qs = request.query_string.decode("utf-8", errors="ignore")
            target = f"{target}?{qs}"
        elif request.args:
            target = f"{target}?{urlencode(request.args, doseq=True)}"
        body = request.get_data()
        forward_headers = {}
        hop_by_hop = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
        }
        for key, value in request.headers.items():
            if key.lower() in hop_by_hop:
                continue
            forward_headers[key] = value
        original_host = str(request.headers.get("Host") or request.host or "").strip()
        if original_host:
            forward_headers["X-Forwarded-Host"] = original_host
        forward_headers["X-Forwarded-Proto"] = str(request.scheme or "http").strip().lower() or "http"
        try:
            host_port = int((request.host or "").split(":")[-1]) if ":" in (request.host or "") else (443 if request.scheme == "https" else 80)
        except Exception:
            host_port = 443 if request.scheme == "https" else 80
        forward_headers["X-Forwarded-Port"] = str(host_port)
        forward_headers["X-Forwarded-For"] = str(request.remote_addr or "")
        forward_headers["Host"] = f"{self._bind_host}:{self._bind_port}"
        conn = HTTPConnection(self._bind_host, self._bind_port, timeout=max(5, int(self._cfg.request_timeout_seconds)))
        try:
            conn.request(request.method, target, body=body, headers=forward_headers)
            upstream = conn.getresponse()
            payload = upstream.read()
            resp = Response(payload, status=int(upstream.status))
            for key, value in upstream.getheaders():
                lk = key.lower()
                if lk in hop_by_hop:
                    continue
                if lk == "content-length":
                    continue
                resp.headers.add(key, value)
            return resp
        finally:
            conn.close()


def create_app() -> Flask:
    cfg = load_config()
    app = Flask(
        __name__,
        static_folder=str(cfg.static_dir),
        static_url_path="/static",
    )
    app.config["ENV"] = cfg.flask_env
    app.config["DEBUG"] = bool(cfg.debug)
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(cfg.secure_cookie)
    app.config["PROPAGATE_EXCEPTIONS"] = not cfg.is_production

    proxy_runtime = LegacyProxyRuntime()

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def catch_all(path: str) -> Response:
        return proxy_runtime.proxy()

    return app
