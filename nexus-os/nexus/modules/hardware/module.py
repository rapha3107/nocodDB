"""NEXUS hardware module: CPU/GPU/RAM info, controllers (DualSense), power profiles, MangoHud.

Every system path is injectable (`sys_root`, `proc_root`, `disk_path`) so tests run on
a fake sysfs/procfs tree. Defaults can also be overridden with the settings keys
`hardware.sys_root` / `hardware.proc_root` (development only).

Nothing here needs root. Writing the DualSense light bar requires a udev rule that
makes `/sys/class/leds/input*:rgb:indicator/{multi_intensity,brightness}` writable by
the desktop user (see `UDEV_LIGHTBAR_RULE`, installed by the Build agent).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ...core.errors import CommandFailed, InvalidParams, NexusError, NotFound, PermissionDenied, Unavailable
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk

# (vendor, product) -> model. Only ids known with certainty; vendor-only entries use None.
KNOWN_CONTROLLERS: dict[tuple[str, str | None], dict] = {
    ("054c", "0ce6"): {"model": "DualSense", "brand": "Sony", "family": "dualsense"},
    ("054c", "0df2"): {"model": "DualSense Edge", "brand": "Sony", "family": "dualsense"},
    ("054c", "05c4"): {"model": "DualShock 4", "brand": "Sony", "family": "dualshock4"},
    ("054c", "09cc"): {"model": "DualShock 4 (v2)", "brand": "Sony", "family": "dualshock4"},
    ("045e", "028e"): {"model": "Manette Xbox 360", "brand": "Microsoft", "family": "xbox"},
    ("045e", "02ea"): {"model": "Manette Xbox One S", "brand": "Microsoft", "family": "xbox"},
    ("045e", "0b12"): {"model": "Manette Xbox Series X|S", "brand": "Microsoft", "family": "xbox"},
    ("057e", "2009"): {"model": "Manette Switch Pro", "brand": "Nintendo", "family": "switch"},
    ("28de", None): {"model": "Périphérique Valve (Steam Controller / Steam Deck)", "brand": "Valve",
                     "family": "valve"},
}
DUALSENSE_IDS = {("054c", "0ce6"), ("054c", "0df2")}
BUS_TYPES = {"0003": "usb", "0005": "bluetooth"}

GPU_VENDORS = {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}

# powerprofilesctl profile names (power-profiles-daemon / tuned-ppd) — validated strictly.
POWER_PROFILES = ["power-saver", "balanced", "performance"]

LED_NAME_RE = re.compile(r"^input\d{1,6}:rgb:indicator$")
PPC_LINE_RE = re.compile(r"^(\*)?\s*(power-saver|balanced|performance):\s*$")

UDEV_LIGHTBAR_RULE = (
    "# /etc/udev/rules.d/70-nexus-dualsense-lightbar.rules\n"
    "# Rend la barre lumineuse des DualSense / DualSense Edge (hid-playstation) modifiable\n"
    "# par l'utilisateur de la session (couleur uniquement, aucun autre attribut).\n"
    'ACTION=="add", SUBSYSTEM=="leds", KERNEL=="input*:rgb:indicator", '
    'KERNELS=="*:054C:0CE6.*|*:054C:0DF2.*", '
    'RUN+="/usr/bin/chmod 0666 /sys%p/multi_intensity /sys%p/brightness"\n'
)

PAIRING_STEPS = [
    "Ouvrez les paramètres Bluetooth (ou laissez NEXUS afficher l'état de l'adaptateur ci-dessous) "
    "et vérifiez que le Bluetooth est activé.",
    "Manette éteinte, maintenez simultanément les boutons PS et Create (à gauche du pavé tactile) "
    "pendant environ 3 à 5 secondes.",
    "Relâchez quand la barre lumineuse clignote rapidement par paires : la manette est en mode appairage.",
    "Dans la liste des appareils Bluetooth, choisissez « DualSense Wireless Controller » "
    "(ou « DualSense Edge Wireless Controller ») et confirmez l'appairage.",
    "La barre lumineuse devient fixe une fois connectée. Par la suite, un simple appui sur PS "
    "reconnecte la manette.",
    "En cas d'échec : branchez la manette en USB-C (fonctionne immédiatement en filaire), "
    "ou réinitialisez-la avec le petit bouton au dos (trombone, 5 s) puis recommencez.",
]


def _read(p: Path, default: str | None = None) -> str | None:
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return default


def _hex4(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower().removeprefix("0x")
    return v.zfill(4)[-4:] if re.fullmatch(r"[0-9a-f]{1,8}", v) else None


def identify(vendor: str | None, product: str | None) -> dict | None:
    if not vendor:
        return None
    return KNOWN_CONTROLLERS.get((vendor, product)) or KNOWN_CONTROLLERS.get((vendor, None))


def parse_proc_input_devices(text: str) -> list[dict]:
    """Parse /proc/bus/input/devices into dicts (bus, vendor, product, name, uniq, sysfs, handlers)."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        d: dict = {}
        for line in block.splitlines():
            if line.startswith("I:"):
                for k, v in re.findall(r"(\w+)=([0-9a-fA-F]+)", line):
                    d[k.lower()] = v.lower()
            elif line.startswith("N: Name="):
                d["name"] = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("U: Uniq="):
                d["uniq"] = line[len("U: Uniq="):].strip()
            elif line.startswith("S: Sysfs="):
                d["sysfs"] = line[len("S: Sysfs="):].strip()
            elif line.startswith("H: Handlers="):
                d["handlers"] = line[len("H: Handlers="):].split()
        if d:
            out.append(d)
    return out


