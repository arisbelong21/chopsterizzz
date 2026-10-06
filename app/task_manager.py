"""Qt-safe background task manager with cancellation and duplicate protection."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    id: str
    name: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    message: str = ""
    error: str = ""


class WorkerSignals(QObject):
    started = Signal(str)
    progress = Signal(str, int, str)
    finished = Signal(str)
    failed = Signal(str, str)
    result = Signal(str, object)


class Worker(QRunnable):
    """Runs a pure/background function. The function receives signals and cancel_check."""

    def __init__(self, task_id: str, fn: Callable, *args: Any, **kwargs: Any):
        super().__init__()
        self.task_id = task_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._cancel = Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self) -> None:
        self.signals.started.emit(self.task_id)
        try:
            if self.is_cancelled():
                self.signals.failed.emit(self.task_id, "Dibatalkan")
                return
            result = self.fn(
                *self.args,
                signals=self.signals,
                cancel_check=self.is_cancelled,
                **self.kwargs,
            )
            if self.is_cancelled():
                self.signals.failed.emit(self.task_id, "Dibatalkan")
            else:
                self.signals.result.emit(self.task_id, result)
                self.signals.finished.emit(self.task_id)
        except Exception as exc:
            self.signals.failed.emit(self.task_id, str(exc))


class TaskManager(QObject):
    task_started = Signal(str)
    task_progress = Signal(str, int, str)
    task_finished = Signal(str)
    task_failed = Signal(str, str)
    task_result = Signal(str, object)

    def __init__(self, max_threads: int = 4):
        super().__init__()
        # The manager, pool and signal QObjects outlive every submitted job.
        self.pool = QThreadPool(self)
        self._closing = False
        self.pool.setMaxThreadCount(max(2, int(max_threads)))
        self._tasks: dict[str, Worker] = {}
        self._infos: dict[str, TaskInfo] = {}

    def is_active(self, task_id: str) -> bool:
        info = self._infos.get(task_id)
        return bool(info and info.status in (TaskStatus.PENDING, TaskStatus.RUNNING))

    def submit(self, task_id: str, name: str, fn: Callable, *args: Any, **kw: Any) -> Worker:
        if self._closing:
            raise RuntimeError("Aplikasi sedang ditutup; tugas baru tidak dapat dimulai.")
        existing = self._tasks.get(task_id)
        if existing and self.is_active(task_id):
            return existing
        info = TaskInfo(id=task_id, name=name)
        self._infos[task_id] = info
        w = Worker(task_id, fn, *args, **kw)
        # Parent signals on the GUI thread; QRunnable auto-deletion must not
        # release the QObject while its progress/error signals are in use.
        w.signals.setParent(self)
        w.signals.started.connect(self._on_started, Qt.QueuedConnection)
        w.signals.progress.connect(self._on_progress, Qt.QueuedConnection)
        w.signals.finished.connect(self._on_finished, Qt.QueuedConnection)
        w.signals.failed.connect(self._on_failed, Qt.QueuedConnection)
        w.signals.result.connect(self._on_result, Qt.QueuedConnection)
        self._tasks[task_id] = w
        self.pool.start(w)
        return w

    def cancel(self, task_id: str) -> None:
        w = self._tasks.get(task_id)
        if w:
            w.cancel()

    def cancel_all(self, prefix: str | None = None) -> None:
        for tid, worker in list(self._tasks.items()):
            if prefix is None or tid.startswith(prefix):
                worker.cancel()

    def has_active_tasks(self) -> bool:
        return any(self.is_active(tid) for tid in self._tasks) or self.pool.activeThreadCount() > 0

    def begin_shutdown(self) -> None:
        """Stop accepting work, and request cooperative cancellation."""
        self._closing = True
        self.cancel_all()

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        return self.pool.waitForDone(timeout_ms)

    def shutdown(self) -> None:
        """Final barrier before QApplication, widgets or the database die."""
        self.begin_shutdown()
        self.wait_for_done()
        for tid in list(self._tasks):
            self._release_worker(tid)

    def _release_worker(self, tid: str) -> None:
        worker = self._tasks.pop(tid, None)
        if worker is not None:
            worker.signals.deleteLater()

    @Slot(str, object)
    def _on_result(self, tid: str, result: object) -> None:
        if not self._closing:
            self.task_result.emit(tid, result)

    @Slot(str)
    def _on_started(self, tid: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.RUNNING
        if not self._closing:
            self.task_started.emit(tid)

    @Slot(str, int, str)
    def _on_progress(self, tid: str, pct: int, msg: str) -> None:
        if tid in self._infos:
            self._infos[tid].progress = max(0, min(100, int(pct)))
            self._infos[tid].message = msg
        if not self._closing:
            self.task_progress.emit(tid, pct, msg)

    @Slot(str)
    def _on_finished(self, tid: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.DONE
            self._infos[tid].progress = 100
        # Remove the old job before notifying callbacks which may retry its ID.
        self._release_worker(tid)
        if not self._closing:
            self.task_finished.emit(tid)

    @Slot(str, str)
    def _on_failed(self, tid: str, err: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.CANCELLED if err == "Dibatalkan" else TaskStatus.FAILED
            self._infos[tid].error = err
        self._release_worker(tid)
        if not self._closing:
            self.task_failed.emit(tid, err)
