"""Qt-safe background task manager with cancellation and duplicate protection."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


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
        self._cancel = False
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancel = True

    def is_cancelled(self) -> bool:
        return self._cancel

    @Slot()
    def run(self) -> None:
        self.signals.started.emit(self.task_id)
        try:
            if self._cancel:
                self.signals.failed.emit(self.task_id, "Dibatalkan")
                return
            result = self.fn(
                *self.args,
                signals=self.signals,
                cancel_check=self.is_cancelled,
                **self.kwargs,
            )
            if self._cancel:
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
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(2, int(max_threads)))
        self._tasks: dict[str, Worker] = {}
        self._infos: dict[str, TaskInfo] = {}

    def is_active(self, task_id: str) -> bool:
        info = self._infos.get(task_id)
        return bool(info and info.status in (TaskStatus.PENDING, TaskStatus.RUNNING))

    def submit(self, task_id: str, name: str, fn: Callable, *args: Any, **kw: Any) -> Worker:
        existing = self._tasks.get(task_id)
        if existing and self.is_active(task_id):
            return existing
        info = TaskInfo(id=task_id, name=name)
        self._infos[task_id] = info
        w = Worker(task_id, fn, *args, **kw)
        w.signals.started.connect(self._on_started)
        w.signals.progress.connect(self._on_progress)
        w.signals.finished.connect(self._on_finished)
        w.signals.failed.connect(self._on_failed)
        w.signals.result.connect(self.task_result)
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

    @Slot(str)
    def _on_started(self, tid: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.RUNNING
        self.task_started.emit(tid)

    @Slot(str, int, str)
    def _on_progress(self, tid: str, pct: int, msg: str) -> None:
        if tid in self._infos:
            self._infos[tid].progress = max(0, min(100, int(pct)))
            self._infos[tid].message = msg
        self.task_progress.emit(tid, pct, msg)

    @Slot(str)
    def _on_finished(self, tid: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.DONE
            self._infos[tid].progress = 100
        self.task_finished.emit(tid)
        self._tasks.pop(tid, None)

    @Slot(str, str)
    def _on_failed(self, tid: str, err: str) -> None:
        if tid in self._infos:
            self._infos[tid].status = TaskStatus.CANCELLED if err == "Dibatalkan" else TaskStatus.FAILED
            self._infos[tid].error = err
        self.task_failed.emit(tid, err)
        self._tasks.pop(tid, None)