def parse_powerprofiles_list(text: str) -> tuple[list[str], str | None]:
    profiles, active = [], None
    for line in text.splitlines():
        m = PPC_LINE_RE.match(line)
        if m:
            profiles.append(m.group(2))
            if m.group(1):
                active = m.group(2)
    return profiles, active


class HardwareModule(NexusModule):
    name = "hardware"
    title = "Matériel"
    description = "Informations matériel, manettes (DualSense), profils d'énergie et MangoHud"

    def __init__(self, ctx, *, sys_root: str | Path | None = None, proc_root: str | Path | None = None,
                 disk_path: str | Path | None = None):
        super().__init__(ctx)
        self.sys = Path(sys_root or ctx.settings.get("hardware.sys_root", "/sys"))
        self.proc = Path(proc_root or ctx.settings.get("hardware.proc_root", "/proc"))
        self.disk_path = Path(disk_path or ctx.paths.home)

    # ------------------------------------------------------------------ info
    def _cpu(self) -> dict:
        text = _read(self.proc / "cpuinfo", "") or ""
        model, threads, flags = None, 0, set()
        cores_per_pkg: dict[str, str] = {}
        cur_phys = None
        for line in text.splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "processor":
                threads += 1
            elif k == "model name" and model is None:
                model = v
            elif k == "flags" and not flags:
                flags = set(v.split())
            elif k == "physical id":
                cur_phys = v
            elif k == "cpu cores" and cur_phys is not None:
                cores_per_pkg[cur_phys] = v
        cores = sum(int(c) for c in cores_per_pkg.values() if c.isdigit()) or None
        return {"model": model, "threads": threads or os.cpu_count(), "cores": cores,
                "avx2": "avx2" in flags if flags else None}

    def _memory(self) -> dict:
        vals = {}
        for line in (_read(self.proc / "meminfo", "") or "").splitlines():
            m = re.match(r"^(MemTotal|MemAvailable|SwapTotal):\s+(\d+)\s*kB", line)
            if m:
                vals[m.group(1)] = int(m.group(2)) * 1024
        return {"total_bytes": vals.get("MemTotal"), "available_bytes": vals.get("MemAvailable"),
                "swap_total_bytes": vals.get("SwapTotal")}

    def _lspci_names(self) -> dict[tuple[str, str], str]:
        if not self.ctx.runner.available("lspci"):
            return {}
        try:
            r = self.ctx.runner.run(["lspci", "-nn"], timeout=15)
        except NexusError:
            return {}
        names = {}
        for line in r.stdout.splitlines():
            m = re.match(r"^\S+\s+[^\[]*\[03[0-9a-f]{2}\]:\s*(.*?)\s*\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
            if m:
                names[(m.group(2), m.group(3))] = m.group(1)
        return names

    def _gpus(self) -> list[dict]:
        drm = self.sys / "class" / "drm"
        gpus = []
        if not drm.is_dir():
            return gpus
        names = None
        for card in sorted(drm.iterdir()):
            if not re.fullmatch(r"card\d+", card.name):
                continue
            vendor = (_read(card / "device" / "vendor") or "").lower()
            device = (_read(card / "device" / "device") or "").lower()
            if not vendor:
                continue
            if names is None:
                names = self._lspci_names()
            drv = card / "device" / "driver"
            gpus.append({
                "card": card.name,
                "vendor_id": vendor,
                "device_id": device or None,
                "vendor": GPU_VENDORS.get(vendor, "inconnu"),
                "name": names.get((_hex4(vendor), _hex4(device))) if names else None,
                "driver": os.path.basename(os.readlink(drv)) if drv.is_symlink() else None,
            })
        return gpus

    def _vulkan(self) -> dict:
        if not self.ctx.runner.available("vulkaninfo"):
            return {"available": False, "reason": "vulkaninfo absent (paquet vulkan-tools)"}
        try:
            r = self.ctx.runner.run(["vulkaninfo", "--summary"], timeout=30)
        except NexusError as e:
            return {"available": False, "reason": e.message}
        if not r.ok:
            return {"available": False, "reason": (r.stderr or "vulkaninfo a échoué").strip()[-300:]}
        devices = [{"name": m} for m in re.findall(r"deviceName\s*=\s*(.+)", r.stdout)]
        apis = re.findall(r"apiVersion\s*=\s*(\S+)", r.stdout)
        for d, a in zip(devices, apis):
            d["api_version"] = a
        return {"available": bool(devices), "devices": devices}

    def _disk(self) -> dict:
        try:
            u = shutil.disk_usage(self.disk_path)
        except OSError:
            return {"path": str(self.disk_path), "total_bytes": None, "free_bytes": None}
        return {"path": str(self.disk_path), "total_bytes": u.total, "free_bytes": u.free}

    @action("info", Risk.READ, "Informations matériel : processeur, mémoire, carte graphique, Vulkan, disque")
    def info(self):
        return {"cpu": self._cpu(), "memory": self._memory(), "gpus": self._gpus(),
                "vulkan": self._vulkan(), "disk": self._disk()}

    # ----------------------------------------------------------- controllers
    def _input_devices(self) -> list[dict]:
        devs: dict[str, dict] = {}
        cls = self.sys / "class" / "input"
        if cls.is_dir():
            for d in sorted(cls.iterdir()):
                if not re.fullmatch(r"input\d+", d.name):
                    continue
                handlers = [c.name for c in d.iterdir() if re.fullmatch(r"(js|event)\d+", c.name)] \
                    if d.is_dir() else []
                devs[d.name] = {
                    "input": d.name,
                    "name": _read(d / "name") or _read(d / "device" / "name"),
                    "bus": _hex4(_read(d / "id" / "bustype")),
                    "vendor": _hex4(_read(d / "id" / "vendor")),
                    "product": _hex4(_read(d / "id" / "product")),
                    "uniq": _read(d / "uniq") or None,
                    "handlers": sorted(handlers),
                }
        proc = _read(self.proc / "bus" / "input" / "devices")
        if proc:
            for p in parse_proc_input_devices(proc):
                key = os.path.basename(p.get("sysfs", "")) or f"proc{len(devs)}"
                cur = devs.setdefault(key, {"input": key})
                for k_src, k_dst in (("name", "name"), ("bus", "bus"), ("vendor", "vendor"),
                                     ("product", "product"), ("uniq", "uniq"), ("handlers", "handlers")):
                    v = p.get(k_src)
                    if k_src in ("bus", "vendor", "product"):
                        v = _hex4(v)
                    if v and not cur.get(k_dst):
                        cur[k_dst] = v
        return list(devs.values())

    def _battery(self, uniq: str | None) -> dict | None:
        ps = self.sys / "class" / "power_supply"
        if not uniq or not ps.is_dir():
            return None
        d = ps / f"ps-controller-battery-{uniq.lower()}"
        if not d.is_dir():
            return None
        cap = _read(d / "capacity")
        return {"capacity": int(cap) if cap and cap.isdigit() else None, "status": _read(d / "status")}

    def _controllers(self) -> list[dict]:
        out, seen = [], set()
        for d in self._input_devices():
            handlers = d.get("handlers") or []
            if not any(h.startswith("js") for h in handlers):
                continue  # not a joystick/gamepad (motion sensors, touchpad, keyboards…)
            vid, pid = d.get("vendor"), d.get("product")
            key = (vid, pid, (d.get("uniq") or d["input"]).lower())
            if key in seen:
                continue
            seen.add(key)
            known = identify(vid, pid)
            entry = {
                "id": d["input"],
                "name": d.get("name"),
                "model": known["model"] if known else "Manette générique",
                "brand": known["brand"] if known else None,
                "family": known["family"] if known else "generic",
                "vendor_id": vid,
                "product_id": pid,
                "connection": BUS_TYPES.get(d.get("bus") or "", "inconnue"),
                "battery": None,
                "lightbar": None,
            }
            if (vid, pid) in DUALSENSE_IDS:
                entry["battery"] = self._battery(d.get("uniq"))
                led = self.sys / "class" / "leds" / f"{d['input']}:rgb:indicator"
                entry["lightbar"] = led.name if led.exists() else None
            out.append(entry)
        return out

    @action("controllers", Risk.READ, "Liste les manettes connectées (modèle, USB/Bluetooth, batterie DualSense)")
    def controllers(self):
        return {"controllers": self._controllers()}

    # -------------------------------------------------------------- lightbar
    def _leds_root(self) -> Path:
        return self.sys / "class" / "leds"

    def _validate_led_dir(self, led: Path) -> Path:
        """Refuse anything that is not `<sys>/class/leds/input<N>:rgb:indicator` of a DualSense."""
        root = self._leds_root()
        led = Path(os.path.normpath(led))
        if led.parent != Path(os.path.normpath(root)) or not LED_NAME_RE.match(led.name):
            raise PermissionDenied("Chemin de LED refusé : seules les barres lumineuses DualSense de "
                                   "/sys/class/leds sont modifiables", details={"path": str(led)})
        if not led.is_dir():
            raise NotFound(f"LED introuvable : {led.name}")
        real_sys = self.sys.resolve()
        real_led = led.resolve()
        if real_led != real_sys and real_sys not in real_led.parents:
            raise PermissionDenied("Chemin de LED refusé : la cible sort de sysfs", details={"path": str(led)})
        for attr in ("multi_intensity", "brightness"):
            if (led / attr).is_symlink():
                raise PermissionDenied(f"Attribut de LED refusé (lien symbolique) : {attr}")
        if not self._led_is_dualsense(led):
            raise PermissionDenied("Cette LED n'appartient pas à une DualSense", details={"led": led.name})
        return led

    def _led_is_dualsense(self, led: Path) -> bool:
        uevent = _read(led / "device" / "uevent") or ""
        m = re.search(r"^HID_ID=([0-9A-Fa-f]+):([0-9A-Fa-f]+):([0-9A-Fa-f]+)$", uevent, re.M)
        if m:
            return (_hex4(m.group(2)), _hex4(m.group(3))) in DUALSENSE_IDS
        inp = self.sys / "class" / "input" / led.name.split(":", 1)[0]
        return (_hex4(_read(inp / "id" / "vendor")), _hex4(_read(inp / "id" / "product"))) in DUALSENSE_IDS

    def _write_lightbar(self, led: Path, r: int, g: int, b: int) -> None:
        for name, v in (("r", r), ("g", g), ("b", b)):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
                raise InvalidParams(f"Valeur {name} invalide : entier de 0 à 255 attendu")
        led = self._validate_led_dir(led)
        order = (_read(led / "multi_index") or "red green blue").split()
        comp = {"red": r, "green": g, "blue": b}
        if sorted(order) != ["blue", "green", "red"]:
            raise CommandFailed("Format multi_index inattendu pour la LED", details={"multi_index": order})
        max_b = _read(led / "max_brightness") or "255"
        max_b = max_b if max_b.isdigit() else "255"
        try:
            (led / "multi_intensity").write_text(" ".join(str(comp[c]) for c in order) + "\n")
            (led / "brightness").write_text(max_b + "\n")
        except PermissionError as e:
            raise PermissionDenied(
                "Accès refusé à la barre lumineuse : la règle udev NEXUS (70-nexus-dualsense-lightbar.rules) "
                "n'est pas installée ou la manette a été branchée avant son installation",
                details={"led": led.name, "udev_rule": UDEV_LIGHTBAR_RULE}) from e
        except OSError as e:
            raise CommandFailed(f"Écriture de la LED impossible : {e.strerror}", details={"led": led.name}) from e

    @action("controller_lightbar", Risk.CONTROL, "Change la couleur de la barre lumineuse des DualSense",
            {"r": Param("int", description="Rouge 0-255"),
             "g": Param("int", description="Vert 0-255"),
             "b": Param("int", description="Bleu 0-255"),
             "controller": Param("str", required=False,
                                 description="LED ciblée (ex. input12:rgb:indicator) ; toutes par défaut")})
    def controller_lightbar(self, r, g, b, controller=None):
        for name, v in (("r", r), ("g", g), ("b", b)):
            if not 0 <= v <= 255:
                raise InvalidParams(f"Valeur {name} invalide : entier de 0 à 255 attendu")
        root = self._leds_root()
        if controller is not None:
            if not LED_NAME_RE.match(controller):
                raise InvalidParams("Identifiant de LED invalide")
            leds = [root / controller]
        else:
            leds = [p for p in sorted(root.iterdir()) if LED_NAME_RE.match(p.name)
                    and self._led_is_dualsense(p)] if root.is_dir() else []
        if not leds:
            raise Unavailable("Aucune DualSense avec barre lumineuse détectée (pilote hid-playstation)")
        for led in leds:
            self._write_lightbar(led, r, g, b)
        return {"ok": True, "leds": [l.name for l in leds], "color": {"r": r, "g": g, "b": b}}

    # ----------------------------------------------------------------- setup
    @action("controller_setup", Risk.READ, "Guide d'appairage Bluetooth d'une DualSense et état de l'adaptateur")
    def controller_setup(self):
        bt: dict = {"available": False}
        if self.ctx.runner.available("bluetoothctl"):
            try:
                r = self.ctx.runner.run(["bluetoothctl", "show"], timeout=10)
                info = {}
                for line in r.stdout.splitlines():
                    line = line.strip()
                    m = re.match(r"^Controller\s+([0-9A-Fa-f:]{17})", line)
                    if m:
                        info["address"] = m.group(1)
                    for key in ("Powered", "Discoverable", "Pairable"):
                        if line.startswith(key + ":"):
                            info[key.lower()] = line.split(":", 1)[1].strip() == "yes"
                bt = {"available": "address" in info, **info}
                if not bt["available"]:
                    bt["reason"] = "Aucun adaptateur Bluetooth détecté"
            except NexusError as e:
                bt = {"available": False, "reason": e.message}
        else:
            bt["reason"] = "bluetoothctl absent"
        return {
            "steps": PAIRING_STEPS,
            "bluetooth": bt,
            "automatic_pairing": False,
            "notes": "L'appairage automatique n'est pas proposé dans cette version (non testé sur matériel) : "
                     "utilisez les paramètres Bluetooth du système. En USB, la DualSense fonctionne sans appairage.",
            "controllers": self._controllers(),
        }

    # ---------------------------------------------------------- performance
    def _ppc(self, *args: str):
        if not self.ctx.runner.available("powerprofilesctl"):
            raise Unavailable("powerprofilesctl absent (power-profiles-daemon / tuned-ppd)",
                              details={"binary": "powerprofilesctl"})
        return self.ctx.runner.run(["powerprofilesctl", *args], timeout=15)

    @action("performance_profiles", Risk.READ, "Liste les profils d'énergie (économie, équilibré, performance)")
    def performance_profiles(self):
        r = self._ppc("list")
        if not r.ok:
            raise CommandFailed("powerprofilesctl list a échoué", details={"stderr": r.stderr[-500:]})
        profiles, active = parse_powerprofiles_list(r.stdout)
        if active is None:
            g = self._ppc("get")
            active = g.stdout.strip() if g.ok and g.stdout.strip() in POWER_PROFILES else None
        return {"profiles": profiles, "active": active}

    @action("set_performance_profile", Risk.SETTINGS, "Active un profil d'énergie",
            {"profile": Param("str", choices=POWER_PROFILES, description="power-saver | balanced | performance")},
            summary=lambda p: f"Activer le profil d'énergie « {p.get('profile')} »")
    def set_performance_profile(self, profile):
        if profile not in POWER_PROFILES:  # defence in depth (choices already enforced)
            raise InvalidParams("Profil inconnu")
        r = self._ppc("set", profile)
        if not r.ok:
            raise CommandFailed(f"Impossible d'activer le profil {profile}",
                                details={"stderr": r.stderr[-500:]})
        return {"ok": True, "profile": profile}

    # --------------------------------------------------------------- overlay
    @action("overlay", Risk.SETTINGS, "Active/désactive l'overlay MangoHud pour les prochains lancements de jeux",
            {"enabled": Param("bool")},
            summary=lambda p: ("Activer" if p.get("enabled") else "Désactiver") + " l'overlay MangoHud")
    def overlay(self, enabled):
        self.ctx.settings.set("hardware.mangohud", bool(enabled))
        return {"ok": True, "enabled": bool(enabled),
                "mangohud_installed": self.ctx.runner.available("mangohud"),
                "notes": "Réglage enregistré (hardware.mangohud). Il s'applique aux jeux lancés ensuite via les "
                         "options de lancement (MANGOHUD=1 / mangohud %command%) gérées par le module games ; "
                         "aucun effet global ni sur les jeux déjà lancés."}

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        try:
            ctrls = self._controllers()
        except OSError:
            ctrls = []
        profile, reason = None, None
        if self.ctx.runner.available("powerprofilesctl"):
            try:
                r = self.ctx.runner.run(["powerprofilesctl", "get"], timeout=10)
                profile = r.stdout.strip() if r.ok and r.stdout.strip() in POWER_PROFILES else None
            except NexusError as e:
                reason = e.message
        else:
            reason = "powerprofilesctl absent"
        return {"ok": True, "controllers": len(ctrls),
                "controller_models": [c["model"] for c in ctrls],
                "performance_profile": profile,
                **({"performance_profile_reason": reason} if reason else {}),
                "mangohud": bool(self.ctx.settings.get("hardware.mangohud", False))}
