"""Persistent state of NEXUS AI: provider config (hot-reloaded JSON), secrets (0600),
usage/cost ledger, circuit breakers and outbound redaction."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from ...core.config import load_json, save_json

# --------------------------------------------------------------------------- provider config

CATEGORIES = ["diagnostic", "optimize", "compare", "research", "planning", "multi_step", "open_question"]

DEFAULT_CONFIG: dict = {
    "version": 1,
    "providers": {
        "ollama": {"type": "ollama", "enabled": False, "endpoint": "http://127.0.0.1:11434",
                   "models": {"default": "qwen2.5:3b"}, "timeout": 60},
        "anthropic": {"type": "anthropic", "enabled": False, "endpoint": "https://api.anthropic.com",
                      "models": {"strong": "claude-sonnet-5-5", "economy": None}, "timeout": 30},
        "openai_compat": {"type": "openai_compat", "enabled": False, "endpoint": "https://api.openai.com",
                          "models": {"strong": None, "economy": None}, "timeout": 30},
    },
    # Which LLM handles what. Entries are [provider, model tier]; first available wins.
    "routing": {
        "local_llm": [["ollama", "default"]],
        "complex": {
            "diagnostic": [["anthropic", "strong"], ["openai_compat", "strong"]],
            "short": [["anthropic", "economy"], ["openai_compat", "economy"], ["anthropic", "strong"],
                      ["openai_compat", "strong"]],
            "default": [["anthropic", "strong"], ["openai_compat", "strong"]],
        },
    },
    # Per-category consent for remote use (all true by default once ai.remote_enabled is on).
    "remote_categories": {c: True for c in CATEGORIES},
    # Prices per million tokens. null = unknown: NEXUS never guesses a price.
    "pricing": {},
    "currency": "USD",
    "breaker": {"failures": 3, "cooldown_seconds": 120},
}


class ProviderConfig:
    """`ctx.paths.config/ai_providers.json`, re-read whenever the file changes (hot reload)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._data: dict = copy.deepcopy(DEFAULT_CONFIG)
        if not path.exists():
            self._write(self._data)
        self._reload(force=True)

    def _reload(self, force=False) -> None:
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return
        if not force and m == self._mtime:
            return
        raw = load_json(self.path, None)
        if isinstance(raw, dict):
            self._data = _merge(copy.deepcopy(DEFAULT_CONFIG), raw)
        self._mtime = m

    def get(self) -> dict:
        with self._lock:
            self._reload()
            return copy.deepcopy(self._data)

    def update(self, fn) -> dict:
        with self._lock:
            self._reload()
            data = copy.deepcopy(self._data)
            fn(data)
            self._write(data)
            self._data = data
            self._mtime = self.path.stat().st_mtime_ns
            return copy.deepcopy(data)

    def _write(self, data: dict) -> None:
        save_json(self.path, data)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k not in ("pricing",):
            base[k] = _merge(base[k], v)
        else:
            base[k] = v
    return base


# --------------------------------------------------------------------------- secrets

