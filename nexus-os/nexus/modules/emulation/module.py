"""NEXUS emulation module: emulator catalogue, ROM library, launch, recommendations."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import unquote

from ...core.config import load_json, save_json
from ...core.errors import CommandFailed, InvalidParams, NexusError, NotFound, Unavailable
from ...core.flatpak import Flatpak
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk
from ...security.verify import safe_join, validate_flatpak_id
from . import hardware
from .catalog import EMULATORS, RETROARCH_CORES, SYSTEMS, public_emulator
from .library import COVER_EXTS, _detect_dir_game, scan_system

EMU_KEYS = sorted(EMULATORS)
SYSTEM_KEYS = list(SYSTEMS)

CONTROLLER_HELP = {
    "summary": "Les émulateurs du catalogue (RetroArch, PCSX2, RPCS3, Dolphin, PPSSPP, Cemu, melonDS, "
               "mGBA, xemu, Flycast, Azahar…) utilisent SDL2 pour les manettes : une DualSense/DualShock 4 "
               "ou une manette Xbox est reconnue directement. L'association des touches se règle dans les "
               "paramètres de chaque émulateur ; NEXUS n'écrit aucune configuration de manette.",
    "notes": [
        "Dans la session gamescope, Steam Input peut intercepter la manette : si un émulateur ne la voit "
        "pas, désactivez Steam Input pour ce raccourci ou lancez l'émulateur hors de Steam.",
        "Le gyroscope de la DualSense est exploitable dans les émulateurs qui gèrent le mouvement SDL "
        "(ex. Cemu, Dolphin, Azahar) via leurs propres réglages.",
        "La barre lumineuse et l'appairage Bluetooth relèvent du module hardware.",
    ],
}


class EmulationModule(NexusModule):
    name = "emulation"
    title = "Émulation"
    description = "Émulateurs libres (Flathub), bibliothèque de ROMs personnelles et lancement."

    def __init__(self, ctx, *, cpuinfo_path: str | Path = "/proc/cpuinfo",
                 meminfo_path: str | Path = "/proc/meminfo",
                 flatpak_system_dir: str | Path = "/var/lib/flatpak"):
        super().__init__(ctx)
        self.cpuinfo_path = Path(cpuinfo_path)
        self.meminfo_path = Path(meminfo_path)
        self.flatpak_system_dir = Path(flatpak_system_dir)
        self.flatpak = Flatpak(ctx.runner)
        self._lock = threading.Lock()
        self._index_path = ctx.paths.cache / "emulation" / "roms.json"
        self._index: dict | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def home(self) -> Path:
        return Path(self.ctx.paths.home)

    def rom_dir(self) -> Path:
        v = self.ctx.settings.get("emulation.rom_dir")
        if not v:
            return self.home / "Games" / "ROMs"
        v = str(v)
        if v == "~" or v.startswith("~/"):
            return self.home / v[2:]
        return Path(v)

    def _installed_ids(self) -> set[str] | None:
        """Installed Flatpak app ids, or None when Flatpak cannot be queried."""
        try:
            if not self.flatpak.available():
                return None
            return {a.app_id for a in self.flatpak.installed()}
        except NexusError:
            return None

    @staticmethod
    def _emu(key: str) -> dict:
        if key not in EMULATORS:
            raise NotFound(f"Émulateur inconnu : {key}")
        return EMULATORS[key]

    def _emu_view(self, key: str, installed: set[str] | None) -> dict:
        d = public_emulator(key)
        fid = d["flatpak_id"]
        d["installed"] = None if installed is None else bool(fid and fid in installed)
        return d

    def _load_index(self) -> dict:
        if self._index is None:
            data = load_json(self._index_path, None)
            self._index = data if isinstance(data, dict) and "systems" in data else None
        return self._index

    # ------------------------------------------------------------------ status / files

    def status(self) -> dict:
        installed = self._installed_ids()
        idx = self._load_index() or {}
        counts = {s: len(v) for s, v in (idx.get("systems") or {}).items() if v}
        rd = self.rom_dir()
        out = {
            "ok": installed is not None,
            "flatpak": installed is not None,
            "rom_dir": str(rd),
            "rom_dir_exists": rd.is_dir(),
            "installed_emulators": sorted(k for k, e in EMULATORS.items()
                                          if installed and e["flatpak_id"] in installed),
            "roms_per_system": counts,
            "roms_total": sum(counts.values()),
            "last_scan": idx.get("scanned_at"),
        }
        if installed is None:
            out["message"] = "Flatpak indisponible : installation et lancement des émulateurs impossibles."
        return out

    def serve_file(self, subpath: str) -> Path | None:
        sub = unquote(subpath or "")
        if not sub.startswith("cover/"):
            return None
        rest = sub[len("cover/"):]
        system, _, rel = rest.partition("/")
        if system not in SYSTEMS or not rel:
            return None
        try:
            p = safe_join(self.rom_dir() / system, rel)
        except NexusError:
            return None
        if p.suffix.lower() not in COVER_EXTS or not p.is_file():
            return None
        return p

    # ------------------------------------------------------------------ catalogue

    @action("systems", Risk.READ, "Liste les plateformes émulées avec émulateur recommandé, niveau de "
                                  "support honnête et statut d'installation.")
    def systems(self):
        installed = self._installed_ids()
        idx = self._load_index() or {}
        out = []
        for key, s in SYSTEMS.items():
            emus = s["emulators"]
            rec = next((e for e in emus if EMULATORS[e]["installable"]), emus[0] if emus else None)
            inst = None if installed is None or not rec else any(
                EMULATORS[e]["flatpak_id"] in installed for e in emus if EMULATORS[e]["flatpak_id"])
            out.append({
                "id": key, "name": s["name"], "support": s["support"], "note": s["note"],
                "recommended_emulator": rec,
                "emulators": emus,
                "installed": inst if rec else False,
                "rom_count": len((idx.get("systems") or {}).get(key, [])),
                "extensions": s["extensions"],
                "rom_folder": str(self.rom_dir() / key) if s["kind"] != "none" else None,
            })
        return out

    @action("emulators", Risk.READ, "Catalogue des émulateurs libres (identifiants Flathub, licence, "
                                    "compatibilité, BIOS requis) avec leur statut d'installation.")
    def emulators(self):
        installed = self._installed_ids()
        return {"flatpak_available": installed is not None,
                "emulators": [self._emu_view(k, installed) for k in EMULATORS]}

    @action("details", Risk.READ, "Détails d'un émulateur : sauvegardes, BIOS/firmware, exigences, "
                                  "syntaxe de lancement.",
            {"emulator": Param("str", choices=EMU_KEYS, description="Identifiant de l'émulateur")})
    def details(self, emulator):
        e = self._emu(emulator)
        d = self._emu_view(emulator, self._installed_ids())
        if e.get("bios_dir"):
            bdir = self.home / e["bios_dir"]
            try:
                has = bdir.is_dir() and any(bdir.iterdir())
            except OSError:
                has = False
            d["bios_dir_path"] = str(bdir)
            d["bios_dir_has_files"] = has  # indicator only: contents are not verified
        if emulator == "retroarch":
            d["cores"] = {s: [{"core": c, "path": self._find_core(c)} for c in cores]
                          for s, cores in RETROARCH_CORES.items()}
        return d

    @action("controller_help", Risk.READ, "Explique la prise en charge des manettes (DualSense via SDL2) "
                                          "dans les émulateurs.")
    def controller_help(self):
        return CONTROLLER_HELP

    # ------------------------------------------------------------------ install / uninstall

    @action("install", Risk.INSTALL_VERIFIED, "Installe un émulateur libre depuis Flathub (tâche de fond).",
            {"emulator": Param("str", choices=EMU_KEYS, description="Identifiant de l'émulateur")},
            summary=lambda p: f"Installer l'émulateur {p.get('emulator')} depuis Flathub")
    def install(self, emulator):
        e = self._emu(emulator)
        if not e["installable"] or not e["flatpak_id"]:
            raise Unavailable(f"{e['name']} n'est pas installable automatiquement : "
                              f"{e['compatibility']['note']}",
                              details={"emulator": emulator, "availability": e["availability"]})
        app_id = validate_flatpak_id(e["flatpak_id"])
        if not self.flatpak.available():
            raise Unavailable("Flatpak est absent de ce système", details={"binary": "flatpak"})

        def job(task):
            if e["availability"] != "flathub":
                task.update(0.05, f"Vérification de {app_id} sur Flathub")
                r = self.ctx.runner.run(["flatpak", "remote-info", "flathub", app_id], timeout=120)
                if not r.ok:
                    raise NotFound(f"{app_id} est introuvable sur Flathub",
                                   details={"stderr": (r.stderr or "")[-500:]})
            task.update(0.1, f"Installation de {e['name']} depuis Flathub")
            self.flatpak.install(app_id, "flathub")
            task.update(1.0, f"{e['name']} installé")
            self.ctx.events.publish("emulation.installed", {"emulator": emulator, "app_id": app_id})
            return {"emulator": emulator, "app_id": app_id, "installed": True}

        return self.ctx.tasks.submit(f"Installation de {e['name']}", job)

    @action("uninstall", Risk.DELETE_DATA, "Désinstalle un émulateur (option : supprimer aussi ses données, "
                                           "dont les sauvegardes).",
            {"emulator": Param("str", choices=EMU_KEYS),
             "delete_data": Param("bool", required=False, default=False,
                                  description="Supprime aussi ~/.var/app/<id> (sauvegardes incluses)")},
            summary=lambda p: f"Désinstaller {p.get('emulator')}"
                              + (" et SUPPRIMER ses données/sauvegardes" if p.get("delete_data") else ""))
    def uninstall(self, emulator, delete_data=False):
        e = self._emu(emulator)
        if not e["flatpak_id"]:
            raise Unavailable(f"{e['name']} n'est pas géré par NEXUS")
        app_id = e["flatpak_id"]
        installed = self._installed_ids()
        if installed is None:
            raise Unavailable("Flatpak est absent de ce système", details={"binary": "flatpak"})
        if app_id not in installed:
            raise NotFound(f"{e['name']} n'est pas installé")

        def job(task):
            task.update(0.1, f"Désinstallation de {e['name']}")
            self.flatpak.uninstall(app_id, delete_data=bool(delete_data))
            self.ctx.events.publish("emulation.uninstalled", {"emulator": emulator, "app_id": app_id})
            return {"emulator": emulator, "app_id": app_id, "uninstalled": True,
                    "data_deleted": bool(delete_data)}

        return self.ctx.tasks.submit(f"Désinstallation de {e['name']}", job)

    # ------------------------------------------------------------------ library

    @action("scan", Risk.READ, "Analyse le dossier de ROMs (fichiers fournis par l'utilisateur).")
    def scan(self):
        rd = self.rom_dir()
        systems = {s: scan_system(rd, s) for s in SYSTEMS} if rd.is_dir() else {s: [] for s in SYSTEMS}
        idx = {"rom_dir": str(rd), "scanned_at": time.time(), "systems": systems}
        with self._lock:
            self._index = idx
            save_json(self._index_path, idx)
        self.ctx.events.publish("emulation.scanned", {"total": sum(len(v) for v in systems.values())})
        return {"rom_dir": str(rd), "rom_dir_exists": rd.is_dir(),
                "counts": {s: len(v) for s, v in systems.items() if v},
                "total": sum(len(v) for v in systems.values())}

    @action("roms", Risk.READ, "Liste les ROMs de la bibliothèque (optionnellement pour une plateforme).",
            {"system": Param("str", required=False, choices=SYSTEM_KEYS)})
    def roms(self, system=None):
        idx = self._load_index()
        if idx is None or idx.get("rom_dir") != str(self.rom_dir()):
            self.scan()
            idx = self._index
        roms = [r for s, lst in idx["systems"].items() if system in (None, s) for r in lst]
        for r in roms:
            r["cover_url"] = f"/api/m/emulation/{r['cover']}" if r.get("cover") else None
        return roms

    @action("prepare_folders", Risk.SETTINGS, "Crée l'arborescence ~/Games/ROMs/<plateforme>/ (dossiers vides).")
    def prepare_folders(self):
        rd = self.rom_dir()
        made = []
        for s, spec in SYSTEMS.items():
            if spec["kind"] == "none":
                continue
            d = rd / s / "media" / "covers"
            if not (rd / s).exists():
                made.append(s)
            d.mkdir(parents=True, exist_ok=True)
        return {"rom_dir": str(rd), "created": made}

    # ------------------------------------------------------------------ launch

    def _find_core(self, core: str) -> str | None:
        """Path of a libretro core usable *inside* the RetroArch sandbox, or None."""
        fname = f"{core}_libretro.so"
        user = self.home / ".var/app/org.libretro.RetroArch/config/retroarch/cores" / fname
        if user.is_file():
            return str(user)  # ~/.var/app/<id> is mounted at the same path in the sandbox
        for root in (self.flatpak_system_dir, self.home / ".local/share/flatpak"):
            if (root / "app/org.libretro.RetroArch/current/active/files/lib/libretro" / fname).is_file():
                return f"/app/lib/libretro/{fname}"
        return None

    def _resolve_rom(self, rom_id: str) -> tuple[str, Path]:
        if not isinstance(rom_id, str) or "/" not in rom_id:
            raise InvalidParams("Identifiant de ROM invalide (attendu « <plateforme>/<chemin> »)")
        system, rel = rom_id.split("/", 1)
        if system not in SYSTEMS:
            raise NotFound(f"Plateforme inconnue : {system}")
        if "\x00" in rel or not rel:
            raise InvalidParams("Identifiant de ROM invalide")
        path = safe_join(self.rom_dir() / system, rel)  # refuses ../ and symlink escapes
        spec = SYSTEMS[system]
        if spec["kind"] == "none":
            raise Unavailable(f"{spec['name']} : {spec['note']}")
        if spec["kind"] == "file":
            if not path.is_file():
                raise NotFound("ROM introuvable")
            if path.suffix.lower() not in spec["extensions"]:
                raise InvalidParams(f"Extension non prise en charge pour {spec['name']}")
        else:
            if not path.is_dir() or not _detect_dir_game(system, path):
                raise NotFound("Dossier de jeu introuvable ou invalide (PARAM.SFO manquant)")
        return system, path

    def build_launch_argv(self, emulator: str, system: str, path: Path) -> tuple[list[str], bool]:
        e = self._emu(emulator)
        if system not in e["systems"]:
            raise InvalidParams(f"{e['name']} ne gère pas la plateforme {system}")
        if not e["flatpak_id"]:
            raise Unavailable(f"{e['name']} : {e['compatibility']['note']}")
        builder = e["rom_args"]
        if builder is None:
            return self.flatpak.run_argv(e["flatpak_id"]), False
        core = None
        if emulator == "retroarch":
            cores = RETROARCH_CORES.get(system, [])
            core = next((c for c in map(self._find_core, cores) if c), None)
            if not core:
                raise Unavailable(
                    f"Aucun cœur RetroArch trouvé pour {SYSTEMS[system]['name']} "
                    f"(attendu : {', '.join(c + '_libretro.so' for c in cores)}). Téléchargez-le depuis "
                    "RetroArch : Menu principal > Mise à jour en ligne > Téléchargeur de cœurs.",
                    details={"cores": cores})
        args = builder(path, core)
        return self.flatpak.run_argv(e["flatpak_id"], *args), True

    @action("launch", Risk.CONTROL, "Lance une ROM de la bibliothèque avec l'émulateur adapté.",
            {"rom_id": Param("str", description="Identifiant renvoyé par emulation.roms"),
             "emulator": Param("str", required=False, choices=EMU_KEYS,
                               description="Forcer un émulateur (sinon : recommandé installé)")},
            summary=lambda p: f"Lancer {p.get('rom_id')}")
    def launch(self, rom_id, emulator=None):
        system, path = self._resolve_rom(rom_id)
        installed = self._installed_ids()
        if installed is None:
            raise Unavailable("Flatpak est absent de ce système", details={"binary": "flatpak"})
        cands = [emulator] if emulator else SYSTEMS[system]["emulators"]
        chosen = next((c for c in cands if EMULATORS[c]["flatpak_id"] in installed), None)
        if not chosen:
            rec = emulator or next((c for c in cands if EMULATORS[c]["installable"]), None)
            raise Unavailable(
                f"Aucun émulateur installé pour {SYSTEMS[system]['name']}"
                + (f" : installez {EMULATORS[rec]['name']} (emulation.install)." if rec else "."),
                details={"system": system, "recommended": rec})
        argv, rom_loaded = self.build_launch_argv(chosen, system, path)
        pid = self.ctx.runner.spawn(argv)
        self.ctx.events.publish("emulation.launched", {"rom_id": rom_id, "emulator": chosen, "pid": pid})
        out = {"rom_id": rom_id, "emulator": chosen, "pid": pid, "argv": argv, "rom_loaded": rom_loaded}
        if not rom_loaded:
            out["message"] = (f"{EMULATORS[chosen]['name']} ouvert : sélectionnez le jeu dans son interface "
                              "(lancement direct non pris en charge).")
        return out

    # ------------------------------------------------------------------ recommend

    def hardware(self) -> dict:
        return {"cpu": hardware.read_cpu(self.cpuinfo_path),
                "ram_gb": hardware.read_mem_gb(self.meminfo_path),
                "vulkan": hardware.read_vulkan(self.ctx.runner)}

    @action("recommend", Risk.READ, "Recommande un émulateur pour une plateforme selon le matériel réel "
                                    "(AVX2, cœurs, RAM, Vulkan) avec avertissements.",
            {"system": Param("str", choices=SYSTEM_KEYS)})
    def recommend(self, system):
        spec = SYSTEMS[system]
        hw = self.hardware()
        installed = self._installed_ids()
        emus = spec["emulators"]
        rec = next((e for e in emus if EMULATORS[e]["installable"]), None)
        warnings: list[str] = []
        if rec is None:
            warnings.append(f"{spec['name']} : {spec['note']}")
        else:
            e = EMULATORS[rec]
            warnings += hardware.requirement_warnings(e["name"], e["requirements"], hw["cpu"], hw["ram_gb"],
                                                      hw["vulkan"])
            if not hw["cpu"]["known"]:
                warnings.append("Informations CPU indisponibles : recommandation non adaptée au matériel.")
            if e["availability"] != "flathub":
                warnings.append(f"Disponibilité Flathub de {e['name']} à vérifier.")
        bios = [b for k in emus for b in EMULATORS[k]["bios"] if b["system"] == system]
        return {
            "system": system, "name": spec["name"], "support": spec["support"], "note": spec["note"],
            "recommended": self._emu_view(rec, installed) if rec else None,
            "retroarch_cores": RETROARCH_CORES.get(system) if rec == "retroarch" else None,
            "alternatives": [self._emu_view(k, installed) for k in emus if k != rec],
            "bios": bios,
            "warnings": warnings,
            "hardware": {"cpu_model": hw["cpu"].get("model"), "threads": hw["cpu"].get("threads"),
                         "avx2": hw["cpu"].get("avx2"), "ram_gb": hw["ram_gb"],
                         "vulkan": hw["vulkan"].get("available"),
                         "vulkan_devices": hw["vulkan"].get("devices"),
                         "vulkan_api": hw["vulkan"].get("api_version")},
        }
