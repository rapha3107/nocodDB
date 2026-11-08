"""NEXUS Browser : navigateur externe plein écran, favoris, historique, téléchargements.

Dépendance importante : la navigation à la manette dans le navigateur externe
(Firefox) repose sur Steam Input / le mappage manette→souris (module `hardware`).
Firefox ne gère pas nativement la manette comme périphérique de navigation.
"""

from __future__ import annotations

import mimetypes
import os
import re
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ...core.config import load_json, save_json
from ...core.errors import InvalidParams, NexusError, NotFound, PermissionDenied, Unavailable
from ...core.flatpak import Flatpak
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk
from ...security.verify import safe_join
from . import urls
from .services import CONTROLLER_NOTE, SERVICES

# Navigateurs pris en charge, par ordre de préférence. Arguments placés APRÈS
# l'identifiant d'application (le validateur flatpak interdit les options avant).
BROWSERS: list[dict] = [
    {"app_id": "org.mozilla.firefox", "name": "Firefox", "args": ["--kiosk"],
     "markers": ("org.mozilla.firefox", "firefox")},
    {"app_id": "org.chromium.Chromium", "name": "Chromium", "args": ["--kiosk"],
     "markers": ("org.chromium.chromium", "chromium")},
    {"app_id": "com.google.Chrome", "name": "Google Chrome", "args": ["--kiosk"],
     "markers": ("com.google.chrome", "chrome")},
]

HISTORY_MAX = 100
BOOKMARKS_MAX = 500
TITLE_MAX = 200

DEFAULT_BOOKMARKS: list[tuple[str, str]] = [
    ("ProtonDB", "https://www.protondb.com"),
    ("Steam (boutique)", "https://store.steampowered.com"),
    ("Documentation Bazzite", "https://docs.bazzite.gg"),
    ("Flathub", "https://flathub.org"),
    ("Wikipédia", "https://fr.wikipedia.org"),
    ("YouTube", "https://www.youtube.com"),
]

_PARTIAL_SUFFIXES = (".part", ".crdownload", ".download", ".partial")
_XDG_DL_RE = re.compile(r'^\s*XDG_DOWNLOAD_DIR\s*=\s*"(.*)"\s*$')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kind(name: str, mime: str | None) -> str:
    low = name.lower()
    if low.endswith(_PARTIAL_SUFFIXES):
        return "partiel"
    if not mime:
        if low.endswith((".appimage", ".exe", ".msi", ".sh", ".run", ".deb", ".rpm", ".flatpak", ".flatpakref")):
            return "exécutable/paquet"
        return "autre"
    major = mime.split("/", 1)[0]
    if major in ("image", "video", "audio", "text"):
        return {"image": "image", "video": "vidéo", "audio": "audio", "text": "texte"}[major]
    if mime == "application/pdf":
        return "document"
    if mime in ("application/zip", "application/x-tar", "application/gzip", "application/x-7z-compressed",
                "application/x-rar-compressed", "application/vnd.rar", "application/x-xz",
                "application/x-bzip2", "application/zstd"):
        return "archive"
    if low.endswith((".appimage", ".exe", ".msi", ".sh", ".run", ".deb", ".rpm", ".flatpak", ".flatpakref")):
        return "exécutable/paquet"
    return "autre"


