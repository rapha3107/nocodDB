"""NEXUS Games — unified PC game library (Steam, Heroic, Lutris).

Everything is read from the launchers' own local files (read-only). Games are
started through the launchers' official URL handlers with ``xdg-open`` (never by
executing game binaries directly), so DRM, Proton and Steam Input keep working.

Stable game ids:
  * ``steam:<appid>``                          e.g. ``steam:570``
  * ``heroic:<runner>:<appName>``              runner in legendary|gog|nile|sideload
  * ``lutris:<id>``                            id = row id in Lutris' pga.db

Paths are derived from ``ctx.paths.home`` (redirected to tmp in tests); the
attributes ``home``, ``drm_root``, ``disk_usage`` can be overridden for tests.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ...core.config import load_json, save_json
from ...core.errors import CommandFailed, InvalidParams, NexusError, NotFound, Unavailable
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk
from . import heroic, lutris, steam

STEAM_ID_RE = re.compile(r"^(?:steam:)?([0-9]{1,10})$")
HEROIC_ID_RE = re.compile(r"^heroic:(legendary|gog|nile|sideload):([A-Za-z0-9._-]{1,128})$")
LUTRIS_ID_RE = re.compile(r"^lutris:([0-9]{1,10})$")

CACHE_TTL = 600            # library cache: rescan after 10 min
PROTONDB_TTL = 24 * 3600   # ProtonDB summary cache
PROTONDB_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"

# ProtonDB community tiers -> NEXUS honest levels (never a guarantee).
TIER_LEVELS = {
    "platinum": "stable",
    "gold": "bon",
    "silver": "expérimental",
    "bronze": "limité",
    "borked": "non supporté",
}
TIER_FR = {
    "platinum": "Platine : fonctionne parfaitement d'après les rapports",
    "gold": "Or : fonctionne parfaitement après quelques réglages",
    "silver": "Argent : fonctionne avec des problèmes mineurs",
    "bronze": "Bronze : fonctionne mais plante souvent ou a des problèmes gênants",
    "borked": "Cassé : ne démarre pas ou est injouable d'après les rapports",
    "native": "Version Linux native",
}

# Steam EAppState bits used to explain the install state.
STATE_BITS = {
    2: "mise à jour requise",
    32: "fichiers manquants",
    128: "fichiers corrompus",
    256: "mise à jour en cours",
    512: "mise à jour en pause",
    1024: "mise à jour démarrée",
    1048576: "téléchargement en cours",
}

GPU_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
LOW_DISK = 5 * 1024 ** 3
CRITICAL_DISK = 1 * 1024 ** 3
LOG_ERR_RE = re.compile(r"(^|[:\s])err:|Unhandled exception|wine: |fatal|failed to|cannot open|"
                        r"Assertion|vkCreateInstance failed|VK_ERROR", re.I)

HEROIC_FLATPAK = "com.heroicgameslauncher.hgl"


def _iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def parse_id(gid) -> tuple[str, tuple]:
    """Validate a game id. Returns (source, key). Raises InvalidParams."""
    if not isinstance(gid, str) or len(gid) > 160:
        raise InvalidParams("Identifiant de jeu invalide")
    m = STEAM_ID_RE.fullmatch(gid)
    if m:
        return "steam", (m.group(1),)
    m = HEROIC_ID_RE.fullmatch(gid)
    if m:
        return "heroic", (m.group(1), m.group(2))
    m = LUTRIS_ID_RE.fullmatch(gid)
    if m:
        return "lutris", (int(m.group(1)),)
    raise InvalidParams(f"Identifiant de jeu invalide: {gid!r} (attendu steam:<appid>, "
                        "heroic:<runner>:<appName> ou lutris:<id>)")


def canonical_id(gid: str) -> str:
    src, key = parse_id(gid)
    if src == "steam":
        return f"steam:{key[0]}"
    if src == "heroic":
        return f"heroic:{key[0]}:{key[1]}"
    return f"lutris:{key[0]}"


ID_PARAM = Param("str", description="Identifiant du jeu (steam:<appid>, heroic:<runner>:<appName>, lutris:<id>)",
                 validator=parse_id)


class GamesModule(NexusModule):
    name = "games"
    title = "Jeux"
    description = "Bibliothèque unifiée Steam, Heroic (Epic, GOG, Amazon) et Lutris"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.home: Path = Path(ctx.paths.home)
        self.drm_root: Path = Path("/sys/class/drm")
        self.disk_usage = shutil.disk_usage
        self.cache_dir: Path = Path(ctx.paths.cache) / "games"
        self.data_dir: Path = Path(ctx.paths.data) / "games"
        self._lock = threading.RLock()
        self._lib: dict | None = None   # {"games": [...], "covers": {...}, "meta": {...}, "updated": ts}

    # ------------------------------------------------------------------ scanning
    @property
    def _cache_file(self) -> Path:
        return self.cache_dir / "library.json"

    def _scan(self) -> dict:
        games: list[dict] = []
        covers: dict[str, str] = {}
        launches = load_json(self.data_dir / "launches.json", {})
        if not isinstance(launches, dict):
            launches = {}

        st = steam.scan(self.home)
        for g in st["games"]:
            gid = f"steam:{g['appid']}"
            if g["appid"] in st["covers"]:
                covers[gid] = str(st["covers"][g["appid"]])
            games.append({
                "id": gid, "title": g["name"], "source": "steam", "runner": "steam", "appid": g["appid"],
                "installed": g["installed"], "size": g["size"],
                "state_flags": g["state_flags"], "install_path": g["install_path"],
                "library": g["library"], "root": g["root"], "manifest": g["manifest"],
                "compat_tool": g["compat_tool"], "buildid": g["buildid"],
                "last_played_ts": g["last_played"], "last_updated": _iso(g["last_updated"]),
                "can_install": not g["installed"], "can_uninstall": g["installed"],
            })

        he = heroic.scan(self.home)
        for g in he["games"]:
            gid = f"heroic:{g['runner']}:{g['app_name']}"
            games.append({
                "id": gid, "title": g["title"], "source": "heroic", "runner": g["runner"],
                "app_name": g["app_name"], "installed": g["installed"], "size": g["size"],
                "install_path": g["install_path"], "cover_remote": g["art"],
                "last_played_ts": g["last_played"],
                "can_install": False, "can_uninstall": False,  # done inside Heroic
            })

        lu = lutris.scan(self.home)
        for g in lu["games"]:
            gid = f"lutris:{g['lutris_id']}"
            if g["lutris_id"] in lu["covers"]:
                covers[gid] = str(lu["covers"][g["lutris_id"]])
            games.append({
                "id": gid, "title": g["title"], "source": "lutris", "runner": g["runner"],
                "slug": g["slug"], "platform": g["platform"], "installed": g["installed"], "size": None,
                "install_path": g["install_path"], "playtime_hours": g["playtime_hours"],
                "last_played_ts": g["last_played"],
                "can_install": False, "can_uninstall": False,  # done inside Lutris
            })

        for g in games:
            ts = max(int(g.get("last_played_ts") or 0), int(launches.get(g["id"]) or 0))
            g["last_played"] = _iso(ts)
            g["last_played_ts"] = ts or None
            g["cover"] = f"/api/m/games/cover/{g['id'].replace(':', '/', 1)}" if g["id"] in covers else None
            g.setdefault("cover_remote", None)

        games.sort(key=lambda g: (not g["installed"], -(g["last_played_ts"] or 0), g["title"].lower()))
        lib = {
            "games": games, "covers": covers, "updated": time.time(),
            "meta": {"steam_roots": st["roots"], "steam_libraries": st["libraries"],
                     "heroic_roots": he["roots"], "lutris_dbs": lu["dbs"]},
        }
        try:
            save_json(self._cache_file, lib)
        except OSError as e:
            self.log.warning("cache bibliothèque non écrit: %s", e)
        return lib

    def _library(self, force: bool = False) -> dict:
        with self._lock:
            if force:
                self._lib = self._scan()
                return self._lib
            if self._lib is None:
                cached = load_json(self._cache_file, None)
                if isinstance(cached, dict) and isinstance(cached.get("games"), list) \
                        and time.time() - float(cached.get("updated") or 0) < CACHE_TTL:
                    self._lib = cached
            if self._lib is None or time.time() - float(self._lib.get("updated") or 0) >= CACHE_TTL:
                self._lib = self._scan()
            return self._lib

    def _find(self, gid: str) -> dict | None:
        cid = canonical_id(gid)
        return next((g for g in self._library()["games"] if g["id"] == cid), None)

    def _get(self, gid: str) -> dict:
        g = self._find(gid)
        if not g:
            raise NotFound(f"Jeu introuvable dans la bibliothèque: {gid}")
        return g

    @staticmethod
    def _public(g: dict) -> dict:
        hidden = {"manifest", "root", "state_flags", "last_played_ts", "buildid", "library"}
        return {k: v for k, v in g.items() if k not in hidden}

    # --------------------------------------------------------------- detection
    def _launchers(self) -> dict:
        lib = self._library()
        meta = lib.get("meta", {})
        return {
            "steam": {"detected": bool(meta.get("steam_roots")) or self.ctx.runner.available("steam"),
                      "roots": meta.get("steam_roots", []), "flatpak": any(".var/app/" in r for r in meta.get("steam_roots", []))},
            "heroic": {"detected": bool(meta.get("heroic_roots")), "roots": meta.get("heroic_roots", []),
                       "flatpak": any(HEROIC_FLATPAK in r for r in meta.get("heroic_roots", []))},
            "lutris": {"detected": bool(meta.get("lutris_dbs")) or self.ctx.runner.available("lutris"),
                       "databases": meta.get("lutris_dbs", [])},
        }

    def _require_launcher(self, source: str) -> None:
        if not self._launchers()[source]["detected"]:
            names = {"steam": "Steam", "heroic": "Heroic Games Launcher", "lutris": "Lutris"}
            raise Unavailable(f"{names[source]} n'est pas détecté sur ce système", details={"launcher": source})
        if not self.ctx.runner.available("xdg-open"):
            raise Unavailable("xdg-open est absent : impossible d'ouvrir le lanceur", details={"binary": "xdg-open"})

    def _open_url(self, url: str) -> None:
        self.ctx.runner.spawn(["xdg-open", url])

    def status(self) -> dict:
        try:
            lib = self._library()
            launchers = self._launchers()
        except NexusError as e:
            return {"ok": False, "error": e.message}
        counts = {"steam": 0, "heroic": 0, "lutris": 0}
        installed = dict(counts)
        for g in lib["games"]:
            counts[g["source"]] += 1
            installed[g["source"]] += int(bool(g["installed"]))
        detected = [k for k, v in launchers.items() if v["detected"]]
        out = {"ok": bool(detected) and self.ctx.runner.available("xdg-open"),
               "launchers": launchers, "counts": counts, "installed": installed,
               "total": len(lib["games"]), "updated": _iso(lib.get("updated")),
               "xdg_open": self.ctx.runner.available("xdg-open")}
        if not detected:
            out["message"] = "Aucun lanceur de jeux détecté (Steam, Heroic, Lutris)"
        elif not out["xdg_open"]:
            out["message"] = "xdg-open absent : le lancement des jeux est impossible"
        return out

    # ------------------------------------------------------------------ files
    def serve_file(self, subpath: str) -> Path | None:
        parts = subpath.split("/")
        if len(parts) != 3 or parts[0] != "cover" or parts[1] not in ("steam", "lutris"):
            return None
        try:
            gid = canonical_id(f"{parts[1]}:{parts[2]}")
        except InvalidParams:
            return None
        p = self._library()["covers"].get(gid)
        if not p:
            return None
        p = Path(p)
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png") or not p.is_file():
            return None
        return p

    # ---------------------------------------------------------------- actions
    @action("library", Risk.READ, "Liste les jeux PC (Steam, Heroic, Lutris) avec leur état d'installation",
            {"source": Param("str", required=False, choices=["steam", "heroic", "lutris"],
                             description="Filtrer par lanceur"),
             "installed_only": Param("bool", required=False, default=False,
                                     description="Seulement les jeux installés"),
             "query": Param("str", required=False, description="Filtre sur le titre")})
    def library(self, source=None, installed_only=False, query=None):
        lib = self._library()
        games = lib["games"]
        if source:
            games = [g for g in games if g["source"] == source]
        if installed_only:
            games = [g for g in games if g["installed"]]
        if query:
            q = query.lower()
            games = [g for g in games if q in g["title"].lower()]
        return {"games": [self._public(g) for g in games], "count": len(games), "updated": _iso(lib.get("updated"))}

    @action("refresh", Risk.READ, "Relit les bibliothèques Steam, Heroic et Lutris sur le disque")
    def refresh(self):
        lib = self._library(force=True)
        counts = {"steam": 0, "heroic": 0, "lutris": 0}
        for g in lib["games"]:
            counts[g["source"]] += 1
        self.ctx.events.publish("games.library_updated", {"counts": counts})
        return {"count": len(lib["games"]), "counts": counts, "updated": _iso(lib["updated"])}

    @action("launch", Risk.CONTROL, "Lance un jeu via son lanceur officiel (Steam, Heroic ou Lutris)",
            {"id": Param("str", description="Identifiant du jeu, ex. steam:570", validator=parse_id)},
            summary=lambda p: f"Lancer le jeu {p.get('id')}")
    def launch(self, id):
        src, key = parse_id(id)
        g = self._get(id)
        self._require_launcher(src)
        if src == "steam":
            url = f"steam://rungameid/{key[0]}"
        elif src == "heroic":
            url = f"heroic://launch/{key[0]}/{key[1]}"
        else:
            url = f"lutris:rungameid/{key[0]}"
        if src != "steam" and not g["installed"]:
            raise InvalidParams(f"{g['title']} n'est pas installé : installez-le depuis "
                                f"{'Heroic' if src == 'heroic' else 'Lutris'}")
        self._open_url(url)
        with self._lock:
            launches = load_json(self.data_dir / "launches.json", {})
            if not isinstance(launches, dict):
                launches = {}
            now = int(time.time())
            launches[g["id"]] = now
            save_json(self.data_dir / "launches.json", launches)
            g["last_played_ts"], g["last_played"] = now, _iso(now)
        self.ctx.events.publish("games.launched", {"id": g["id"], "title": g["title"], "source": src})
        msg = f"Lancement de {g['title']}"
        if src == "steam" and not g["installed"]:
            msg += " — le jeu n'est pas entièrement installé, Steam va proposer l'installation ou la mise à jour"
        return {"launched": True, "id": g["id"], "title": g["title"], "url": url, "message": msg}

    @action("install", Risk.INSTALL_OWNED,
            "Installe un jeu possédé via son client officiel (Steam ouvre sa fenêtre d'installation)",
            {"id": Param("str", description="Identifiant du jeu, ex. steam:570", validator=parse_id)},
            summary=lambda p: f"Installer le jeu {p.get('id')} via son client officiel")
    def install(self, id):
        src, key = parse_id(id)
        if src == "steam":
            self._require_launcher("steam")
            g = self._find(id)
            if g and g["installed"]:
                return {"started": False, "id": g["id"], "message": f"{g['title']} est déjà installé"}
            self._open_url(f"steam://install/{key[0]}")
            return {"started": True, "id": f"steam:{key[0]}",
                    "message": "Steam ouvre la fenêtre d'installation (Steam vérifie que vous possédez le jeu)"}
        self._get(id)
        name = "Heroic" if src == "heroic" else "Lutris"
        raise Unavailable(f"L'installation se fait dans {name} : NEXUS ne peut pas la piloter. "
                          f"Utilisez « games.open_launcher » pour ouvrir {name}.",
                          details={"launcher": src, "hint": "open_launcher"})

    @action("uninstall", Risk.DELETE_DATA,
            "Désinstalle un jeu Steam (supprime ses fichiers ; Steam demande aussi confirmation)",
            {"id": Param("str", description="Identifiant du jeu, ex. steam:570", validator=parse_id)},
            summary=lambda p: f"Désinstaller le jeu {p.get('id')} (ses fichiers seront supprimés par le lanceur)")
    def uninstall(self, id):
        src, key = parse_id(id)
        g = self._get(id)
        if src != "steam":
            name = "Heroic" if src == "heroic" else "Lutris"
            raise Unavailable(f"La désinstallation se fait dans {name}. Utilisez « games.open_launcher ».",
                              details={"launcher": src, "hint": "open_launcher"})
        if not g["installed"]:
            return {"started": False, "id": g["id"], "message": f"{g['title']} n'est pas installé"}
        self._require_launcher("steam")
        # Steam itself shows its own confirmation dialog and performs the deletion.
        self._open_url(f"steam://uninstall/{key[0]}")
        return {"started": True, "id": g["id"],
                "message": "Steam affiche sa propre confirmation puis supprime les fichiers du jeu"}

    @action("open_launcher", Risk.CONTROL, "Ouvre l'interface d'un lanceur (Steam, Heroic ou Lutris)",
            {"launcher": Param("str", choices=["steam", "heroic", "lutris"])})
    def open_launcher(self, launcher):
        l = self._launchers()[launcher]
        if not l["detected"]:
            raise Unavailable(f"Lanceur non détecté: {launcher}", details={"launcher": launcher})
        if launcher == "steam":
            self._require_launcher("steam")
            self._open_url("steam://open/games")
        elif launcher == "heroic":
            if not l["flatpak"] or not self.ctx.runner.available("flatpak"):
                raise Unavailable("Seule la version Flatpak de Heroic peut être ouverte par NEXUS",
                                  details={"launcher": "heroic"})
            self.ctx.runner.spawn(["flatpak", "run", HEROIC_FLATPAK])
        else:
            if not self.ctx.runner.available("lutris"):
                raise Unavailable("Le programme lutris est absent", details={"binary": "lutris"})
            self.ctx.runner.spawn(["lutris"])
        return {"opened": launcher}

    @action("details", Risk.READ, "Détails d'un jeu (installation, taille, lanceur, outil de compatibilité)",
            {"id": ID_PARAM})
    def details(self, id):
        g = self._get(id)
        out = self._public(g)
        if g["source"] == "steam":
            appid = g["appid"]
            flags = int(g.get("state_flags") or 0)
            out.update({
                "state": [txt for bit, txt in STATE_BITS.items() if flags & bit] or
                         (["installé"] if g["installed"] else ["non installé"]),
                "build_id": g.get("buildid") or None,
                "compat_tool": g.get("compat_tool"),
                "links": {"store": f"https://store.steampowered.com/app/{appid}",
                          "protondb": f"https://www.protondb.com/app/{appid}"},
            })
        return out

    # ---------------------------------------------------------- compatibility
    def _protondb(self, appid: str) -> dict:
        cache = self.cache_dir / "protondb" / f"{appid}.json"
        cached = load_json(cache, None)
        if isinstance(cached, dict) and time.time() - float(cached.get("_ts") or 0) < PROTONDB_TTL:
            return {**{k: v for k, v in cached.items() if k != "_ts"}, "cached": True}
        url = PROTONDB_URL.format(appid=appid)
        try:
            data = self.ctx.net.get_json(url)
            if not isinstance(data, dict):
                raise ValueError("réponse inattendue")
        except (NexusError, ValueError) as e:
            msg = e.message if isinstance(e, NexusError) else str(e)
            if isinstance(cached, dict) and cached.get("protondb_tier"):
                return {**{k: v for k, v in cached.items() if k != "_ts"}, "stale": True,
                        "error": f"ProtonDB injoignable, dernière donnée connue : {msg}"}
            return {"tier": "inconnu", "protondb_tier": None, "source": "protondb",
                    "error": f"ProtonDB injoignable ou aucun rapport pour ce jeu ({msg})",
                    "note": "Aucune donnée : la compatibilité n'est pas connue."}
        raw = str(data.get("tier") or "").lower()
        res = {
            "tier": TIER_LEVELS.get(raw, "inconnu"),
            "protondb_tier": raw or None,
            "description": TIER_FR.get(raw),
            "confidence": data.get("confidence"),
            "total_reports": data.get("total"),
            "trending_tier": data.get("trendingTier"),
            "best_reported_tier": data.get("bestReportedTier"),
            "source": "protondb",
            "fetched_at": _iso(time.time()),
            "note": "Rapports communautaires ProtonDB : indication, pas une garantie.",
        }
        try:
            save_json(cache, {**res, "_ts": time.time()})
        except OSError:
            pass
        return res

    @action("compatibility", Risk.READ,
            "Niveau de compatibilité Linux/Proton d'un jeu (rapports ProtonDB pour Steam)", {"id": ID_PARAM})
    def compatibility(self, id):
        src, key = parse_id(id)
        cid = canonical_id(id)
        if src == "steam":
            return {"id": cid, **self._protondb(key[0])}
        return {"id": cid, "tier": "inconnu", "protondb_tier": None, "source": "aucune",
                "note": "Aucune base de compatibilité n'est interrogée pour les jeux hors Steam "
                        "(ProtonDB ne référence que les identifiants Steam)."}

    # --------------------------------------------------------------- diagnose
    def _hardware_info(self) -> dict | None:
        """hardware.info if the hardware module is loaded; None otherwise (never fatal)."""
        if not self.ctx.module("hardware"):
            return None
        try:
            info = self.ctx.call("hardware.info")
        except NexusError as e:
            self.log.info("hardware.info indisponible: %s", e.message)
            return None
        return info if isinstance(info, dict) else None

    @staticmethod
    def _vulkan_from_hw(vk: dict) -> dict:
        devices = [str(d.get("name", "")).strip() for d in (vk.get("devices") or []) if isinstance(d, dict)]
        if not vk.get("available") or not devices:
            reason = vk.get("reason") or "aucun périphérique Vulkan"
            if "absent" in reason and "vulkaninfo" in reason:
                return {"level": "info", "code": "vulkan_unchecked",
                        "message": "vulkaninfo absent : le support Vulkan n'a pas pu être vérifié"}
            return {"level": "error", "code": "vulkan_missing",
                    "message": f"Aucun périphérique Vulkan détecté : Proton/DXVK ne peut pas fonctionner ({reason})"}
        hw = [d for d in devices if "llvmpipe" not in d.lower()]
        if not hw:
            return {"level": "error", "code": "vulkan_software",
                    "message": "Seul le rendu logiciel (llvmpipe) est disponible : pilote GPU absent ou cassé",
                    "details": {"devices": devices}}
        return {"level": "ok", "code": "vulkan_ok", "message": f"Vulkan disponible : {', '.join(hw)}",
                "details": {"devices": devices}}

    def _vulkan(self) -> dict:
        if not self.ctx.runner.available("vulkaninfo"):
            return {"level": "info", "code": "vulkan_unchecked",
                    "message": "vulkaninfo absent : le support Vulkan n'a pas pu être vérifié"}
        try:
            r = self.ctx.runner.run(["vulkaninfo", "--summary"], timeout=20)
        except NexusError as e:
            return {"level": "warning", "code": "vulkan_error", "message": f"vulkaninfo a échoué : {e.message}"}
        devices = re.findall(r"deviceName\s*=\s*(.+)", r.stdout)
        devices = [d.strip() for d in devices]
        if not r.ok or not devices:
            return {"level": "error", "code": "vulkan_missing",
                    "message": "Aucun périphérique Vulkan détecté : Proton/DXVK ne peut pas fonctionner",
                    "details": {"stderr": r.stderr[-500:]}}
        hw = [d for d in devices if "llvmpipe" not in d.lower()]
        if not hw:
            return {"level": "error", "code": "vulkan_software",
                    "message": "Seul le rendu logiciel (llvmpipe) est disponible : pilote GPU absent ou cassé",
                    "details": {"devices": devices}}
        return {"level": "ok", "code": "vulkan_ok", "message": f"Vulkan disponible : {', '.join(hw)}",
                "details": {"devices": devices}}

    def _disk(self, path: str | None) -> dict | None:
        p = Path(path) if path else None
        while p is not None and not p.exists() and p != p.parent:
            p = p.parent
        if p is None:
            return None
        try:
            u = self.disk_usage(str(p))
        except OSError:
            return None
        free_gb = round(u.free / 1024 ** 3, 1)
        if u.free < CRITICAL_DISK:
            return {"level": "error", "code": "disk_critical", "message": f"Espace disque presque épuisé : {free_gb} Go libres",
                    "details": {"free": u.free, "path": str(p)}}
        if u.free < LOW_DISK:
            return {"level": "warning", "code": "disk_low",
                    "message": f"Peu d'espace disque : {free_gb} Go libres (mises à jour et caches de shaders peuvent échouer)",
                    "details": {"free": u.free, "path": str(p)}}
        return {"level": "ok", "code": "disk_ok", "message": f"Espace disque suffisant : {free_gb} Go libres",
                "details": {"free": u.free, "path": str(p)}}

    def _proton_log(self, appid: str) -> dict | None:
        f = self.home / f"steam-{appid}.log"
        if not f.is_file():
            return None
        try:
            with f.open("rb") as fh:
                size = f.stat().st_size
                fh.seek(max(0, size - 2 * 1024 * 1024))
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        lines = [l.strip() for l in text.splitlines() if LOG_ERR_RE.search(l)]
        # de-duplicate while keeping order, cap the extract
        seen, extract = set(), []
        for l in lines:
            k = re.sub(r"^[0-9a-f:.\s]+", "", l)[:200]
            if k not in seen:
                seen.add(k)
                extract.append(l[:300])
        extract = extract[-15:]
        return {"level": "warning" if extract else "info", "code": "proton_log",
                "message": (f"Journal Proton trouvé ({f.name}) : {len(extract)} ligne(s) d'erreur extraites"
                            if extract else f"Journal Proton trouvé ({f.name}) sans erreur évidente"),
                "details": {"path": str(f), "errors": extract}}

    @action("diagnose", Risk.READ, "Analyse pourquoi un jeu ne fonctionne pas (installation, Proton, Vulkan, disque, journaux)",
            {"id": ID_PARAM})
    def diagnose(self, id):
        src, key = parse_id(id)
        cid = canonical_id(id)
        findings: list[dict] = []
        suggestions: list[str] = []
        g = self._find(id)
        launchers = self._launchers()

        if not launchers[src]["detected"]:
            findings.append({"level": "error", "code": "launcher_missing",
                             "message": f"Le lanceur {src} n'est pas détecté sur ce système"})
        else:
            findings.append({"level": "ok", "code": "launcher_ok", "message": f"Lanceur {src} détecté"})
        if not self.ctx.runner.available("xdg-open"):
            findings.append({"level": "error", "code": "xdg_open_missing",
                             "message": "xdg-open absent : NEXUS ne peut pas demander au lanceur de démarrer le jeu"})

        if g is None:
            findings.append({"level": "error", "code": "not_in_library",
                             "message": "Ce jeu n'apparaît pas dans la bibliothèque locale du lanceur"})
            suggestions.append("Lancez « Actualiser la bibliothèque », ou installez le jeu depuis son lanceur.")
        else:
            if g["installed"]:
                findings.append({"level": "ok", "code": "installed", "message": "Le jeu est installé"})
            else:
                findings.append({"level": "error", "code": "not_installed", "message": "Le jeu n'est pas (entièrement) installé"})
                suggestions.append("Installez ou terminez la mise à jour du jeu depuis son lanceur.")
            if src == "steam":
                flags = int(g.get("state_flags") or 0)
                states = [txt for bit, txt in STATE_BITS.items() if flags & bit]
                if states:
                    findings.append({"level": "warning", "code": "steam_state",
                                     "message": f"État Steam : {', '.join(states)}"})
                    if flags & (32 | 128):
                        suggestions.append("Dans Steam : Propriétés > Fichiers installés > Vérifier l'intégrité des fichiers.")
            ip = g.get("install_path")
            if g["installed"] and ip:
                if not Path(ip).exists():
                    findings.append({"level": "error", "code": "install_dir_missing",
                                     "message": f"Dossier d'installation introuvable : {ip}"})
                    suggestions.append("Le disque contenant le jeu est peut-être non monté, ou les fichiers ont été déplacés.")
            d = self._disk(ip or g.get("library"))
            if d:
                findings.append(d)
                if d["level"] != "ok":
                    suggestions.append("Libérez de l'espace disque (au moins 5 Go recommandés).")

        if src == "steam":
            appid = key[0]
            comp = self._protondb(appid)
            lvl = {"stable": "ok", "bon": "ok", "expérimental": "warning", "limité": "warning",
                   "non supporté": "error"}.get(comp["tier"], "info")
            findings.append({"level": lvl, "code": "protondb",
                             "message": (f"ProtonDB : {comp.get('description') or comp['protondb_tier']} "
                                         f"({comp.get('total_reports') or '?'} rapports)")
                             if comp.get("protondb_tier") else "ProtonDB : compatibilité inconnue (aucune donnée obtenue)",
                             "details": comp})
            if comp["tier"] in ("limité", "non supporté", "expérimental"):
                suggestions.append("Consultez les rapports ProtonDB pour les options de lancement qui fonctionnent ; "
                                   "essayez Proton Experimental ou une autre version de Proton (Propriétés > Compatibilité).")
            roots = [Path(r) for r in (launchers["steam"]["roots"] or [])]
            root = Path(g["root"]) if g and g.get("root") else (roots[0] if roots else None)
            if root:
                tools = steam.compat_tools(root, steam.library_paths(root))
                if tools:
                    findings.append({"level": "ok", "code": "proton_present",
                                     "message": f"Outils de compatibilité installés : {', '.join(tools)}"})
                else:
                    findings.append({"level": "warning", "code": "proton_missing",
                                     "message": "Aucune version de Proton installée (Steam la télécharge au premier "
                                                "lancement d'un jeu Windows)"})
                forced = g.get("compat_tool") if g else None
                if forced and tools and forced not in tools and not forced.startswith("proton_"):
                    findings.append({"level": "error", "code": "compat_tool_missing",
                                     "message": f"L'outil de compatibilité forcé « {forced} » n'est pas installé"})
                    suggestions.append("Choisissez une version de Proton installée dans Propriétés > Compatibilité.")
                elif forced:
                    findings.append({"level": "info", "code": "compat_tool_forced",
                                     "message": f"Outil de compatibilité forcé pour ce jeu : {forced}"})
            log_f = self._proton_log(appid)
            if log_f:
                findings.append(log_f)
            else:
                suggestions.append("Pour obtenir un journal détaillé : options de lancement « PROTON_LOG=1 %command% », "
                                   f"lancez le jeu puis relancez le diagnostic (fichier ~/steam-{appid}.log).")
        elif src == "heroic":
            suggestions.append("Dans Heroic : page du jeu > Paramètres > « Journaux » pour voir l'erreur de lancement.")
        else:
            suggestions.append("Dans Lutris : clic droit sur le jeu > « Afficher les journaux ».")

        hw = self._hardware_info()
        if hw and isinstance(hw.get("vulkan"), dict):
            findings.append(self._vulkan_from_hw(hw["vulkan"]))  # avoid running vulkaninfo twice
        else:
            findings.append(self._vulkan())
        if hw:
            cpu = hw.get("cpu") if isinstance(hw.get("cpu"), dict) else {}
            if cpu.get("avx2") is False:
                findings.append({"level": "warning", "code": "cpu_no_avx2",
                                 "message": "Le processeur ne prend pas en charge AVX2 : certains jeux récents "
                                            "l'exigent et refusent de démarrer ou plantent au lancement"})
            gpus = [g for g in (hw.get("gpus") or []) if isinstance(g, dict)]
            if gpus:
                findings.append({"level": "info", "code": "gpu",
                                 "message": "Carte(s) graphique(s) : " + ", ".join(
                                     f"{g.get('name') or g.get('vendor')} (pilote {g.get('driver') or '?'})" for g in gpus)})
        else:
            findings.append({"level": "info", "code": "hardware_unchecked",
                             "message": "Module matériel indisponible : processeur et GPU non vérifiés"})
        if any(f["code"] in ("vulkan_missing", "vulkan_software") for f in findings):
            suggestions.append("Vérifiez le pilote graphique (Bazzite : image -nvidia pour les cartes NVIDIA).")

        errors = [f for f in findings if f["level"] == "error"]
        warnings = [f for f in findings if f["level"] == "warning"]
        if errors:
            verdict = f"{len(errors)} problème(s) bloquant(s) trouvé(s) : " + " ; ".join(f["message"] for f in errors[:3])
        elif warnings:
            verdict = "Aucun blocage évident ; points à surveiller : " + " ; ".join(f["message"] for f in warnings[:3])
        else:
            verdict = "Aucun problème détecté par les vérifications automatiques."
        return {"id": cid, "title": g["title"] if g else None, "verdict": verdict,
                "findings": findings, "suggestions": suggestions}

    # --------------------------------------------------------------- optimize
    def _gpu_vendors(self) -> list[str]:
        out = []
        try:
            for f in sorted(self.drm_root.glob("card*/device/vendor")):
                v = GPU_VENDORS.get(f.read_text().strip().lower())
                if v and v not in out:
                    out.append(v)
        except OSError:
            pass
        if not out:  # fall back on the hardware module when sysfs is not readable here
            hw = self._hardware_info() or {}
            for g in hw.get("gpus") or []:
                v = g.get("vendor") if isinstance(g, dict) else None
                if v in ("nvidia", "amd", "intel") and v not in out:
                    out.append(v)
        return out

    @action("optimize", Risk.READ,
            "Recommandations concrètes pour améliorer les performances d'un jeu (rien n'est modifié automatiquement)",
            {"id": ID_PARAM})
    def optimize(self, id):
        src, key = parse_id(id)
        cid = canonical_id(id)
        g = self._find(id)
        gpus = self._gpu_vendors()
        has_gm = self.ctx.runner.available("gamemoderun")
        has_mh = self.ctx.runner.available("mangohud")
        recs: list[dict] = []
        env: list[str] = []
        wrappers: list[str] = []
        if has_gm:
            wrappers.append("gamemoderun")
        recs.append({"title": "GameMode", "option": "gamemoderun %command%", "available": has_gm,
                     "why": "Active le gouverneur CPU performance et priorise le jeu pendant la partie."
                            + ("" if has_gm else " (gamemoderun introuvable sur ce système)")})
        mh_on = bool(self.ctx.settings.get("hardware.mangohud", False))
        if has_mh and mh_on:
            wrappers.append("mangohud")
        recs.append({"title": "MangoHud", "option": "mangohud %command%", "available": has_mh,
                     "enabled_in_settings": mh_on,
                     "why": "Affiche FPS, températures et charge GPU/CPU pour mesurer l'effet des réglages."
                            + (" Overlay activé dans NEXUS (réglage hardware.mangohud) : inclus dans la ligne "
                               "proposée, il prend effet sur Steam uniquement via les options de lancement."
                               if mh_on else " Overlay désactivé dans NEXUS : non inclus dans la ligne proposée "
                               "(activez-le dans Matériel > Overlay ou ajoutez « mangohud » vous-même).")
                            + ("" if has_mh else " (mangohud introuvable sur ce système)")})
        if "nvidia" in gpus:
            env.append("PROTON_ENABLE_NVAPI=1")
            recs.append({"title": "NVAPI (NVIDIA)", "option": "PROTON_ENABLE_NVAPI=1 %command%", "available": True,
                         "why": "Expose NVAPI aux jeux Windows : nécessaire pour DLSS et Reflex sur carte NVIDIA."})
        if src == "steam":
            recs.append({"title": "Journal Proton (dépannage)", "option": "PROTON_LOG=1 %command%", "available": True,
                         "why": "Écrit ~/steam-<appid>.log, utile si le jeu plante (à retirer ensuite)."})
            comp = self._protondb(key[0])
            if comp["tier"] in ("expérimental", "limité", "non supporté"):
                recs.append({"title": "Changer de version de Proton", "option": None, "available": True,
                             "why": f"ProtonDB classe ce jeu « {comp['protondb_tier']} » : essayez Proton Experimental "
                                    "ou GE-Proton (Propriétés > Compatibilité)."})
        if not gpus:
            recs.append({"title": "GPU non identifié", "option": None, "available": False,
                         "why": "Impossible de lire le fabricant du GPU : recommandations spécifiques NVIDIA/AMD omises."})
        launch_options = " ".join(env + wrappers + ["%command%"]) if (env or wrappers) else None
        out = {"id": cid, "title": g["title"] if g else None, "gpu": gpus, "recommendations": recs, "applied": False}
        if src == "steam":
            out["steam_launch_options"] = launch_options
            out["how_to_apply"] = ("Steam > clic droit sur le jeu > Propriétés > Général > Options de lancement : "
                                   "collez la ligne proposée.")
            out["note"] = ("NEXUS n'écrit pas dans la configuration de Steam (localconfig.vdf) : Steam la réécrit "
                           "à la fermeture et une modification externe peut être perdue ou corrompre vos réglages. "
                           "Les réglages NEXUS (ex. overlay MangoHud, hardware.mangohud) ne s'appliquent donc aux "
                           "jeux Steam qu'à travers ces options de lancement.")
        elif src == "heroic":
            out["how_to_apply"] = ("Heroic > page du jeu > Paramètres : cochez « Utiliser GameMode » et "
                                   "« Activer MangoHud » ; les variables d'environnement se mettent dans « Avancé ».")
            out["note"] = "NEXUS ne modifie pas la configuration de Heroic."
        else:
            out["how_to_apply"] = ("Lutris > clic droit sur le jeu > Configurer > Options système : « Activer Feral "
                                   "GameMode », « Compteur FPS (MangoHud) », variables d'environnement.")
            out["note"] = "NEXUS ne modifie pas la configuration de Lutris."
        return out


MODULE = GamesModule
