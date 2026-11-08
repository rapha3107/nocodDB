"""Local music library: directory scan, JSON index, accent-insensitive fuzzy search."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from ...core.config import load_json, save_json
from .tags import read_tags

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav"}
COVER_NAMES = ("cover.jpg", "folder.jpg", "cover.jpeg", "folder.jpeg", "cover.png", "folder.png",
               "Cover.jpg", "Folder.jpg", "front.jpg", "Front.jpg")
MAX_TRACKS = 100_000
INDEX_VERSION = 1


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Éléphant Café!' -> 'elephant cafe'."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("œ", "oe").replace("æ", "ae").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def track_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _trackno(v) -> int:
    m = re.match(r"\s*(\d{1,4})", str(v or ""))
    return int(m.group(1)) if m else 0


def _order(t: dict) -> tuple:
    return (normalize(t.get("artist")), normalize(t.get("album")), t.get("track") or 0, t["path"])


def find_cover(folder: Path) -> Path | None:
    for name in COVER_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


class Library:
    def __init__(self, index_path: Path):
        self.index_path = index_path
        self._lock = threading.Lock()
        self.tracks: dict[str, dict] = {}
        self.dirs: list[str] = []
        self.scanned_at: float | None = None
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        data = load_json(self.index_path, None)
        if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
            tracks = data.get("tracks")
            if isinstance(tracks, dict):
                self.tracks = {k: v for k, v in tracks.items() if isinstance(v, dict) and "path" in v}
            self.dirs = list(data.get("dirs") or [])
            self.scanned_at = data.get("scanned_at")

    @property
    def exists(self) -> bool:
        return self.scanned_at is not None

    def _save(self) -> None:
        save_json(self.index_path, {"version": INDEX_VERSION, "scanned_at": self.scanned_at,
                                    "dirs": self.dirs, "tracks": self.tracks})

    # -- scan ----------------------------------------------------------------
    def scan(self, dirs: list[Path], progress=None) -> dict:
        """Rescan `dirs`; unchanged files (same size and mtime) keep their cached tags."""
        old = {t["path"]: t for t in self.tracks.values()}
        found: dict[str, dict] = {}
        files: list[Path] = []
        missing = []
        for d in dirs:
            d = Path(d).expanduser()
            if not d.is_dir():
                missing.append(str(d))
                continue
            for root, subdirs, names in os.walk(d, followlinks=False):
                subdirs[:] = sorted(s for s in subdirs if not s.startswith("."))
                for n in sorted(names):
                    if not n.startswith(".") and Path(n).suffix.lower() in AUDIO_EXTS:
                        files.append(Path(root) / n)
                if len(files) >= MAX_TRACKS:
                    break
        files = files[:MAX_TRACKS]
        covers: dict[Path, Path | None] = {}
        for i, f in enumerate(files):
            try:
                st = f.stat()
            except OSError:
                continue
            prev = old.get(str(f))
            if prev and prev.get("size") == st.st_size and prev.get("mtime") == int(st.st_mtime):
                entry = dict(prev)
            else:
                tags = read_tags(f)
                entry = {
                    "id": track_id(f), "path": str(f),
                    "title": tags.get("title", f.stem), "artist": tags.get("artist", ""),
                    "album": tags.get("album", ""), "genre": tags.get("genre", ""),
                    "duration": tags.get("duration"), "track": _trackno(tags.get("track")),
                    "format": f.suffix.lower().lstrip("."),
                    "size": st.st_size, "mtime": int(st.st_mtime),
                }
            if f.parent not in covers:
                covers[f.parent] = find_cover(f.parent)
            cover = covers[f.parent]
            entry["cover"] = str(cover) if cover else None
            found[entry["id"]] = entry
            if progress and i % 50 == 0:
                progress(i / max(1, len(files)), f"{i}/{len(files)} fichiers analysés")
        with self._lock:
            self.tracks = found
            self.dirs = [str(Path(d).expanduser()) for d in dirs]
            self.scanned_at = time.time()
            self._save()
        return {"tracks": len(found), "dirs": self.dirs, "missing_dirs": missing}

    # -- queries ---------------------------------------------------------------
    def get(self, tid: str) -> dict | None:
        return self.tracks.get(tid)

    def by_path(self, path: str) -> dict | None:
        for t in self.tracks.values():
            if t["path"] == path:
                return t
        return None

    def all(self) -> list[dict]:
        return sorted(self.tracks.values(), key=_order)

    def search(self, query: str, limit: int = 200) -> list[dict]:
        """Fuzzy search over title/artist/album/genre. Every query word must match a field
        (substring, prefix, or close spelling); results sorted by score."""
        words = [w for w in normalize(query).split() if w]
        if not words:
            return []
        full = " ".join(words)
        scored = []
        for t in self.tracks.values():
            fields = {k: normalize(t.get(k, "")) for k in ("title", "artist", "album", "genre")}
            vocab = set(" ".join(fields.values()).split())
            score = 0.0
            ok = True
            for w in words:
                best = 0.0
                for name, val in fields.items():
                    weight = 1.5 if name in ("title", "artist") else 1.0
                    if not val:
                        continue
                    if re.search(rf"\b{re.escape(w)}\b", val):
                        best = max(best, 2.0 * weight)
                    elif w in val:
                        best = max(best, 1.0 * weight)
                if best == 0.0 and len(w) >= 4:
                    close = difflib.get_close_matches(w, vocab, n=1, cutoff=0.8)
                    if close:
                        best = 0.8
                if best == 0.0:
                    ok = False
                    break
                score += best
            if not ok:
                continue
            for name in ("title", "artist", "album"):
                if fields[name] == full:
                    score += 5
                elif full in fields[name]:
                    score += 2
            scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], _order(x[1])))
        return [t for _, t in scored[:limit]]