class SecretStore:
    """API keys in `ai_secrets.json`, mode 0600. Values never leave this class except to providers."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass
        d = load_json(self.path, {})
        return d if isinstance(d, dict) else {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(d).encode())
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def get(self, provider: str) -> str | None:
        with self._lock:
            v = self._read().get(provider)
        return v if isinstance(v, str) and v else None

    def has(self, provider: str) -> bool:
        return self.get(provider) is not None

    def set(self, provider: str, key: str | None) -> None:
        with self._lock:
            d = self._read()
            if key:
                d[provider] = key
            else:
                d.pop(provider, None)
            self._write(d)

    def values(self) -> list[str]:
        with self._lock:
            return [v for v in self._read().values() if isinstance(v, str) and v]


# --------------------------------------------------------------------------- usage / costs

COST_NOTE = "Estimation — vérifiez les tarifs réels de votre fournisseur."


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4)


def price_for(cfg: dict, provider: str, model: str | None) -> dict | None:
    p = (cfg.get("pricing") or {}).get(f"{provider}/{model}")
    if not isinstance(p, dict):
        return None
    if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
        return None
    return p


def cost_of(price: dict | None, tin: int, tout: int) -> float | None:
    if price is None:
        return None
    return round(tin * float(price["input_per_mtok"]) / 1e6 + tout * float(price["output_per_mtok"]) / 1e6, 6)


class UsageLedger:
    MAX = 2000

    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        d = load_json(path, {})
        self._calls: list[dict] = d.get("calls", []) if isinstance(d, dict) else []

    def record(self, entry: dict) -> None:
        with self._lock:
            self._calls.append({"ts": self.clock(), **entry})
            del self._calls[:-self.MAX]
            save_json(self.path, {"calls": self._calls})

    def _agg(self, calls: list[dict]) -> dict:
        by: dict[str, dict] = {}
        total, unknown = 0.0, 0
        for c in calls:
            b = by.setdefault(c["provider"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                                              "calls_without_price": 0})
            b["calls"] += 1
            b["input_tokens"] += c.get("input_tokens", 0)
            b["output_tokens"] += c.get("output_tokens", 0)
            if c.get("cost") is None:
                b["calls_without_price"] += 1
                unknown += 1
            else:
                b["cost"] = round(b["cost"] + c["cost"], 6)
                total += c["cost"]
        return {"cost": round(total, 6), "calls": len(calls), "calls_without_price": unknown, "by_provider": by}

    def summary(self) -> dict:
        now = time.localtime(self.clock())
        day, month = time.strftime("%Y-%m-%d", now), time.strftime("%Y-%m", now)
        with self._lock:
            calls = list(self._calls)
        d = [c for c in calls if time.strftime("%Y-%m-%d", time.localtime(c["ts"])) == day]
        m = [c for c in calls if time.strftime("%Y-%m", time.localtime(c["ts"])) == month]
        return {"today": {"date": day, **self._agg(d)}, "month": {"month": month, **self._agg(m)},
                "last_calls": calls[-20:][::-1]}

    def month_total(self) -> float:
        return self.summary()["month"]["cost"]


# --------------------------------------------------------------------------- circuit breaker

class CircuitBreaker:
    """Per-provider breaker: after `failures` consecutive failures the provider is skipped for
    `cooldown` seconds, then one trial call is allowed (half-open)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            s = self._state.get(name)
            if not s or s["open_until"] is None:
                return True
            return self.clock() >= s["open_until"]

    def success(self, name: str) -> None:
        with self._lock:
            self._state.pop(name, None)

    def failure(self, name: str, threshold: int, cooldown: float) -> None:
        with self._lock:
            s = self._state.setdefault(name, {"failures": 0, "open_until": None})
            s["failures"] += 1
            if s["failures"] >= max(1, threshold):
                s["open_until"] = self.clock() + cooldown

    def info(self, name: str) -> dict:
        with self._lock:
            s = dict(self._state.get(name) or {"failures": 0, "open_until": None})
        now = self.clock()
        if s["open_until"] is None:
            state = "closed"
        elif now < s["open_until"]:
            state = "open"
        else:
            state = "half_open"
        return {"state": state, "failures": s["failures"],
                "retry_in_seconds": max(0, round(s["open_until"] - now)) if state == "open" else 0}


# --------------------------------------------------------------------------- redaction

_HOME_RE = re.compile(r"(?:/var)?/home/[^/\s\"']+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{12,}|sk-ant-[A-Za-z0-9_\-]{12,}|(?:gh[pousr]|hf)_[A-Za-z0-9]{16,}|"
                     r"AIza[0-9A-Za-z_\-]{20,}|[A-Fa-f0-9]{40,}|[A-Za-z0-9+_\-]{48,}={0,2})\b")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")


class Redactor:
    """Minimises what is sent to a remote provider: home paths, user names, e-mails, keys, MACs."""

    def __init__(self, home: Path | None, usernames: list[str], secrets: list[str]):
        self.home = str(home) if home else None
        self.usernames = [u for u in usernames if u and len(u) >= 2 and u not in ("root", "user", "home")]
        self.secrets = [s for s in secrets if s]

    def text(self, s: str) -> str:
        if not s:
            return s
        for sec in self.secrets:
            s = s.replace(sec, "<clé>")
        if self.home and self.home not in ("/", ""):
            s = s.replace(self.home, "~")
        s = _HOME_RE.sub("~", s)
        s = _EMAIL_RE.sub("<email>", s)
        s = _KEY_RE.sub("<clé>", s)
        s = _MAC_RE.sub("<mac>", s)
        for u in self.usernames:
            s = re.sub(rf"(?<![\w]){re.escape(u)}(?![\w])", "<utilisateur>", s)
        return s
