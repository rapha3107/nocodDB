"""NEXUS Music — local library, playlists, Internet radio, volume, driven through mpv IPC.

See README.md in this package for the user-facing behaviour (background playback
during games, ducking, Spotify / YouTube Music policy).
"""

from __future__ import annotations

import queue
import random
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path

from ...core.config import load_json, save_json
from ...core.errors import CommandFailed, InvalidParams, NexusError, NotFound, Unavailable
from ...core.flatpak import Flatpak
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk
from .library import Library, normalize
from .mpv import SOCKET_NAME, MpvError, MpvNotRunning, MpvPlayer, MpvWatcher
from .radio import RadioBrowser, is_stream_url, validate_uuid

MAX_QUEUE = 2000
WPCTL_VALUE_RE = re.compile(r"^(?:0\.\d{2}|1\.00)$")
WPCTL_SINK = "@DEFAULT_AUDIO_SINK@"
GAME_START_EVENTS = {"games.launched"}
GAME_END_EVENTS = {"games.exited"}

SPOTIFY_FLATPAK = "com.spotify.Client"
YOUTUBE_MUSIC_URL = "https://music.youtube.com"

_UNAVAILABLE = {
    "missing": "mpv n'est pas installé : installez le paquet mpv (prévu dans l'image NEXUS) "
               "ou le Flatpak io.mpv.Mpv depuis Flathub.",
    "socket_path": "Chemin du socket IPC de mpv trop long (limite 107 octets).",
    "no_socket": "mpv a été lancé mais ne répond pas sur son socket IPC.",
    "no_socket_flatpak": "Le Flatpak io.mpv.Mpv a été lancé mais son socket IPC est inaccessible "
                         "(bac à sable Flatpak). Installez mpv natif pour NEXUS Music.",
}


