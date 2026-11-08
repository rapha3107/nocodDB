"""In-process event bus. Events are pushed to the UI over Server-Sent Events."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any


class EventBus:
    def __init__(self, history: int = 100):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: list[dict] = []
        self._max = history

    def publish(self, topic: str, data: Any = None) -> dict:
        event = {"topic": topic, "data": data, "ts": time.time()}
        with self._lock:
            self._history.append(event)
            del self._history[: -self._max]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow consumer: drop rather than block the system
        return event

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, topic_prefix: str = "") -> list[dict]:
        with self._lock:
            return [e for e in self._history if e["topic"].startswith(topic_prefix)]

    def notify(self, title: str, body: str = "", level: str = "info") -> dict:
        """User-visible notification (shown as a toast in NEXUS Home)."""
        return self.publish("notification", {"title": title, "body": body, "level": level})
