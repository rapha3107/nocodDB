"""NEXUS VR module: headset detection (USB sysfs), OpenXR runtimes, VR apps, Monado control.

Nothing here promises compatibility: every answer carries a `support` level
(`bon`, `expérimental`, `limité`, `inconnu`) and `notes`.

Injectable paths (constructor kwargs, or settings `vr.sys_root` / `vr.etc_root` for dev):
  * `sys_root`  — sysfs root (`/sys`), USB devices read from `bus/usb/devices/*`;
  * `etc_root`  — `/etc` (system OpenXR active_runtime.json);
  * `home`      — user home (defaults to ctx.paths.home): Steam libraries, ~/.config/openxr.
"""

from __future__ import annotations

import json
import os
import re
import signal
from pathlib import Path

from ...core.errors import InvalidParams, NexusError, Unavailable
from ...core.flatpak import Flatpak
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk

# --- Headsets -----------------------------------------------------------------------------
# Exact VID:PID pairs are those used by Monado's USB probers (vive, rift-s, oh/rift, wmr).
# Vendor-only entries (`pid: None`) are reported with match="vendor".
SUPPORT_NOTES = {
    "lighthouse": "Bon support via SteamVR (Proton/Linux natif) ou Monado (pilote vive/lighthouse). "
                  "Stations de base requises.",
    "rift_cv1": "Expérimental : Monado (pilote OpenHMD/rift) ou OpenHMD ; capteurs Constellation "
                "partiellement pris en charge. Pas de runtime Oculus officiel sous Linux.",
    "rift_s": "Expérimental via Monado (pilote rift-s, suivi inside-out partiel).",
    "quest": "Pas de support USB direct (Oculus Link n'existe pas sous Linux). Utilisez WiVRn ou ALVR "
             "en sans-fil (Wi-Fi 5/6 recommandé, réseau local).",
    "wmr": "Expérimental via Monado (pilote WMR) ; suivi des manettes limité selon les modèles.",
    "psvr2": "Expérimental / limité : pas de support officiel sous Linux ; projets communautaires en cours.",
    "pimax": "Limité : support non officiel, à vérifier modèle par modèle.",
    "unknown": "Modèle non identifié avec certitude : support inconnu.",
}

HEADSETS: list[dict] = [
    {"vid": "28de", "pid": "2300", "model": "Valve Index", "family": "lighthouse", "support": "bon"},
    {"vid": "0bb4", "pid": "2c87", "model": "HTC Vive", "family": "lighthouse", "support": "bon"},
    {"vid": "0bb4", "pid": "0309", "model": "HTC Vive Pro", "family": "lighthouse", "support": "bon"},
    {"vid": "2833", "pid": "0031", "model": "Oculus Rift CV1", "family": "rift_cv1", "support": "expérimental"},
    {"vid": "2833", "pid": "0051", "model": "Oculus Rift S", "family": "rift_s", "support": "expérimental"},
    {"vid": "03f0", "pid": "0580", "model": "HP Reverb G2", "family": "wmr", "support": "expérimental"},
    {"vid": "03f0", "pid": "0c6a", "model": "HP Reverb (G1)", "family": "wmr", "support": "expérimental"},
    # Every WMR headset exposes this Microsoft "HoloLens Sensors" companion device.
    {"vid": "045e", "pid": "0659", "model": "Casque Windows Mixed Reality", "family": "wmr",
     "support": "expérimental"},
    # Oculus/Meta vendor id: Quest models (and other Oculus devices) not identified by PID here.
    {"vid": "2833", "pid": None, "model": "Casque Oculus/Meta (Quest ou autre)", "family": "quest",
     "support": "limité"},
]
# Headsets identified by their USB product string only (VID unknown/ambiguous for us).
NAME_PATTERNS: list[tuple[re.Pattern, dict]] = [
    (re.compile(r"pimax", re.I), {"model": "Pimax", "family": "pimax", "support": "limité"}),
    (re.compile(r"ps\s*vr\s*2|playstation\s*vr2", re.I),
     {"model": "PlayStation VR2", "family": "psvr2", "support": "expérimental"}),
    (re.compile(r"\bquest\b", re.I), {"model": "Meta Quest", "family": "quest", "support": "limité"}),
]

# --- Runtimes -----------------------------------------------------------------------------
STEAMVR_APPID = "250820"
WIVRN_FLATPAK = "io.github.wivrn.wivrn"

# --- Known VR apps (Steam app ids known with certainty) ------------------------------------
KNOWN_VR_APPS = {
    "546560": "Half-Life: Alyx",
    "620980": "Beat Saber",
    "450390": "The Lab",
    "823500": "BONEWORKS",
    "438100": "VRChat",
}

