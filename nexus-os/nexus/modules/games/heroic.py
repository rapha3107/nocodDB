"""Heroic Games Launcher library reader (read-only, defensive).

Heroic's on-disk formats changed across versions, so every reader below accepts
several shapes and silently ignores what it does not understand.

Config roots searched (first existing ones, both are read):
  * Flatpak: ``~/.var/app/com.heroicgameslauncher.hgl/config/heroic``
  * native : ``~/.config/heroic``

Supported files (relative to a config root unless stated):
  * Epic / legendary installed games:
      ``legendaryConfig/legendary/installed.json`` and ``~/.config/legendary/installed.json``
      -> ``{appName: {app_name, title, install_path, install_size, is_dlc, ...}}``
  * Epic library (owned, maybe not installed): ``store_cache/legendary_library.json``
      -> ``{"library": [{app_name, title, is_installed, art_square, ...}]}`` (or a bare list)
  * GOG installed: ``gog_store/installed.json`` -> ``{"installed": [{appName, install_path,
      install_size, platform, ...}]}`` (or a bare list)
  * GOG library: ``store_cache/gog_library.json`` (``{"games": [...]}``) and legacy
      ``gog_store/library.json`` (``{"games": [...]}``)
  * Amazon / nile: ``store_cache/nile_library.json`` (``{"library": [...]}``) and
      ``nile_config/nile/installed.json`` (list of ``{id, path, size}``)
  * Sideloaded apps: ``sideload_apps/library.json`` (``{"games": [{app_name, title,
      is_installed, install: {executable, ...}, art_square}]}``)
  * Last played: ``store/timestamp.json`` -> ``{appName: {lastPlayed: ISO|ms, ...}}``

Launch URL (Heroic protocol handler): ``heroic://launch/<runner>/<appName>``.
Heroic >= 2.10 also accepts ``heroic://launch?appName=..&runner=..``; the path form
is the one Heroic's own desktop shortcuts historically used. Validate on a real
machine (see module docs).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("nexus.games.heroic")

RUNNERS = ("legendary", "gog", "nile", "sideload")
APPNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_JSON = 64 * 1024 * 1024


def config_roots(home: Path) -> list[Path]:
    cands = [home / ".var/app/com.heroicgameslauncher.hgl/config/heroic", home / ".config/heroic"]
    return [c for c in cands if c.is_dir()]


def _load(path: Path):
    try:
        if not path.is_file() or path.stat().st_size > MAX_JSON:
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        log.warning("JSON Heroic illisible %s: %s", path, e)
        return None


def _items(doc, *keys) -> list:
    """Accept a bare list, or a dict holding the list under one of `keys`, or a dict of dicts."""
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    if isinstance(doc, dict):
        for k in keys:
            v = doc.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                return [dict(x, app_name=x.get("app_name") or n) for n, x in v.items() if isinstance(x, dict)]
        if doc and all(isinstance(v, dict) for v in doc.values()):
            return [dict(x, app_name=x.get("app_name") or n) for n, x in doc.items()]
    return []


_UNITS = {"b": 1, "kb": 1024, "kib": 1024, "mb": 1024 ** 2, "mib": 1024 ** 2,
          "gb": 1024 ** 3, "gib": 1024 ** 3, "tb": 1024 ** 4, "tib": 1024 ** 4}


def parse_size(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        m = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-z]*)\s*$", v)
        if m:
            num = float(m.group(1).replace(",", "."))
            unit = (m.group(2) or "b").lower()
            if unit in _UNITS:
                return int(num * _UNITS[unit]) or None
    return None


def _ts(v) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e11 else int(v)  # ms or s
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp())
        except ValueError:
            return None
    return None


def _art(x: dict) -> str | None:
    for k in ("art_square", "art_cover", "art_logo"):
        v = x.get(k)
        if isinstance(v, str) and v.startswith("https://"):
            return v
    return None


def scan(home: Path) -> dict:
    """Returns {"roots": [...], "games": [{runner, app_name, title, installed, size, install_path, art, last_played}]}."""
    roots = config_roots(home)
    games: dict[tuple, dict] = {}

    def upsert(runner: str, app_name, **fields):
        if not isinstance(app_name, str) or not APPNAME_RE.fullmatch(app_name):
            return
        g = games.setdefault((runner, app_name), {
            "runner": runner, "app_name": app_name, "title": None, "installed": False,
            "size": None, "install_path": None, "art": None, "last_played": None, "is_dlc": False})
        for k, v in fields.items():
            if v in (None, "", False) and k != "installed":
                continue
            if k == "installed":
                g[k] = g[k] or bool(v)
            elif k == "title":
                g[k] = g[k] or str(v)
            else:
                g[k] = v

    legendary_installed = [r / "legendaryConfig/legendary/installed.json" for r in roots]
    legendary_installed.append(home / ".config/legendary/installed.json")
    timestamps: dict[str, int] = {}

    for root in roots:
        # --- Epic library (owned)
        for x in _items(_load(root / "store_cache/legendary_library.json"), "library", "games"):
            upsert("legendary", x.get("app_name") or x.get("appName"), title=x.get("title"),
                   installed=x.get("is_installed") is True, art=_art(x), is_dlc=bool(x.get("is_dlc")))
        # --- GOG
        for f in (root / "store_cache/gog_library.json", root / "gog_store/library.json"):
            for x in _items(_load(f), "games", "library"):
                upsert("gog", x.get("app_name") or x.get("appName"), title=x.get("title"),
                       installed=x.get("is_installed") is True, art=_art(x))
        for x in _items(_load(root / "gog_store/installed.json"), "installed"):
            upsert("gog", x.get("appName") or x.get("app_name"), installed=True,
                   install_path=x.get("install_path"), size=parse_size(x.get("install_size")))
        # --- Amazon (nile)
        for x in _items(_load(root / "store_cache/nile_library.json"), "library", "games"):
            upsert("nile", x.get("app_name") or x.get("appName"), title=x.get("title"),
                   installed=x.get("is_installed") is True, art=_art(x))
        for x in _items(_load(root / "nile_config/nile/installed.json"), "installed"):
            upsert("nile", x.get("id") or x.get("app_name"), installed=True,
                   install_path=x.get("path"), size=parse_size(x.get("size")))
        # --- sideload
        for x in _items(_load(root / "sideload_apps/library.json"), "games", "library"):
            upsert("sideload", x.get("app_name") or x.get("appName"), title=x.get("title"),
                   installed=x.get("is_installed") is not False, art=_art(x),
                   install_path=(x.get("install") or {}).get("executable") if isinstance(x.get("install"), dict) else None)
        # --- timestamps
        ts = _load(root / "store/timestamp.json")
        if isinstance(ts, dict):
            for name, v in ts.items():
                t = _ts(v.get("lastPlayed")) if isinstance(v, dict) else None
                if t and t > timestamps.get(name, 0):
                    timestamps[name] = t

    for f in legendary_installed:
        for x in _items(_load(f), "installed"):
            upsert("legendary", x.get("app_name") or x.get("appName"), title=x.get("title"), installed=True,
                   install_path=x.get("install_path"), size=parse_size(x.get("install_size")),
                   is_dlc=bool(x.get("is_dlc")))

    out = []
    for g in games.values():
        if g["is_dlc"]:
            continue
        g["title"] = g["title"] or g["app_name"]
        g["last_played"] = timestamps.get(g["app_name"])
        out.append(g)
    return {"roots": [str(r) for r in roots], "games": out}
