"""Paths and persistent settings.

All paths follow XDG. Setting NEXUS_ROOT redirects everything under one
directory (used by tests and by the dev server).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    cache: Path
    runtime: Path
    home: Path

    @classmethod
    def detect(cls) -> "Paths":
        root = os.environ.get("NEXUS_ROOT")
        if root:
            r = Path(root)
            return cls(r / "config", r / "data", r / "cache", r / "run", r / "home")
        home = Path.home()
        env = os.environ
        return cls(
            config=Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "nexus",
            data=Path(env.get("XDG_DATA_HOME", home / ".local/share")) / "nexus",
            cache=Path(env.get("XDG_CACHE_HOME", home / ".cache")) / "nexus",
            runtime=Path(env.get("XDG_RUNTIME_DIR", f"/tmp/nexus-{os.getuid()}")) / "nexus",
            home=home,
        )

    def ensure(self) -> None:
        for p in (self.config, self.data, self.cache, self.runtime):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)


DEFAULTS: dict[str, Any] = {
    "language": "fr",
    "profile.active": "default",
    "ai.provider": "local",          # local | ollama | anthropic | openai_compat
    "ai.remote_enabled": False,       # remote LLMs are strictly opt-in
    "ai.voice_enabled": True,
    "security.auto_install_verified": True,   # verified FOSS from Flathub
    "security.auto_install_free_games": True, # free legal games from authorized sources
    "music.volume": 70,
    "ui.theme": "dark",
    "browser.search_engine": "https://duckduckgo.com/?q=",
    "browser.allowed_local_hosts": [],
}


class Settings:
    """Thread-safe JSON settings store with defaults."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (OSError, ValueError):
                # Corrupt settings must never prevent boot: keep a copy, reset.
                path.rename(path.with_suffix(".corrupt"))
                self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def all(self) -> dict[str, Any]:
        with self._lock:
            return {**DEFAULTS, **self._data}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)  # atomic


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