PERF_LEVELS = {"economie": "power-saver", "equilibre": "balanced", "performance": "performance"}


def _read(p: Path) -> str | None:
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return None


def _acf_value(text: str, key: str) -> str | None:
    m = re.search(r'"%s"\s+"([^"]*)"' % re.escape(key), text, re.I)
    return m.group(1) if m else None


def identify_headset(vid: str | None, pid: str | None, product: str | None) -> dict | None:
    vid, pid = (vid or "").lower(), (pid or "").lower()
    for h in HEADSETS:
        if h["pid"] is not None and h["vid"] == vid and h["pid"] == pid:
            return {**h, "match": "vid_pid"}
    for pat, h in NAME_PATTERNS:
        if product and pat.search(product):
            return {**h, "vid": vid or None, "pid": pid or None, "match": "name"}
    for h in HEADSETS:
        if h["pid"] is None and h["vid"] == vid:
            return {**h, "pid": pid or None, "match": "vendor"}
    return None


class VRModule(NexusModule):
    name = "vr"
    title = "Réalité virtuelle"
    description = "Casques VR, runtimes OpenXR (Monado, SteamVR, WiVRn, ALVR) et jeux VR"

    def __init__(self, ctx, *, sys_root: str | Path | None = None, etc_root: str | Path | None = None,
                 home: str | Path | None = None):
        super().__init__(ctx)
        self.sys = Path(sys_root or ctx.settings.get("vr.sys_root", "/sys"))
        self.etc = Path(etc_root or ctx.settings.get("vr.etc_root", "/etc"))
        self.home = Path(home or ctx.paths.home)
        self.flatpak = Flatpak(ctx.runner)
        self._monado_pid: int | None = None

    # -------------------------------------------------------------- headsets
    def _headsets(self) -> list[dict]:
        root = self.sys / "bus" / "usb" / "devices"
        found: dict[str, dict] = {}
        if not root.is_dir():
            return []
        for d in sorted(root.iterdir()):
            vid = (_read(d / "idVendor") or "").lower()
            if not vid:
                continue  # interfaces (1-1:1.0) have no idVendor
            pid = (_read(d / "idProduct") or "").lower()
            product = _read(d / "product")
            h = identify_headset(vid, pid, product)
            if not h:
                continue
            key = h["model"]
            # Prefer an exact match over a vendor/name match for the same model.
            if key in found and found[key]["match"] == "vid_pid":
                continue
            found[key] = {
                "model": h["model"], "family": h["family"], "vendor_id": vid, "product_id": pid or None,
                "usb_product": product, "usb_path": d.name, "match": h["match"],
                "support": h["support"],
                "notes": SUPPORT_NOTES.get(h["family"], SUPPORT_NOTES["unknown"]),
            }
        # A WMR companion device next to an exact HP Reverb match is the same headset.
        if any(v["family"] == "wmr" and v["model"] != "Casque Windows Mixed Reality" for v in found.values()):
            found.pop("Casque Windows Mixed Reality", None)
        # Rift CV1 sensors / other Oculus devices next to an identified Rift are not a Quest.
        if any(v["family"] in ("rift_cv1", "rift_s") for v in found.values()):
            found.pop("Casque Oculus/Meta (Quest ou autre)", None)
        return list(found.values())

    # -------------------------------------------------------------- runtimes
    def _steam_libraries(self) -> list[Path]:
        roots = [self.home / ".steam" / "steam", self.home / ".local" / "share" / "Steam",
                 self.home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"]
        libs: list[Path] = []
        for r in roots:
            sa = r / "steamapps"
            if sa.is_dir():
                libs.append(sa)
                vdf = _read(sa / "libraryfolders.vdf") or ""
                for p in re.findall(r'"path"\s+"([^"]+)"', vdf):
                    libs.append(Path(p.replace("\\\\", "\\")) / "steamapps")
        out, seen = [], set()
        for l in libs:
            try:
                k = l.resolve()
            except OSError:
                k = l
            if k not in seen and l.is_dir():
                seen.add(k)
                out.append(l)
        return out

    def _appmanifests(self) -> dict[str, dict]:
        apps = {}
        for lib in self._steam_libraries():
            for m in lib.glob("appmanifest_*.acf"):
                text = _read(m) or ""
                appid = _acf_value(text, "appid") or m.stem.removeprefix("appmanifest_")
                if appid.isdigit() and appid not in apps:
                    apps[appid] = {"appid": appid, "name": _acf_value(text, "name"),
                                   "installdir": _acf_value(text, "installdir"), "library": str(lib)}
        return apps

    def _flatpak_ids(self) -> list[str]:
        if not self.flatpak.available():
            return []
        try:
            return [a.app_id for a in self.flatpak.installed()]
        except NexusError:
            return []

    def _active_runtime(self) -> dict | None:
        for p in (self.home / ".config" / "openxr" / "1" / "active_runtime.json",
                  self.etc / "xdg" / "openxr" / "1" / "active_runtime.json"):
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                return {"path": str(p), "name": None, "runtime": "inconnu", "error": "Fichier illisible"}
            rt = data.get("runtime", {}) if isinstance(data, dict) else {}
            name = rt.get("name") if isinstance(rt, dict) else None
            lib = rt.get("library_path") if isinstance(rt, dict) else None
            hay = f"{name or ''} {lib or ''} {os.path.realpath(p)}".lower()
            kind = ("monado" if "monado" in hay else "wivrn" if "wivrn" in hay
                    else "steamvr" if ("steamvr" in hay or "vrclient" in hay) else "alvr" if "alvr" in hay
                    else "inconnu")
            return {"path": str(p), "name": name, "library_path": lib, "runtime": kind,
                    "scope": "utilisateur" if str(p).startswith(str(self.home)) else "système"}
        return None

    def _runtimes(self) -> dict:
        fp = self._flatpak_ids()
        apps = self._appmanifests()
        monado_bin = self.ctx.runner.available("monado-service")
        monado_fp = [a for a in fp if "monado" in a.lower()]
        alvr_fp = [a for a in fp if "alvr" in a.lower()]
        return {
            "monado": {"installed": bool(monado_bin or monado_fp),
                       "source": "système" if monado_bin else ("flatpak" if monado_fp else None),
                       "running": self._monado_running(),
                       "notes": "Runtime OpenXR libre ; casques Lighthouse (bon), Rift/WMR (expérimental)."},
            "steamvr": {"installed": STEAMVR_APPID in apps,
                        "source": "steam" if STEAMVR_APPID in apps else None,
                        "notes": "Lancé par Steam (NEXUS ne le démarre pas lui-même)."},
            "wivrn": {"installed": WIVRN_FLATPAK in fp, "source": "flatpak" if WIVRN_FLATPAK in fp else None,
                      "notes": "Streaming sans fil vers Quest/Pico (application WiVRn sur le casque, même réseau)."},
            "alvr": {"installed": bool(alvr_fp), "source": "flatpak" if alvr_fp else None,
                     "notes": "Streaming sans fil via SteamVR. Détection limitée au Flatpak ; "
                              "une installation AppImage n'est pas détectée."},
        }

    def _recommend(self, headsets: list[dict], runtimes: dict) -> str:
        if not headsets:
            return ("Aucun casque USB détecté. Pour un Quest, installez WiVRn (Flathub) et utilisez le "
                    "streaming sans fil sur le réseau local.")
        fams = {h["family"] for h in headsets}
        if "lighthouse" in fams:
            if runtimes["steamvr"]["installed"]:
                return "Casque Lighthouse détecté : utilisez SteamVR (via Steam) ou Monado."
            return "Casque Lighthouse détecté : installez SteamVR depuis Steam, ou utilisez Monado."
        if "quest" in fams:
            return "Quest détecté en USB : le mode Link USB n'est pas disponible sous Linux, utilisez WiVRn ou ALVR."
        if fams & {"rift_cv1", "rift_s", "wmr"}:
            return "Casque pris en charge de façon expérimentale par Monado uniquement : attendez-vous à des limites."
        return "Support incertain pour ce casque : consultez la documentation de Monado."

    # --------------------------------------------------------- monado proc
    def _monado_running(self) -> bool:
        pid = self._monado_pid
        if not pid:
            return False
        comm = _read(Path("/proc") / str(pid) / "comm")
        stat = _read(Path("/proc") / str(pid) / "stat") or ""
        if comm != "monado-service" or re.search(r"\)\s+Z\s", stat):
            self._monado_pid = None
            return False
        return True

    # --------------------------------------------------------------- actions
    @action("status", Risk.READ, "État VR : casques détectés, runtimes OpenXR, runtime actif, recommandation")
    def vr_status(self):
        headsets = self._headsets()
        runtimes = self._runtimes()
        active = self._active_runtime()
        support = max((h["support"] for h in headsets), key=["inconnu", "limité", "expérimental", "bon"].index,
                      default="inconnu")
        return {
            "headsets": headsets,
            "runtimes": runtimes,
            "active_runtime": active,
            "recommendation": self._recommend(headsets, runtimes),
            "performance_level": self.ctx.settings.get("vr.performance", "equilibre"),
            "support": support,
            "notes": "Détection USB indicative : la compatibilité dépend du casque, du runtime et du jeu. "
                     "Aucune compatibilité n'est garantie.",
        }

    @action("apps", Risk.READ, "Jeux VR Steam installés (liste connue, non exhaustive)",
            {"include_unknown": Param("bool", required=False, default=False,
                                      description="Inclure les autres jeux Steam (VR inconnu)")})
    def apps(self, include_unknown=False):
        manifests = self._appmanifests()
        vr, other = [], []
        for appid, m in sorted(manifests.items(), key=lambda kv: (kv[1]["name"] or "").lower()):
            if appid == STEAMVR_APPID:
                continue
            if appid in KNOWN_VR_APPS:
                vr.append({**m, "name": m["name"] or KNOWN_VR_APPS[appid], "vr": "oui", "launch": f"steam://rungameid/{appid}"})
            else:
                other.append({**m, "vr": "inconnu"})
        return {"apps": vr + (other if include_unknown else []),
                "unknown_count": len(other),
                "support": "inconnu",
                "notes": "Seuls quelques jeux VR connus sont reconnus ; les autres jeux sont marqués « inconnu ». "
                         "Le bon fonctionnement dépend du casque, du runtime et de Proton."}

    @action("start_runtime", Risk.CONTROL, "Démarre le service Monado (runtime OpenXR libre)")
    def start_runtime(self):
        if not self.ctx.runner.available("monado-service"):
            raise Unavailable("monado-service absent : Monado n'est pas installé sur ce système. "
                              "SteamVR se lance depuis Steam.", details={"binary": "monado-service"})
        if self._monado_running():
            return {"ok": True, "already_running": True, "pid": self._monado_pid, "support": "expérimental",
                    "notes": "Monado est déjà démarré par NEXUS."}
        self._monado_pid = self.ctx.runner.spawn(["monado-service"])
        active = self._active_runtime()
        notes = "Monado démarré. Lancez ensuite l'application OpenXR."
        if not active or active.get("runtime") != "monado":
            notes += (" Attention : le runtime OpenXR actif n'est pas Monado ; les jeux risquent d'utiliser "
                      "un autre runtime (active_runtime.json).")
        self.ctx.events.publish("vr.runtime", {"runtime": "monado", "running": True})
        return {"ok": True, "pid": self._monado_pid, "active_runtime": active, "support": "expérimental",
                "notes": notes}

    @action("stop_runtime", Risk.CONTROL, "Arrête le service Monado démarré par NEXUS")
    def stop_runtime(self):
        if not self.ctx.runner.available("monado-service"):
            raise Unavailable("monado-service absent : rien à arrêter.", details={"binary": "monado-service"})
        if not self._monado_running():
            return {"ok": True, "was_running": False, "support": "expérimental",
                    "notes": "Aucun service Monado démarré par NEXUS. SteamVR se ferme depuis Steam."}
        pid = self._monado_pid
        try:
            os.kill(pid, signal.SIGTERM)  # only our own child, verified by /proc/<pid>/comm
        except ProcessLookupError:
            pass
        self._monado_pid = None
        self.ctx.events.publish("vr.runtime", {"runtime": "monado", "running": False})
        return {"ok": True, "was_running": True, "pid": pid, "support": "expérimental",
                "notes": "Signal d'arrêt envoyé à Monado."}

    @action("set_performance", Risk.SETTINGS, "Règle le niveau de performance VR (économie, équilibré, performance)",
            {"level": Param("str", choices=list(PERF_LEVELS), description="economie | equilibre | performance")},
            summary=lambda p: f"Régler la performance VR sur « {p.get('level')} »")
    def set_performance(self, level):
        if level not in PERF_LEVELS:
            raise InvalidParams("Niveau inconnu")
        self.ctx.settings.set("vr.performance", level)
        profile = PERF_LEVELS[level]
        applied, notes = False, ""
        if self.ctx.module("hardware") is None:
            notes = "Module matériel indisponible : réglage enregistré mais non appliqué."
        else:
            try:
                self.ctx.call("hardware.set_performance_profile", {"profile": profile})
                applied = True
                notes = f"Profil d'énergie système « {profile} » appliqué."
            except NexusError as e:
                notes = f"Réglage enregistré mais profil d'énergie non appliqué : {e.message}"
        return {"ok": True, "level": level, "power_profile": profile, "applied": applied,
                "support": "bon" if applied else "limité",
                "notes": notes + " Le niveau n'agit que sur le profil d'énergie du système, pas sur la "
                                 "résolution de rendu du runtime VR."}

    def status(self) -> dict:
        try:
            headsets = self._headsets()
        except OSError:
            headsets = []
        return {"ok": True, "headsets": [h["model"] for h in headsets],
                "monado_installed": self.ctx.runner.available("monado-service"),
                "monado_running": self._monado_running(),
                "active_runtime": (self._active_runtime() or {}).get("runtime"),
                "performance_level": self.ctx.settings.get("vr.performance", "equilibre")}
