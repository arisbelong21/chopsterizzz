"""Private localhost runtime for Chopster Auto Clip Studio.

The service binds only to 127.0.0.1, starts once, and is embedded by QWebEngine.
It does not affect Chopster's downloader or Content Clipper AI services.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request
from dataclasses import dataclass


@dataclass
class RuntimeState:
    url: str
    port: int


class AutoClipWebRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None
        self._state: RuntimeState | None = None
        self._error: Exception | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def start(self, timeout: float = 20.0) -> RuntimeState:
        with self._lock:
            if self._state and self._thread and self._thread.is_alive():
                return self._state
            self._error = None
            port = self._free_port()
            self._state = RuntimeState(f"http://127.0.0.1:{port}/", port)

            def serve():
                try:
                    import uvicorn
                    from .engine.main import app
                    # Explicit logging config with a plain logging.Formatter.
                    # uvicorn's default LOGGING_CONFIG resolves formatters by
                    # string ("()": "uvicorn.logging.DefaultFormatter"), which
                    # can fail inside a frozen EXE when that submodule is not
                    # importable ("unable to configure formatter 'default'").
                    # Using an inline config keeps the backend independent of
                    # that lazy import and logs only warnings, so the loading
                    # page and feature behavior are unchanged.
                    log_config = {
                        "version": 1,
                        "disable_existing_loggers": False,
                        "formatters": {
                            "default": {
                                "format": "%(levelname)s: %(message)s",
                            },
                        },
                        "handlers": {
                            "default": {
                                "class": "logging.StreamHandler",
                                "formatter": "default",
                                "stream": "ext://sys.stderr",
                            },
                        },
                        "loggers": {
                            "uvicorn": {
                                "handlers": ["default"],
                                "level": "WARNING",
                                "propagate": False,
                            },
                            "uvicorn.error": {"level": "WARNING"},
                        },
                    }
                    config = uvicorn.Config(
                        app, host="127.0.0.1", port=port,
                        log_level="warning", access_log=False, lifespan="off",
                        log_config=log_config,
                    )
                    self._server = uvicorn.Server(config)
                    self._server.run()
                except Exception as exc:  # reported to the loading page
                    self._error = exc

            self._thread = threading.Thread(
                target=serve, daemon=True, name="auto-clip-studio-server"
            )
            self._thread.start()

        deadline = time.monotonic() + timeout
        health = f"http://127.0.0.1:{port}/api/health"
        while time.monotonic() < deadline:
            if self._error:
                raise RuntimeError(f"Auto Clip Studio backend failed: {self._error}")
            try:
                with urllib.request.urlopen(health, timeout=.7) as response:
                    if response.status == 200:
                        return self._state
            except Exception:
                time.sleep(.12)
        raise TimeoutError("Auto Clip Studio backend did not become ready in time.")

    def stop(self):
        with self._lock:
            if self._server is not None:
                self._server.should_exit = True
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)


runtime = AutoClipWebRuntime()
