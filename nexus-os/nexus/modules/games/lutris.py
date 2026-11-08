"""Lutris library reader: ``pga.db`` (SQLite), opened strictly read-only.

Database locations searched: ``~/.local/share/lutris/pga.db`` (native) and
``~/.var/app/net.lutris.Lutris/data/lutris/pga.db`` (Flatpak).
Columns used from table ``games`` (all optional except id/name): id, name, slug,
runner, platform, directory, installed, lastplayed, playtime, hidden.
Covers: ``coverart/<slug>.jpg`` next to the database, or ``~/.cache/lutris/coverart``.
"""

from __future__ import annotations

import logging
import sqlite3
import urllib.parse
from pathlib import Path

log = logging.getLogger("nexus.games.lutris")

SLUG_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def db_paths(home: Path) -> list[Path]:
    cands = [home / ".local/share/lutris/pga.db", home / ".var/app/net.lutris.Lutris/data/lutris/pga.db"]
    return [c for c in cands if c.is_file()]


def _cover(home: Path, db: Path, slug: str) -> Path | None:
    if not slug or not set(slug) <= SLUG_OK or slug.startswith("."):
        return None
    for d in (db.parent / "coverart", home / ".cache/lutris/coverart", db.parent / "banners",
              home / ".cache/lutris/banners"):
        for ext in (".jpg", ".png"):
            p = d / f"{slug}{ext}"
            if p.is_file():
                return p
    return None


def scan(home: Path) -> dict:
    dbs = db_paths(home)
    games, covers = [], {}
    seen = set()
    for db in dbs:
        uri = "file:" + urllib.parse.quote(str(db)) + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2)
        except sqlite3.Error as e:
            log.warning("pga.db inaccessible %s: %s", db, e)
            continue
        try:
            con.row_factory = sqlite3.Row
            cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
            if not {"id", "name"} <= cols:
                continue
            want = [c for c in ("id", "name", "slug", "runner", "platform", "directory", "installed",
                                "lastplayed", "playtime", "hidden") if c in cols]
            for r in con.execute(f"SELECT {', '.join(want)} FROM games"):
                row = dict(r)
                gid = row.get("id")
                if not isinstance(gid, int) or gid in seen:
                    continue
                if row.get("hidden"):
                    continue
                seen.add(gid)
                lp = row.get("lastplayed")
                g = {
                    "lutris_id": gid,
                    "title": str(row.get("name") or f"Lutris {gid}"),
                    "slug": row.get("slug") or "",
                    "runner": row.get("runner") or None,
                    "platform": row.get("platform") or None,
                    "install_path": row.get("directory") or None,
                    "installed": bool(row.get("installed")),
                    "last_played": int(lp) if isinstance(lp, (int, float)) and lp > 0 else None,
                    "playtime_hours": float(row["playtime"]) if isinstance(row.get("playtime"), (int, float)) else None,
                    "db": str(db),
                }
                games.append(g)
                c = _cover(home, db, g["slug"])
                if c:
                    covers[gid] = c
        except sqlite3.Error as e:
            log.warning("lecture pga.db échouée %s: %s", db, e)
        finally:
            con.close()
    return {"dbs": [str(d) for d in dbs], "games": games, "covers": covers}