def _check_level(name: str, value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParams(f"{name} doit être un entier")
    if not lo <= value <= hi:
        raise InvalidParams(f"{name} doit être compris entre {lo} et {hi}")
    return value


def _clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise InvalidParams("Nom invalide")
    n = " ".join(name.split())
    if not n or len(n) > 80 or any(unicodedata.category(c) == "Cc" for c in n):
        raise InvalidParams("Le nom doit contenir entre 1 et 80 caractères imprimables")
    return n


class MusicModule(NexusModule):
    name = "music"
    title = "NEXUS Music"
    description = "Musique locale, playlists, radios Internet et volume (lecture en arrière-plan via mpv)."

    def __init__(self, ctx):
        super().__init__(ctx)
        base_cache = Path(ctx.paths.cache) / "music"
        self.data_dir = Path(ctx.paths.data) / "music"
        self.library = Library(base_cache / "index.json")
        self.playlists_path = self.data_dir / "playlists.json"
        self.favorites_path = self.data_dir / "radio_favorites.json"
        self.radio = RadioBrowser(ctx.net)
        self.player = MpvPlayer(ctx.runner, Path(ctx.paths.runtime) / SOCKET_NAME,
                                flatpak=Flatpak(ctx.runner),
                                initial_volume=lambda: self._saved_volume())
        self._lock = threading.RLock()
        self._source: str | None = None       # "local" | "radio" | None
        self._station: dict | None = None
        self._ducked_from: int | None = None
        self._watcher: MpvWatcher | None = None
        self._events_q = None
        self._stop_evt = threading.Event()
        self._events_thread: threading.Thread | None = None
        self._last_state: tuple | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.ctx.events is not None:
            self._events_q = self.ctx.events.subscribe()
            self._events_thread = threading.Thread(target=self._event_loop, daemon=True,
                                                   name="nexus-music-events")
            self._events_thread.start()
        if not self.library.exists and self.ctx.tasks is not None and self.ctx.settings.get("music.scan_on_start", True):
            self.ctx.tasks.submit("Analyse de la bibliothèque musicale", self._scan_task)
        if self.player.running():          # mpv survived a nexusd restart: reattach
            self._ensure_watcher()

    def stop(self) -> None:
        # mpv is deliberately left running: music is independent of NEXUS Home.
        self._stop_evt.set()
        if self._events_q is not None:
            self.ctx.events.unsubscribe(self._events_q)
        if self._watcher:
            self._watcher.close()

    def status(self) -> dict:
        backend = "running" if self.player.running() else self.player.backend()
        msg = "" if backend else _UNAVAILABLE["missing"]
        return {"ok": bool(backend), "backend": backend, "tracks": len(self.library.tracks),
                "message": msg}

    # ------------------------------------------------------------------ helpers
    def music_dirs(self) -> list[Path]:
        dirs = self.ctx.settings.get("music.dirs")
        if not isinstance(dirs, list) or not dirs or not all(isinstance(d, str) and d for d in dirs):
            return [Path(self.ctx.paths.home) / "Music"]
        return [Path(d).expanduser() for d in dirs]

    def _saved_volume(self) -> int:
        v = self.ctx.settings.get("music.volume", 70)
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100 else 70

    def _public_track(self, t: dict | None) -> dict | None:
        if not t:
            return None
        out = {k: t.get(k) for k in ("id", "title", "artist", "album", "genre", "duration", "track", "format")}
        out["cover_url"] = f"/api/m/music/cover/{t['id']}" if t.get("cover") else None
        return out

    def _ipc(self):
        try:
            ipc = self.player.ensure()
        except LookupError as e:
            reason = str(e.args[0]) if e.args else "missing"
            raise Unavailable(_UNAVAILABLE.get(reason, _UNAVAILABLE["missing"]),
                              details={"binary": "mpv", "reason": reason}) from None
        self._ensure_watcher()
        return ipc

    def _ensure_watcher(self) -> None:
        if self._watcher is None or not self._watcher.is_alive():
            self._watcher = MpvWatcher(self.player.socket_path, lambda _msg: self._publish())
            self._watcher.start()

    def _mpv(self, fn, *args):
        """Run an IPC call, mapping transport errors to NexusError."""
        try:
            return fn(*args)
        except MpvNotRunning as e:
            raise Unavailable("mpv ne répond plus", details={"reason": str(e)}) from None
        except MpvError as e:
            raise CommandFailed(f"mpv a refusé la commande: {e}") from None

    def _publish(self) -> dict | None:
        try:
            st = self.player_status()
        except NexusError:
            return None
        key = (st["state"], st["source"], (st.get("track") or {}).get("id"),
               (st.get("station") or {}).get("id"), st.get("media_title"), st.get("volume"))
        if key != self._last_state:
            self._last_state = key
            self.ctx.events.publish("music.state", st)
        return st

    # ------------------------------------------------------------------ events (ducking)
    def _event_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                ev = self._events_q.get(timeout=0.3)
            except queue.Empty:
                continue
            topic = ev.get("topic") if isinstance(ev, dict) else None
            try:
                if topic in GAME_START_EVENTS:
                    self._duck()
                elif topic in GAME_END_EVENTS:
                    self._unduck()
            except NexusError:
                self.log.debug("ducking failed", exc_info=True)

    def _duck(self) -> None:
        if not self.ctx.settings.get("music.duck_on_game", True) or not self.player.running():
            return
        level = self.ctx.settings.get("music.duck_level", 30)
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            level = 30
        ipc = self.player.ipc
        props = self._mpv(ipc.get_properties, ["pause", "idle-active", "volume"])
        if props.get("idle-active") or props.get("pause"):
            return
        cur = int(round(props.get("volume") or 0))
        if cur <= level:
            return
        with self._lock:
            if self._ducked_from is None:
                self._ducked_from = cur
        self._mpv(ipc.set_property, "volume", level)
        self._publish()

    def _unduck(self) -> None:
        with self._lock:
            prev, self._ducked_from = self._ducked_from, None
        if prev is not None and self.player.running():
            self._mpv(self.player.ipc.set_property, "volume", prev)
            self._publish()

    # ------------------------------------------------------------------ library
    def _scan_task(self, task=None) -> dict:
        res = self.library.scan(self.music_dirs(), progress=(task.update if task else None))
        if task:
            task.update(1.0, f"{res['tracks']} morceaux indexés")
        self.ctx.events.publish("music.library", {"tracks": res["tracks"]})
        return res

    @action("scan", Risk.CONTROL, "Analyse les dossiers de musique (réglage music.dirs, ~/Music par défaut).")
    def scan(self):
        return self.ctx.tasks.submit("Analyse de la bibliothèque musicale", self._scan_task)

    @action("library", Risk.READ, "Liste la bibliothèque locale (filtrage flou optionnel).",
            {"query": Param("str", required=False, description="Texte libre: titre, artiste, album, genre"),
             "limit": Param("int", required=False, default=500, description="Nombre maximal de morceaux"),
             "offset": Param("int", required=False, default=0)})
    def library_list(self, query=None, limit=500, offset=0):
        if limit < 1 or limit > 5000 or offset < 0:
            raise InvalidParams("limit doit être entre 1 et 5000 et offset ≥ 0")
        tracks = self.library.search(query, limit=100_000) if query else self.library.all()
        return {"total": len(tracks), "scanned": self.library.exists,
                "dirs": [str(d) for d in self.music_dirs()],
                "tracks": [self._public_track(t) for t in tracks[offset:offset + limit]]}

    def serve_file(self, subpath: str) -> Path | None:
        kind, _, tid = subpath.partition("/")
        if kind != "cover" or not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
            return None
        t = self.library.get(tid)
        if not t or not t.get("cover"):
            return None
        cover = Path(t["cover"])
        try:
            folder = Path(t["path"]).parent.resolve()
            real = cover.resolve()
        except OSError:
            return None
        # the cover must be a regular file sitting in the track's own folder
        if real.parent != folder or not real.is_file():
            return None
        return real

    # ------------------------------------------------------------------ playback
    def _load_tracks(self, tracks: list[dict], shuffle: bool) -> None:
        tracks = list(tracks)[:MAX_QUEUE]
        if shuffle:
            random.shuffle(tracks)
        ipc = self._ipc()
        cmds = [["loadfile", tracks[0]["path"], "replace"]]
        cmds += [["loadfile", t["path"], "append"] for t in tracks[1:]]
        cmds.append(["set_property", "pause", False])
        res = self._mpv(ipc.commands, cmds)
        if isinstance(res[0], MpvError):
            raise CommandFailed(f"mpv n'a pas pu ouvrir le fichier: {res[0]}")
        with self._lock:
            self._source, self._station = "local", None

    def _playlist_by_name(self, query: str) -> dict | None:
        q = normalize(query)
        q2 = re.sub(r"^(?:ma |la )?playlist ", "", q)
        for p in self._playlists().values():
            if normalize(p["name"]) in (q, q2):
                return p
        return None

    @action("play", Risk.CONTROL,
            "Joue de la musique locale : query (texte libre, recherche floue titre/artiste/album/genre), "
            "track_id ou playlist_id. Sans paramètre : reprend la lecture ou joue toute la bibliothèque en aléatoire.",
            {"query": Param("str", required=False, description="Texte libre, ex. « daft punk »"),
             "track_id": Param("str", required=False),
             "playlist_id": Param("str", required=False),
             "shuffle": Param("bool", required=False, description="Lecture aléatoire")})
    def play(self, query=None, track_id=None, playlist_id=None, shuffle=None):
        given = [x for x in (query, track_id, playlist_id) if x not in (None, "")]
        if len(given) > 1:
            raise InvalidParams("Indiquez un seul critère parmi query, track_id ou playlist_id")
        if not self.player.running() and self.player.backend() is None:
            raise Unavailable(_UNAVAILABLE["missing"], details={"binary": "mpv", "reason": "missing"})
        if not self.library.exists:
            self._scan_task()
        if track_id:
            t = self.library.get(track_id)
            if not t:
                raise NotFound(f"Morceau introuvable: {track_id}")
            tracks, default_shuffle = [t], False
        elif playlist_id:
            p = self._playlists().get(playlist_id)
            if not p:
                raise NotFound(f"Playlist introuvable: {playlist_id}")
            tracks = [self.library.get(i) for i in p["tracks"]]
            tracks = [t for t in tracks if t]
            if not tracks:
                raise NotFound(f"La playlist « {p['name']} » est vide (ou ses morceaux ont disparu)")
            default_shuffle = False
        elif query:
            p = self._playlist_by_name(query)
            tracks = [t for t in (self.library.get(i) for i in p["tracks"]) if t] if p else []
            if not tracks:
                tracks = self.library.search(query)
            if not tracks:
                raise NotFound(
                    f"Aucun morceau ne correspond à « {query} » dans la bibliothèque locale "
                    f"({len(self.library.tracks)} morceaux). Essayez une autre recherche ou une radio "
                    f"(music.radio_search).", details={"query": query, "library_size": len(self.library.tracks)})
            default_shuffle = False
        else:
            if self.player.running():
                props = self._mpv(self.player.ipc.get_properties, ["pause", "idle-active"])
                if props.get("pause") and not props.get("idle-active"):
                    self._mpv(self.player.ipc.set_property, "pause", False)
                    return self._publish() or self.player_status()
            tracks = self.library.all()
            if not tracks:
                dirs = ", ".join(str(d) for d in self.music_dirs())
                raise NotFound(f"Bibliothèque musicale vide : ajoutez des fichiers audio dans {dirs} "
                               f"puis lancez une analyse (music.scan).")
            default_shuffle = True
        self._load_tracks(tracks, default_shuffle if shuffle is None else shuffle)
        return self._publish() or self.player_status()

    @action("pause", Risk.CONTROL, "Met la musique en pause.")
    def pause(self):
        if self.player.running():
            self._mpv(self.player.ipc.set_property, "pause", True)
        return self._publish() or self.player_status()

    @action("resume", Risk.CONTROL, "Reprend la musique (ou lance la bibliothèque en aléatoire si rien n'est chargé).")
    def resume(self):
        if self.player.running():
            props = self._mpv(self.player.ipc.get_properties, ["idle-active"])
            if not props.get("idle-active"):
                self._mpv(self.player.ipc.set_property, "pause", False)
                return self._publish() or self.player_status()
        return self.play()

    @action("stop", Risk.CONTROL, "Arrête la musique et vide la file de lecture.")
    def stop_playback(self):
        if self.player.running():
            self._mpv(self.player.ipc.stop)
        with self._lock:
            self._source, self._station = None, None
        return self._publish() or self.player_status()

    @action("next", Risk.CONTROL, "Passe au morceau suivant.")
    def next_track(self):
        self._require_running()
        try:
            self.player.ipc.playlist_next()
        except MpvError:
            raise NotFound("Pas de morceau suivant dans la file de lecture") from None
        except MpvNotRunning:
            raise Unavailable("mpv ne répond plus") from None
        return self._publish() or self.player_status()

    @action("previous", Risk.CONTROL, "Revient au morceau précédent.")
    def previous_track(self):
        self._require_running()
        try:
            self.player.ipc.playlist_prev()
        except MpvError:
            raise NotFound("Pas de morceau précédent dans la file de lecture") from None
        except MpvNotRunning:
            raise Unavailable("mpv ne répond plus") from None
        return self._publish() or self.player_status()

    def _require_running(self) -> None:
        if not self.player.running():
            raise NotFound("Aucune musique en cours de lecture")

    @action("status", Risk.READ, "État du lecteur : morceau, position, durée, volume, état et source.")
    def player_status(self):
        base = {"state": "stopped", "source": None, "track": None, "station": None, "media_title": None,
                "position": None, "duration": None, "volume": self._saved_volume(),
                "queue_position": None, "queue_length": 0, "ducked": self._ducked_from is not None,
                "running": False}
        if not self.player.running():
            return base
        try:
            p = self.player.ipc.get_properties(
                ["pause", "idle-active", "path", "media-title", "time-pos", "duration", "volume",
                 "playlist-pos", "playlist-count"])
        except MpvNotRunning:
            return base
        base["running"] = True
        if isinstance(p.get("volume"), (int, float)):
            base["volume"] = int(round(p["volume"]))
        path = p.get("path")
        if p.get("idle-active") or not path:
            return base
        with self._lock:
            source, station = self._source, self._station
        base.update({
            "state": "paused" if p.get("pause") else "playing",
            "media_title": p.get("media-title"),
            "position": round(p["time-pos"], 1) if isinstance(p.get("time-pos"), (int, float)) else None,
            "duration": round(p["duration"], 1) if isinstance(p.get("duration"), (int, float)) else None,
            "queue_position": p.get("playlist-pos"), "queue_length": p.get("playlist-count") or 0,
        })
        track = self.library.by_path(path)
        if track:
            base.update(source="local", track=self._public_track(track))
        elif station and station.get("url") == path:
            base.update(source="radio", station=station)
        else:
            base["source"] = source or ("radio" if is_stream_url(path) else "local")
        return base

    # ------------------------------------------------------------------ volume
    @action("volume", Risk.CONTROL, "Règle le volume de la musique (0 à 100).",
            {"level": Param("int", description="Volume de 0 à 100")})
    def volume(self, level):
        level = _check_level("level", level, 0, 100)
        self.ctx.settings.set("music.volume", level)
        with self._lock:
            self._ducked_from = None
        applied = False
        if self.player.running():
            self._mpv(self.player.ipc.set_property, "volume", level)
            applied = True
        self._publish()
        return {"volume": level, "applied": applied}

    @action("volume_step", Risk.CONTROL, "Monte ou baisse le volume de la musique (delta de -100 à 100).",
            {"delta": Param("int", description="Variation, ex. 10 ou -10")})
    def volume_step(self, delta):
        delta = _check_level("delta", delta, -100, 100)
        cur = self._saved_volume()
        if self.player.running():
            v = self._mpv(self.player.ipc.get_property, "volume")
            if isinstance(v, (int, float)):
                cur = int(round(v))
        return self.volume(max(0, min(100, cur + delta)))

    def _wpctl_set(self, level: int) -> dict:
        level = _check_level("level", level, 0, 100)
        value = f"{level / 100:.2f}"
        if not WPCTL_VALUE_RE.fullmatch(value):  # defence in depth: exact "0.50" format only
            raise InvalidParams("Valeur de volume système invalide")
        r = self.ctx.runner.run(["wpctl", "set-volume", WPCTL_SINK, value], timeout=10)
        if not r.ok:
            raise CommandFailed("Impossible de régler le volume système (wpctl)",
                                details={"stderr": r.stderr[-500:]})
        self.ctx.events.publish("music.system_volume", {"level": level})
        return {"system_volume": level}

    def _wpctl_get(self) -> tuple[int, bool]:
        r = self.ctx.runner.run(["wpctl", "get-volume", WPCTL_SINK], timeout=10)
        m = re.search(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)", r.stdout or "")
        if not r.ok or not m:
            raise CommandFailed("Impossible de lire le volume système (wpctl)",
                                details={"stderr": (r.stderr or "")[-500:]})
        return max(0, min(100, int(round(float(m.group(1)) * 100)))), "[MUTED]" in r.stdout

    @action("system_volume", Risk.CONTROL, "Règle le volume général du système (PipeWire, 0 à 100).",
            {"level": Param("int", description="Volume de 0 à 100")})
    def system_volume(self, level):
        return self._wpctl_set(level)

    @action("system_volume_step", Risk.CONTROL,
            "Monte ou baisse le volume général du système (delta de -100 à 100), ex. « monte le son ».",
            {"delta": Param("int", description="Variation, ex. 10 ou -10")})
    def system_volume_step(self, delta):
        delta = _check_level("delta", delta, -100, 100)
        return self._wpctl_set(max(0, min(100, self._wpctl_get()[0] + delta)))

    @action("system_volume_get", Risk.READ, "Lit le volume général du système (0 à 100).")
    def system_volume_get(self):
        level, muted = self._wpctl_get()
        return {"system_volume": level, "muted": muted}

    # ------------------------------------------------------------------ playlists
    def _playlists(self) -> dict:
        data = load_json(self.playlists_path, {})
        return data if isinstance(data, dict) else {}

    def _save_playlists(self, data: dict) -> None:
        save_json(self.playlists_path, data)
        self.ctx.events.publish("music.playlists", {"count": len(data)})

    def _playlist_summary(self, p: dict, full: bool = False) -> dict:
        out = {"id": p["id"], "name": p["name"], "count": len(p["tracks"]), "created": p.get("created")}
        if full:
            out["tracks"] = [self._public_track(self.library.get(i)) or {"id": i, "missing": True}
                             for i in p["tracks"]]
        return out

    @action("playlists", Risk.READ, "Liste les playlists.")
    def playlists(self):
        return [self._playlist_summary(p) for p in sorted(self._playlists().values(),
                                                          key=lambda p: normalize(p["name"]))]

    @action("playlist", Risk.READ, "Détail d'une playlist (morceaux).", {"playlist_id": Param("str")})
    def playlist(self, playlist_id):
        p = self._playlists().get(playlist_id)
        if not p:
            raise NotFound(f"Playlist introuvable: {playlist_id}")
        return self._playlist_summary(p, full=True)

    @action("playlist_create", Risk.CONTROL, "Crée une playlist.", {"name": Param("str")})
    def playlist_create(self, name):
        name = _clean_name(name)
        with self._lock:
            data = self._playlists()
            if any(normalize(p["name"]) == normalize(name) for p in data.values()):
                raise InvalidParams(f"Une playlist « {name} » existe déjà")
            pid = secrets.token_hex(6)
            data[pid] = {"id": pid, "name": name, "tracks": [], "created": time.time()}
            self._save_playlists(data)
        return self._playlist_summary(data[pid])

    @action("playlist_add", Risk.CONTROL, "Ajoute un morceau à une playlist.",
            {"playlist_id": Param("str"), "track_id": Param("str")})
    def playlist_add(self, playlist_id, track_id):
        if not self.library.get(track_id):
            raise NotFound(f"Morceau introuvable: {track_id}")
        with self._lock:
            data = self._playlists()
            p = data.get(playlist_id)
            if not p:
                raise NotFound(f"Playlist introuvable: {playlist_id}")
            if len(p["tracks"]) >= MAX_QUEUE:
                raise InvalidParams(f"Une playlist est limitée à {MAX_QUEUE} morceaux")
            p["tracks"].append(track_id)
            self._save_playlists(data)
        return self._playlist_summary(p)

    @action("playlist_remove_track", Risk.CONTROL, "Retire un morceau d'une playlist.",
            {"playlist_id": Param("str"), "track_id": Param("str")})
    def playlist_remove_track(self, playlist_id, track_id):
        with self._lock:
            data = self._playlists()
            p = data.get(playlist_id)
            if not p:
                raise NotFound(f"Playlist introuvable: {playlist_id}")
            if track_id not in p["tracks"]:
                raise NotFound("Ce morceau n'est pas dans la playlist")
            p["tracks"].remove(track_id)
            self._save_playlists(data)
        return self._playlist_summary(p)

    @action("playlist_delete", Risk.DELETE_DATA, "Supprime une playlist (les fichiers audio ne sont pas touchés).",
            {"playlist_id": Param("str")},
            summary=lambda p: f"Supprimer la playlist {p.get('playlist_id')}")
    def playlist_delete(self, playlist_id):
        with self._lock:
            data = self._playlists()
            p = data.pop(playlist_id, None)
            if not p:
                raise NotFound(f"Playlist introuvable: {playlist_id}")
            self._save_playlists(data)
        return {"deleted": playlist_id, "name": p["name"]}

    # ------------------------------------------------------------------ radio
    def _favorites(self) -> list:
        data = load_json(self.favorites_path, [])
        return [s for s in data if isinstance(s, dict) and "id" in s] if isinstance(data, list) else []

    @action("radio_search", Risk.READ, "Cherche une radio Internet (annuaire Radio Browser).",
            {"query": Param("str", description="Nom de la station, ex. « FIP »")})
    def radio_search(self, query):
        stations = self.radio.search(query)
        favs = {s["id"] for s in self._favorites()}
        for s in stations:
            s["favorite"] = s["id"] in favs
        return stations

    @action("radio_play", Risk.CONTROL, "Joue une radio Internet (identifiant stationuuid de Radio Browser).",
            {"station_id": Param("str")})
    def radio_play(self, station_id):
        sid = validate_uuid(station_id)
        if not self.player.running() and self.player.backend() is None:
            raise Unavailable(_UNAVAILABLE["missing"], details={"binary": "mpv", "reason": "missing"})
        try:
            station = self.radio.get(sid)
        except NexusError as e:
            fav = next((s for s in self._favorites() if s["id"] == sid), None)
            if e.code != "network_error" or not fav:
                raise
            station = fav  # directory unreachable: use the cached favourite
        if not is_stream_url(station.get("url")):
            raise InvalidParams("Seuls les flux http(s) sont acceptés")
        ipc = self._ipc()
        self._mpv(ipc.loadfile, station["url"], "replace")
        self._mpv(ipc.set_property, "pause", False)
        with self._lock:
            self._source, self._station = "radio", station
        return self._publish() or self.player_status()

    @action("radio_favorites", Risk.READ, "Liste les radios favorites.")
    def radio_favorites(self):
        return self._favorites()

    @action("radio_favorite_add", Risk.CONTROL, "Ajoute une radio aux favoris.", {"station_id": Param("str")})
    def radio_favorite_add(self, station_id):
        sid = validate_uuid(station_id)
        favs = self._favorites()
        if any(s["id"] == sid for s in favs):
            return favs
        favs.append(self.radio.get(sid))
        save_json(self.favorites_path, favs)
        return favs

    @action("radio_favorite_remove", Risk.CONTROL, "Retire une radio des favoris.", {"station_id": Param("str")})
    def radio_favorite_remove(self, station_id):
        sid = validate_uuid(station_id)
        favs = self._favorites()
        new = [s for s in favs if s["id"] != sid]
        if len(new) == len(favs):
            raise NotFound("Cette radio n'est pas dans les favoris")
        save_json(self.favorites_path, new)
        return new

    # ------------------------------------------------------------------ external services
    @action("services", Risk.READ, "Services de streaming externes (Spotify, YouTube Music) et comment les utiliser.")
    def services(self):
        spotify_installed = None
        fp = Flatpak(self.ctx.runner)
        if fp.available():
            try:
                spotify_installed = fp.is_installed(SPOTIFY_FLATPAK)
            except NexusError:
                spotify_installed = None
        return [
            {
                "id": "spotify", "name": "Spotify", "kind": "flatpak_app", "app_id": SPOTIFY_FLATPAK,
                "installed": spotify_installed,
                "install": {"action": "store.install", "params": {"app_id": SPOTIFY_FLATPAK}},
                "integration": "Application officielle Spotify (Flatpak, Flathub). NEXUS ne contrôle pas "
                               "Spotify directement ; le contrôle via MPRIS n'est pas intégré.",
                "terms": "Compte Spotify requis ; soumis aux conditions d'utilisation de Spotify. "
                         "NEXUS n'intègre aucun contournement (téléchargement, suppression de publicités).",
            },
            {
                "id": "youtube_music", "name": "YouTube Music", "kind": "website", "url": YOUTUBE_MUSIC_URL,
                "open": {"action": "browser.open", "params": {"url": YOUTUBE_MUSIC_URL}},
                "integration": "Site web officiel ouvert dans NEXUS Browser.",
                "terms": "Soumis aux conditions d'utilisation de YouTube. Pas d'extraction de flux ni de "
                         "lecture en arrière-plan contournant YouTube Premium.",
            },
        ]
