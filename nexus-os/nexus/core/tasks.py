"""Background jobs (installs, scans, updates) with progress pushed as events.

Usage in a module action:
    return self.ctx.tasks.submit("Installation de RPCS3", lambda task: ...)
The callable receives the Task and may call task.progress(0.5, "message").
The action returns {"task_id": ...}; the UI follows `task.update` events.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import NexusError

log = logging.getLogger("nexus.tasks")


@dataclass
class Task:
    id: str
    title: str
    state: str = "queued"     # queued | running | done | failed
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: dict | None = None
    created: float = field(default_factory=time.time)
    _events: Any = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message is not None:
            self.message = message
        if self._events:
            self._events.publish("task.update", self.to_dict())


class TaskManager:
    def __init__(self, events, workers: int = 4):
        self.events = events
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nexus-task")
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def submit(self, title: str, fn: Callable[[Task], Any]) -> dict:
        t = Task(secrets.token_hex(8), title, _events=self.events)
        with self._lock:
            self._tasks[t.id] = t
            if len(self._tasks) > 200:
                for k in sorted(self._tasks, key=lambda k: self._tasks[k].created)[:50]:
                    if self._tasks[k].state in ("done", "failed"):
                        self._tasks.pop(k)
        t.update()

        def run():
            t.state = "running"
            t.update()
            try:
                t.result = fn(t)
                t.state, t.progress = "done", 1.0
            except NexusError as e:
                t.state, t.error = "failed", e.to_dict()
            except Exception as e:
                log.exception("task %s crashed", title)
                t.state, t.error = "failed", {"error": "internal_error", "message": f"{type(e).__name__}: {e}"}
            t.update()
            if self.events:
                level = "info" if t.state == "done" else "error"
                msg = t.message if t.state == "done" else (t.error or {}).get("message", "")
                self.events.notify(t.title + (" — terminé" if t.state == "done" else " — échec"), msg, level)

        self._pool.submit(run)
        return {"task_id": t.id, "title": title}

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def wait(self, task_id: str, timeout: float = 30) -> Task:
        end = time.time() + timeout
        t = self._tasks[task_id]
        while t.state in ("queued", "running") and time.time() < end:
            time.sleep(0.01)
        return t

    def list(self) -> list[dict]:
        return [t.to_dict() for t in sorted(self._tasks.values(), key=lambda t: -t.created)]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
