"""Browser Bridge — HTTP server for browser extension (Manifest V3).

Listens on 127.0.0.1:18421..18425, endpoints:
  GET  /            -> status check
  GET  /status      -> status check
  GET  /add?url=... -> accept URL (redirect-compatible)
  POST /add {"url": "..."} -> accept URL (supports JSON & form-encoded)

On valid URL, forwards to Application.receive_external_url(url) on the Qt main thread.
"""
from __future__ import annotations

import json
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from chopster.downloader.validators import valid_url

# The extension must distinguish this updated desktop build from older
# Chopster instances that may still be listening on an earlier bridge port.
PRODUCT_ID = "chopster-by-aris-v8.5.5"
PRODUCT_NAME = "Chopster"
PRODUCT_VERSION = "8.5.5"

log = logging.getLogger("chopster.bridge")


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler — class attribute `app` must be set to Application instance."""

    app = None  # type: ignore

    def log_message(self, fmt, *args):
        # suppress default stderr logging
        return

    def _cors(self):
        origin = (self.headers.get("Origin") or "").strip()
        # Chrome 142+ Local Network Access rejects "*" for loopback preflights.
        if origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        elif origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        req_headers = (self.headers.get("Access-Control-Request-Headers") or "").strip()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", req_headers or "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Local-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/status", "/"):
            self._json(200, {"ok": True, "product_id": PRODUCT_ID, "app": PRODUCT_NAME, "version": PRODUCT_VERSION})
            return
        if parsed.path == "/add":
            qs = parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0]
            self._accept(unquote(url))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        url = ""
        if raw:
            try:
                payload = json.loads(raw)
                url = payload.get("url") or ""
            except json.JSONDecodeError:
                qs = parse_qs(raw)
                url = (qs.get("url") or [""])[0]
        parsed = urlparse(self.path)
        if parsed.path == "/add":
            if not url:
                url = (parse_qs(parsed.query).get("url") or [""])[0]
            self._accept(unquote(url))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _accept(self, url: str):
        url = (url or "").strip()
        if not valid_url(url) or self.app is None:
            self._json(400, {"ok": False, "error": "URL tidak valid"})
            return
        try:
            # Application.receive_external_url is thread-safe: it only queues the URL.
            # The Qt main thread drains the queue via QTimer in main.py.
            self.app.receive_external_url(url)
            self._json(200, {"ok": True, "queued": True, "product_id": PRODUCT_ID, "app": PRODUCT_NAME, "version": PRODUCT_VERSION})
            return
        except Exception as exc:
            log.warning("bridge _accept failed: %s", exc)
            self._json(500, {"ok": False, "error": "Gagal mengantrekan URL"})

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeServer:
    """Manages the ThreadingHTTPServer lifecycle."""

    def __init__(self, app, base_port: int = 18421, port_range: int = 5):
        self.app = app
        self.base_port = base_port
        self.port_range = port_range
        self.server: ThreadingHTTPServer | None = None
        self.port: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int | None:
        """Try ports base_port .. base_port+range-1. Returns bound port or None."""
        if self.server is not None:
            return self.port
        BridgeHandler.app = self.app
        for port in range(self.base_port, self.base_port + self.port_range):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
                # allow reuse
                server.daemon_threads = True
                self.server = server
                self.port = port
                self._thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"chopster-bridge:{port}")
                self._thread.start()
                log.info("Browser bridge listening on http://127.0.0.1:%d/add", port)
                return port
            except OSError as exc:
                log.debug("bridge port %d busy: %s", port, exc)
                continue
        log.warning("Browser bridge failed to bind any port %d..%d", self.base_port, self.base_port + self.port_range - 1)
        return None

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
            self.port = None
        BridgeHandler.app = None
        log.info("Browser bridge stopped")

    @property
    def is_running(self) -> bool:
        return self.server is not None
