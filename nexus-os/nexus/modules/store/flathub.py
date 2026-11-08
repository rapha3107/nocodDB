"""Client de l'API publique Flathub v2 (via ctx.net) avec cache disque.

Endpoints utilisés (hôte flathub.org, HTTPS, allow-list de nexus/core/net.py) :
  * POST /api/v2/search                    {"query": ...} -> {"hits": [...]}
  * GET  /api/v2/appstream/<app_id>        métadonnées AppStream
  * GET  /api/v2/verification/<app_id>/status -> {"verified": bool, ...}
  * GET  /api/v2/summary/<app_id>          (facultatif) métadonnées flatpak dont permissions

Le format des réponses Flathub peut évoluer : tout est lu de façon défensive.
Chaque réponse réussie est mise en cache (JSON, horodatée). Si Flathub est
injoignable, la dernière donnée connue est renvoyée avec `stale: True`.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from nexus.core.config import load_json, save_json
from nexus.core.errors import NexusError
from nexus.security.verify import FLATPAK_ID_RE

API = "https://flathub.org/api/v2"
APP_PAGE = "https://flathub.org/apps/"

TTL_SEARCH = 3600
TTL_DETAILS = 24 * 3600
TTL_VERIFICATION = 24 * 3600

# Identifiants SPDX de licences libres courantes (préfixes).
_FREE_LICENSE_PREFIXES = (
    "GPL", "LGPL", "AGPL", "MIT", "BSD", "Apache", "MPL", "Zlib", "ISC", "CC0", "CC-BY",
    "Unlicense", "Artistic", "EPL", "EUPL", "BSL-1.0", "OFL", "WTFPL", "0BSD", "PSF", "Python",
    "Ruby", "PHP", "X11", "curl", "libpng", "FTL", "OpenSSL", "MS-PL", "Sleepycat", "NCSA",
)


class NotOnFlathub(NexusError):
    code = "not_found"
    http_status = 404


def _truthy(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no", ""):
            return False
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def is_open_source(license_expr, is_free_license=None) -> bool | None:
    """True = licence libre connue, False = propriétaire, None = inconnu."""
    if isinstance(license_expr, str) and "LicenseRef-proprietary" in license_expr:
        return False
    flag = _truthy(is_free_license)
    if flag is not None:
        return flag
    if not isinstance(license_expr, str) or not license_expr.strip():
        return None
    tokens = [t for t in re.split(r"[\s()]+", license_expr) if t and t.upper() not in ("AND", "OR", "WITH")]
    if tokens and all(t.startswith(_FREE_LICENSE_PREFIXES) for t in tokens):
        return True
    return None


def _str(v, limit=2000) -> str | None:
    return v[:limit] if isinstance(v, str) and v else None


def _https(v) -> str | None:
    return v if isinstance(v, str) and v.startswith("https://") else None


def _http_status(err: Exception) -> int | None:
    status = (getattr(err, "details", None) or {}).get("status")
    if isinstance(status, int):
        return status
    cause = err.__cause__  # older core without details["status"]
    code = getattr(cause, "code", None)
    return code if isinstance(code, int) else None


class Cache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / f"{h}.json"

    def get(self, key: str):
        d = load_json(self._path(key), None)
        if isinstance(d, dict) and d.get("key") == key and "ts" in d:
            return d
        return None

    def put(self, key: str, data) -> None:
        save_json(self._path(key), {"key": key, "ts": time.time(), "data": data})


class FlathubClient:
    def __init__(self, net, cache_dir: Path, clock=time.time):
        self.net = net
        self.cache = Cache(cache_dir)
        self.clock = clock
        self.state_path = cache_dir / "state.json"
        st = load_json(self.state_path, {})
        self.last_ok: float | None = st.get("last_ok")
        self.last_error: float | None = st.get("last_error")

    # ---- connectivity bookkeeping ----------------------------------------
    def _mark(self, ok: bool) -> None:
        now = self.clock()
        if ok:
            self.last_ok = now
        else:
            self.last_error = now
        try:
            save_json(self.state_path, {"last_ok": self.last_ok, "last_error": self.last_error})
        except OSError:
            pass

    def reachable(self) -> bool | None:
        if self.last_ok is None and self.last_error is None:
            return None
        return (self.last_ok or 0) >= (self.last_error or 0)

    # ---- generic cached fetch --------------------------------------------
    def _cached(self, key: str, ttl: float, fetch):
        """Returns (data, stale, cached_at). Raises the network error if no cache."""
        entry = self.cache.get(key)
        if entry and self.clock() - entry["ts"] < ttl:
            return entry["data"], False, entry["ts"]
        try:
            data = fetch()
        except NexusError as e:
            if _http_status(e) == 404:
                self._mark(True)  # Flathub answered: the app simply does not exist
                raise NotOnFlathub("Application introuvable sur Flathub") from e
            if e.code != "network_error":
                raise
            self._mark(False)
            if entry:
                return entry["data"], True, entry["ts"]
            raise
        self._mark(True)
        self.cache.put(key, data)
        return data, False, self.clock()

    # ---- endpoints --------------------------------------------------------
    def search(self, query: str) -> dict:
        def fetch():
            r = self.net.post_json(f"{API}/search", {"query": query})
            hits = r.get("hits") if isinstance(r, dict) else None
            if not isinstance(hits, list):
                hits = []
            return [h for h in (normalize_hit(x) for x in hits) if h]
        data, stale, ts = self._cached(f"search:{query.lower()}", TTL_SEARCH, fetch)
        return {"results": data, "stale": stale, "cached_at": ts, "source": "flathub"}

    def appstream(self, app_id: str) -> tuple[dict, bool]:
        def fetch():
            r = self.net.get_json(f"{API}/appstream/{app_id}")
            if not isinstance(r, dict) or not r:
                raise NotOnFlathub(f"Application introuvable sur Flathub: {app_id}")
            return r
        data, stale, _ = self._cached(f"appstream:{app_id}", TTL_DETAILS, fetch)
        return data, stale

    def verified(self, app_id: str) -> tuple[bool | None, bool]:
        def fetch():
            r = self.net.get_json(f"{API}/verification/{app_id}/status")
            return {"verified": _truthy(r.get("verified")) if isinstance(r, dict) else None,
                    "method": _str(r.get("method"), 64) if isinstance(r, dict) else None}
        try:
            data, stale, _ = self._cached(f"verification:{app_id}", TTL_VERIFICATION, fetch)
        except NotOnFlathub:
            return False, False
        return data.get("verified"), stale

    def permissions(self, app_id: str) -> dict | None:
        """Permissions flatpak (finish-args) exposées par /summary, si disponibles."""
        def fetch():
            r = self.net.get_json(f"{API}/summary/{app_id}")
            meta = r.get("metadata") if isinstance(r, dict) else None
            perms = meta.get("permissions") if isinstance(meta, dict) else None
            if not isinstance(perms, dict):
                perms = r.get("permissions") if isinstance(r, dict) else None
            if not isinstance(perms, dict):
                return None
            return {k: [str(x)[:200] for x in v][:100] for k, v in perms.items()
                    if isinstance(k, str) and isinstance(v, list)}
        try:
            data, _, _ = self._cached(f"summary:{app_id}", TTL_DETAILS, fetch)
            return data
        except NexusError:
            return None


def normalize_hit(h) -> dict | None:
    if not isinstance(h, dict):
        return None
    app_id = h.get("app_id") or h.get("id")
    if not isinstance(app_id, str) or not FLATPAK_ID_RE.match(app_id) or len(app_id) > 255:
        return None
    lic = _str(h.get("project_license"), 500)
    return {
        "app_id": app_id,
        "name": _str(h.get("name"), 200) or app_id,
        "summary": _str(h.get("summary"), 500),
        "developer": _str(h.get("developer_name"), 200),
        "license": lic,
        "open_source": is_open_source(lic, h.get("is_free_license")),
        "verified": _truthy(h.get("verification_verified")),
        "icon": _https(h.get("icon")),
        "source": "flathub",
        "url": APP_PAGE + app_id,
    }


def normalize_appstream(app_id: str, a: dict) -> dict:
    dev = a.get("developer_name")
    if not dev and isinstance(a.get("developer"), dict):
        dev = a["developer"].get("name")
    lic = _str(a.get("project_license"), 500)
    icon = _https(a.get("icon"))
    if not icon and isinstance(a.get("icons"), list):
        icon = next((_https(i.get("url")) for i in a["icons"] if isinstance(i, dict) and _https(i.get("url"))), None)
    urls = a.get("urls") if isinstance(a.get("urls"), dict) else {}
    return {
        "app_id": app_id,
        "type": _str(a.get("type"), 64),
        "name": _str(a.get("name"), 200) or app_id,
        "summary": _str(a.get("summary"), 500),
        "description": _str(a.get("description"), 20000),
        "developer": _str(dev, 200),
        "license": lic,
        "open_source": is_open_source(lic, a.get("is_free_license")),
        "icon": icon,
        "homepage": _https(urls.get("homepage")),
        "source": "flathub",
        "url": APP_PAGE + app_id,
    }
