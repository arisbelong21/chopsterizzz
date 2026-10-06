"""Application bootstrap — holds config, db, logger, task manager, browser bridge."""
from __future__ import annotations

from queue import Empty, Queue

from chopster.app.configuration import Configuration
from chopster.app.database import Database
from chopster.app.logging_setup import get_logger, setup_logging
from chopster.app.paths import ensure_dirs
from chopster.app.task_manager import TaskManager


class Application:
    # Qt main window is attached later (app.window)
    window = None  # type: ignore

    def __init__(self) -> None:
        ensure_dirs()
        self.logger = setup_logging()
        self.config = Configuration()
        self.db = Database()
        max_threads = max(2, int(self.config.get("parallel", 2)) + 2)
        self.tasks = TaskManager(max_threads=max_threads)
        self.logger.info("Chopster initialized")
        # Browser bridge is deliberately lazy: the UI is created first and the
        # socket thread is started by main.py through QTimer.singleShot(0, ...).
        self._bridge = None
        self._bridge_port: int | None = None
        self._external_url_queue: Queue[str] = Queue()
        self._shutdown_done = False

    # -- external URL (from browser extension / bridge) -----------------

    def receive_external_url(self, url: str) -> None:
        """Thread-safe ingress for browser extension URLs. UI handling happens on Qt main thread."""
        url = (url or "").strip()
        if not url:
            return
        self._external_url_queue.put(url)
        self.logger.info("external url queued: %s", url)

    def drain_external_urls(self, max_items: int = 20) -> None:
        """Drain browser URLs on the Qt main thread and forward them to the visible window."""
        count = 0
        while count < max_items:
            try:
                url = self._external_url_queue.get_nowait()
            except Empty:
                break
            count += 1
            try:
                win = getattr(self, "window", None)
                if win is not None and hasattr(win, "receive_external_url"):
                    win.receive_external_url(url)
                else:
                    self.logger.warning("no window to handle queued external url: %s", url)
            except Exception as exc:
                self.logger.warning("failed to deliver external url %s: %s", url, exc)

    def ensure_bridge(self) -> int | None:
        """Ensure bridge is running (idempotent). Returns port or None."""
        if self._bridge is None:
            if not bool(self.config.get("browser_bridge", True)):
                return None
            try:
                from chopster.app.bridge import BridgeServer
                self._bridge = BridgeServer(self, base_port=int(self.config.get("browser_bridge_port") or 18421))
            except Exception as exc:
                self.logger.warning("ensure_bridge failed: %s", exc)
                return None
        if not self._bridge.is_running:
            self._bridge_port = self._bridge.start()
        return self._bridge_port

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        # Workers can still emit progress and use config/database during cancel.
        # Never tear down these dependencies before the pool has drained.
        self.tasks.shutdown()
        self._shutdown_done = True
        try:
            if self._bridge is not None:
                self._bridge.stop()
        except Exception:
            pass
        try:
            self.config.save()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
