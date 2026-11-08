"""Internet radio directory client — Radio Browser (https://www.radio-browser.info).

Only the public JSON API is used (read-only, no key). The directory lookups go
through ctx.net (HTTPS allow-list). The audio stream itself is opened by mpv,
and only plain http(s) stream URLs are accepted.
"""

from __future__ import annotations

import re
import urllib.parse

from ...core.errors import InvalidParams, NexusError, NotFound

API_HOSTS = ("https://de1.api.radio-browser.info", "https://all.api.radio-browser.info")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_stream_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\r\n\x00 "):
        return False
    u = urllib.parse.urlparse(url)
    return u.scheme in ("http", "https") and bool(u.hostname)


def validate_uuid(station_id: str) -> str:
    if not isinstance(station_id, str) or not UUID_RE.match(station_id):
        raise InvalidParams(f"Identifiant de station invalide: {station_id!r}")
    return station_id.lower()


def _station(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url_resolved") or raw.get("url")
    sid = raw.get("stationuuid")
    if not is_stream_url(url) or not isinstance(sid, str) or not UUID_RE.match(sid):
        return None
    favicon = raw.get("favicon") or ""
    return {
        "id": sid.lower(),
        "name": str(raw.get("name") or "").strip()[:200] or "Station sans nom",
        "url": url,
        "country": str(raw.get("country") or ""),
        "countrycode": str(raw.get("countrycode") or ""),
        "language": str(raw.get("language") or ""),
        "tags": [t for t in str(raw.get("tags") or "").split(",") if t][:10],
        "codec": str(raw.get("codec") or ""),
        "bitrate": raw.get("bitrate") if isinstance(raw.get("bitrate"), int) else None,
        "homepage": raw.get("homepage") if str(raw.get("homepage") or "").startswith("https://") else "",
        "favicon": favicon if favicon.startswith("https://") else "",
    }


class RadioBrowser:
    def __init__(self, net):
        self.net = net

    def _get(self, path: str):
        last: Exception | None = None
        for host in API_HOSTS:
            try:
                return self.net.get_json(host + path)
            except NexusError as e:
                if e.code != "network_error":
                    raise
                last = e
        raise last  # type: ignore[misc]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = query.strip()
        if not q:
            raise InvalidParams("Recherche de radio vide")
        path = "/json/stations/search?" + urllib.parse.urlencode(
            {"name": q[:100], "limit": int(limit), "hidebroken": "true"})
        data = self._get(path)
        out = [s for s in (_station(r) for r in (data if isinstance(data, list) else [])) if s]
        return out

    def get(self, station_id: str) -> dict:
        sid = validate_uuid(station_id)
        data = self._get(f"/json/stations/byuuid/{sid}")
        stations = [s for s in (_station(r) for r in (data if isinstance(data, list) else [])) if s]
        if not stations:
            raise NotFound(f"Station radio introuvable ou sans flux http(s) valide: {sid}")
        return stations[0]