class BrowserModule(NexusModule):
    name = "browser"
    title = "NEXUS Browser"
    description = ("Ouvre un navigateur web plein écran (Firefox Flatpak), gère favoris, historique "
                   "et téléchargements. La manette y pilote un pointeur via Steam Input.")

    def __init__(self, ctx, *, proc_root: Path | str | None = None,
                 kill: Callable[[int, int], None] | None = None,
                 downloads_dir: Path | str | None = None):
        super().__init__(ctx)
        self.proc_root = Path(proc_root) if proc_root else Path("/proc")
        self._kill = kill or os.kill
        self._downloads_override = Path(downloads_dir) if downloads_dir else None
        self._lock = threading.Lock()
        self._pids: dict[int, str] = {}   # pid lancé par NEXUS -> app_id
        base = ctx.paths.data / "browser"
        self.bookmarks_path = base / "bookmarks.json"
        self.history_path = base / "history.json"

    # ------------------------------------------------------------------ helpers
    def _allowed_local(self) -> list[str]:
        v = self.ctx.settings.get("browser.allowed_local_hosts", [])
        return [h for h in v if isinstance(h, str)] if isinstance(v, list) else []

    def _engine(self) -> str:
        return self.ctx.settings.get("browser.search_engine", urls.DEFAULT_SEARCH_ENGINE) \
            or urls.DEFAULT_SEARCH_ENGINE

    def _installed_browsers(self) -> list[dict]:
        fp = Flatpak(self.ctx.runner)
        if not fp.available():
            return []
        try:
            ids = {a.app_id for a in fp.installed()}
        except NexusError:
            return []
        return [b for b in BROWSERS if b["app_id"] in ids]

    def _pick_browser(self) -> dict:
        found = self._installed_browsers()
        if not found:
            raise Unavailable(
                "Aucun navigateur Flatpak détecté (Firefox, Chromium ou Chrome). Installez Firefox "
                "(org.mozilla.firefox) depuis le Store NEXUS.",
                details={"candidates": [b["app_id"] for b in BROWSERS]})
        pref = self.ctx.settings.get("browser.preferred")
        for b in found:
            if b["app_id"] == pref:
                return b
        return found[0]

    def _cmdline(self, pid: int) -> list[str]:
        try:
            raw = (self.proc_root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return []
        return [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]

    def _is_our_browser(self, pid: int, app_id: str) -> bool:
        """Vérifie via /proc/<pid>/cmdline que le PID est bien le navigateur lancé.

        `flatpak run` peut s'être remplacé par `bwrap` (exec) : on accepte un
        processus flatpak/bwrap dont la ligne de commande mentionne le navigateur.
        """
        args = self._cmdline(pid)
        if not args:
            return False
        exe = os.path.basename(args[0])
        markers = next((b["markers"] for b in BROWSERS if b["app_id"] == app_id), (app_id.lower(),))
        joined = " ".join(args).lower()
        if exe == "flatpak":
            return app_id in args
        if exe in ("bwrap", "flatpak-bwrap") or any(m in exe.lower() for m in markers):
            return any(m in joined for m in markers)
        return False

    def _alive_pids(self) -> dict[int, str]:
        with self._lock:
            items = list(self._pids.items())
        alive = {pid: app for pid, app in items if self._is_our_browser(pid, app)}
        with self._lock:
            for pid in list(self._pids):
                if pid not in alive:
                    self._pids.pop(pid, None)
        return alive

    def _load_bookmarks(self) -> list[dict]:
        data = load_json(self.bookmarks_path, None)
        if data is None:
            data = [{"id": uuid.uuid4().hex[:12], "title": t, "url": u, "created": _now_iso(), "default": True}
                    for t, u in DEFAULT_BOOKMARKS]
            save_json(self.bookmarks_path, data)
        return [b for b in data if isinstance(b, dict) and "id" in b and "url" in b] if isinstance(data, list) else []

    def _load_history(self) -> list[dict]:
        data = load_json(self.history_path, [])
        return [h for h in data if isinstance(h, dict) and "url" in h] if isinstance(data, list) else []

    def _record_history(self, url: str, query: str, kind: str) -> None:
        with self._lock:
            hist = [h for h in self._load_history() if h.get("url") != url]
            hist.insert(0, {"url": url, "input": query[:TITLE_MAX], "kind": kind, "opened": _now_iso()})
            save_json(self.history_path, hist[:HISTORY_MAX])

    def downloads_dir(self) -> Path:
        if self._downloads_override:
            return self._downloads_override
        custom = self.ctx.settings.get("browser.downloads_dir")
        if isinstance(custom, str) and custom.startswith("/"):
            return Path(custom)
        home = Path(self.ctx.paths.home)
        try:
            for line in (home / ".config" / "user-dirs.dirs").read_text(encoding="utf-8").splitlines():
                m = _XDG_DL_RE.match(line)
                if m:
                    val = m.group(1).replace("$HOME", str(home))
                    if val.startswith("/") and Path(val) != home:
                        return Path(val)
        except OSError:
            pass
        return home / "Downloads"

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        found = self._installed_browsers()
        alive = self._alive_pids()
        return {
            "ok": bool(found),
            "browser": found[0]["app_id"] if found else None,
            "browsers": [b["app_id"] for b in found],
            "flatpak": Flatpak(self.ctx.runner).available(),
            "open": bool(alive),
            "pids": sorted(alive),
            "controller_note": CONTROLLER_NOTE,
            **({} if found else {"message": "Aucun navigateur Flatpak installé (Firefox recommandé)."}),
        }

    # ------------------------------------------------------------------ actions
    @action("status", Risk.READ, "État du navigateur : détecté, ouvert ou non")
    def status_action(self):
        return self.status()

    @action("resolve", Risk.READ, "Normalise une saisie de barre d'adresse (URL ou recherche) sans l'ouvrir",
            {"url": Param("str", description="URL ou texte à rechercher")})
    def resolve(self, url):
        u, kind = urls.resolve_input(url, engine=self._engine(), allowed_local=self._allowed_local())
        return {"url": u, "kind": kind}

    @action("open", Risk.CONTROL,
            "Ouvre une URL (http/https) ou une recherche dans le navigateur plein écran",
            {"url": Param("str", description="URL (youtube.com, https://…) ou texte à rechercher")},
            summary=lambda p: f"Ouvrir {p.get('url')} dans le navigateur")
    def open(self, url):
        target, kind = urls.resolve_input(url, engine=self._engine(), allowed_local=self._allowed_local())
        browser = self._pick_browser()
        argv = Flatpak(self.ctx.runner).run_argv(browser["app_id"], *browser["args"], target)
        pid = self.ctx.runner.spawn(argv)
        with self._lock:
            self._pids[pid] = browser["app_id"]
        self._record_history(target, url, kind)
        self.ctx.events.publish("browser.opened", {"url": target, "browser": browser["app_id"]})
        return {"opened": True, "url": target, "kind": kind, "browser": browser["app_id"], "pid": pid,
                "controller_note": CONTROLLER_NOTE}

    @action("close", Risk.CONTROL, "Ferme le navigateur lancé par NEXUS (SIGTERM à son seul processus)")
    def close(self):
        with self._lock:
            items = list(self._pids.items())
        closed, stale = [], []
        for pid, app in items:
            if pid <= 1 or not self._is_our_browser(pid, app):
                stale.append(pid)
                continue
            try:
                self._kill(pid, signal.SIGTERM)
                closed.append(pid)
            except ProcessLookupError:
                stale.append(pid)
            except PermissionError:
                stale.append(pid)
        with self._lock:
            for pid in closed + stale:
                self._pids.pop(pid, None)
        if closed:
            self.ctx.events.publish("browser.closed", {"pids": closed})
        return {"closed": bool(closed), "pids": closed,
                **({} if closed else {"message": "Aucun navigateur lancé par NEXUS n'est ouvert."})}

    @action("bookmarks", Risk.READ, "Liste des favoris")
    def bookmarks(self):
        with self._lock:
            return self._load_bookmarks()

    @action("bookmark_add", Risk.SETTINGS, "Ajoute un favori (URL http/https publique)",
            {"title": Param("str", description="Titre"), "url": Param("str", description="Adresse")},
            summary=lambda p: f"Ajouter le favori « {p.get('title')} » ({p.get('url')})")
    def bookmark_add(self, title, url):
        title = " ".join(title.split())[:TITLE_MAX]
        if not title:
            raise InvalidParams("Titre de favori vide")
        target, kind = urls.resolve_input(url, engine=self._engine(), allowed_local=self._allowed_local())
        if kind != "url":
            raise InvalidParams("Adresse de favori invalide : saisissez une URL (ex. youtube.com)")
        with self._lock:
            items = self._load_bookmarks()
            for b in items:
                if b["url"] == target:
                    return {**b, "existing": True}
            if len(items) >= BOOKMARKS_MAX:
                raise InvalidParams(f"Nombre maximal de favoris atteint ({BOOKMARKS_MAX})")
            bm = {"id": uuid.uuid4().hex[:12], "title": title, "url": target, "created": _now_iso()}
            items.append(bm)
            save_json(self.bookmarks_path, items)
        return bm

    @action("bookmark_remove", Risk.SETTINGS, "Supprime un favori",
            {"id": Param("str", description="Identifiant du favori")},
            summary=lambda p: f"Supprimer le favori {p.get('id')}")
    def bookmark_remove(self, id):
        with self._lock:
            items = self._load_bookmarks()
            keep = [b for b in items if b["id"] != id]
            if len(keep) == len(items):
                raise NotFound(f"Favori introuvable: {id}")
            save_json(self.bookmarks_path, keep)
        return {"removed": id}

    @action("services", Risk.READ, "Services de streaming web (URL officielles et compatibilité Linux)")
    def services(self):
        return {"services": [dict(s) for s in SERVICES], "controller_note": CONTROLLER_NOTE}

    @action("history", Risk.READ, "Historique local des pages ouvertes via NEXUS (100 dernières)")
    def history(self):
        with self._lock:
            return self._load_history()

    @action("history_clear", Risk.DELETE_DATA, "Efface l'historique local NEXUS Browser",
            summary=lambda p: "Effacer l'historique de navigation NEXUS")
    def history_clear(self):
        with self._lock:
            n = len(self._load_history())
            save_json(self.history_path, [])
        return {"cleared": n}

    @action("suggest", Risk.READ, "Complétions de barre d'adresse depuis favoris et historique",
            {"text": Param("str", required=False, default="", description="Début de saisie"),
             "limit": Param("int", required=False, default=8, description="Nombre max (1-20)")})
    def suggest(self, text="", limit=8):
        limit = max(1, min(int(limit), 20))
        q = text.strip().lower()
        with self._lock:
            pool = [{"title": b.get("title", ""), "url": b["url"], "source": "bookmark"}
                    for b in self._load_bookmarks()]
            hist = self._load_history()
        # historique : pages d'abord, recherches ensuite
        hist.sort(key=lambda h: h.get("kind") == "search")
        pool += [{"title": h.get("input", ""), "url": h["url"], "source": "history"} for h in hist]

        def bare(u: str) -> str:
            u = u.lower().split("://", 1)[-1]
            return u[4:] if u.startswith("www.") else u

        scored, seen = [], set()
        for i, s in enumerate(pool):
            if s["url"] in seen:
                continue
            t, u = s["title"].lower(), bare(s["url"])
            if not q:
                score = 3
            elif u.startswith(q) or t.startswith(q):
                score = 0
            elif q in u or q in t:
                score = 1
            else:
                continue
            seen.add(s["url"])
            scored.append((score, 0 if s["source"] == "bookmark" else 1, i, s))
        scored.sort(key=lambda x: x[:3])
        return [s for *_, s in scored[:limit]]

    @action("downloads", Risk.READ, "Liste les fichiers du dossier Téléchargements")
    def downloads(self):
        base = self.downloads_dir()
        if not base.is_dir():
            return {"dir": str(base), "files": [], "exists": False}
        files = []
        for entry in base.iterdir():
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat()
            except OSError:
                continue
            is_dir = entry.is_dir()
            mime = None if is_dir else mimetypes.guess_type(entry.name)[0]
            files.append({
                "name": entry.name,
                "size": None if is_dir else st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "mtime": st.st_mtime,
                "type": "dossier" if is_dir else _kind(entry.name, mime),
                "mime": mime,
                "is_dir": is_dir,
                "partial": entry.name.lower().endswith(_PARTIAL_SUFFIXES),
            })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return {"dir": str(base), "files": files, "exists": True}

    @action("download_delete", Risk.DELETE_DATA, "Supprime un fichier du dossier Téléchargements",
            {"name": Param("str", description="Nom du fichier")},
            summary=lambda p: f"Supprimer définitivement le téléchargement « {p.get('name')} »")
    def download_delete(self, name):
        if not name or "/" in name or "\\" in name or "\x00" in name or name in (".", ".."):
            raise PermissionDenied("Nom de fichier invalide (pas de chemin)")
        base = self.downloads_dir()
        if not base.is_dir():
            raise NotFound("Dossier Téléchargements introuvable")
        if (base / name).is_symlink():
            raise PermissionDenied("Les liens symboliques ne sont pas supprimés par NEXUS")
        p = safe_join(base, name)
        if p.parent != base.resolve():
            raise PermissionDenied("Chemin hors de la zone autorisée")
        if not p.exists():
            raise NotFound(f"Fichier introuvable: {name}")
        if not p.is_file():
            raise PermissionDenied("Seuls les fichiers peuvent être supprimés (pas les dossiers)")
        p.unlink()
        return {"deleted": name, "at": time.time()}
