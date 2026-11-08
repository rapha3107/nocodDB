"""Outbound HTTP for modules, restricted to an allowlist of hosts (HTTPS only).

Modules never use urllib directly; they use ctx.net so that tests can inject
FakeNet and so the Security agent can audit every external endpoint here.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import CommandFailed, PermissionDenied

ALLOWED_HOSTS = {
    "flathub.org",                 # app metadata (verified flag, descriptions)
    "dl.flathub.org",
    "de1.api.radio-browser.info",  # Internet radio directory
    "all.api.radio-browser.info",
    "www.protondb.com",            # community compatibility reports (read-only)
    "store.steampowered.com",      # public app details (free-to-play listing)
    "api.anthropic.com",           # optional, user-provided API key only
    "api.openai.com",              # optional, user-provided key, openai-compatible
    "huggingface.co",              # Whisper / Piper model downloads (checksummed)
}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}   # e.g. local Ollama
MAX_BYTES = 20 * 1024 * 1024
USER_AGENT = "NEXUS-OS/0.1 (+https://github.com/rapha3107/nocodDB)"


def check_url(url: str, extra_hosts: set[str] | None = None) -> urllib.parse.ParseResult:
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if host in LOCAL_HOSTS and u.scheme == "http":
        return u
    if u.scheme != "https":
        raise PermissionDenied(f"Seul HTTPS est autorisé: {url}")
    if host not in ALLOWED_HOSTS and host not in (extra_hosts or set()):
        raise PermissionDenied(f"Hôte réseau non autorisé: {host}")
    return u


class Net:
    def __init__(self, audit=None, timeout: float = 15):
        self.audit = audit
        self.timeout = timeout

    def request(self, url: str, *, method: str = "GET", data: bytes | None = None,
                headers: dict | None = None, extra_hosts: set[str] | None = None,
                timeout: float | None = None) -> bytes:
        check_url(url, extra_hosts)
        if self.audit:
            self.audit.log("net", method=method, url=url.split("?")[0])
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read(MAX_BYTES + 1)
        except Exception as e:
            raise CommandFailed(f"Requête réseau échouée: {type(e).__name__}",
                                code="network_error", details={"url": url.split("?")[0]}) from e
        if len(body) > MAX_BYTES:
            raise CommandFailed("Réponse trop volumineuse", code="network_error")
        return body

    def get_json(self, url: str, **kw):
        return json.loads(self.request(url, **kw))

    def post_json(self, url: str, payload: dict, headers: dict | None = None, **kw):
        body = json.dumps(payload).encode()
        return json.loads(self.request(url, method="POST", data=body,
                                       headers={"Content-Type": "application/json", **(headers or {})}, **kw))

    def download(self, url: str, dest: Path, **kw) -> Path:
        data = self.request(url, timeout=kw.pop("timeout", 300), **kw)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest


@dataclass
class FakeNet(Net):
    """Test double. routes: {url_prefix: bytes|dict|list|Exception}."""

    routes: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    audit: object = None
    timeout: float = 1

    def request(self, url, *, method="GET", data=None, headers=None, extra_hosts=None, timeout=None):
        check_url(url, extra_hosts)
        self.requests.append({"url": url, "method": method, "data": data, "headers": headers or {}})
        best = max((p for p in self.routes if url.startswith(p)), key=len, default=None)
        if best is None:
            raise CommandFailed("Requête réseau échouée: offline (FakeNet)", code="network_error")
        r = self.routes[best]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, (dict, list)):
            return json.dumps(r).encode()
        return r
