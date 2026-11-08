"""Append-only JSON-lines audit log of every security decision and command."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

REDACT_KEYS = {"api_key", "token", "password", "secret"}


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in REDACT_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class AuditLog:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self.memory: list[dict] = []  # last entries, for tests and the diagnostics page

    def log(self, event: str, **fields) -> None:
        entry = {"ts": time.time(), "event": event, **_redact(fields)}
        with self._lock:
            self.memory.append(entry)
            del self.memory[:-500]
            if self.path is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                    self.path.replace(self.path.with_suffix(".1"))
                with self.path.open("a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass  # auditing must not crash the system; entry kept in memory
