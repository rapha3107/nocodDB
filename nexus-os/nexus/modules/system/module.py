"""NEXUS system module: atomic OS updates (bootc / rpm-ostree), rollback, power, logs, diagnostics.

Design notes
  * nexusd runs as the desktop user, never root. `rpm-ostree` talks to the
    rpm-ostreed daemon over D-Bus and is authorised by polkit (on Fedora Atomic
    images the `wheel` group may upgrade/rollback); `bootc` needs root for
    write operations. Every operation therefore tries the configured order
    (default: bootc then rpm-ostree) and falls back when the first tool is
    missing or refuses.
  * Updates are staged, never applied live, and NEXUS never reboots on its own:
    `update_apply` only downloads/stages and tells the user a reboot applies it.
  * `power{action}` keeps one entry point for the UI/AI but the risk depends on
    the action: `suspend` is CONTROL (non destructive, state kept in RAM, resumed
    with a button press) while shutdown/reboot/logout end the running game and
    any unsaved progress, so they are delegated to `power_system` (SYSTEM →
    explicit human confirmation, bound to the exact action).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from collections import deque
from pathlib import Path

from ...core.errors import CommandFailed, InvalidParams, NexusError, NotFound, Unavailable
from ...core.flatpak import Flatpak
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk
from .parse import parse_bootc_status, parse_rpm_ostree_status, parse_upgrade_check

try:
    from ... import __version__ as NEXUS_VERSION
except Exception:  # pragma: no cover
    NEXUS_VERSION = "inconnue"

BACKENDS = ("bootc", "rpm-ostree")
POWER_ACTIONS = ["shutdown", "reboot", "suspend", "logout"]
POWER_LABELS = {"shutdown": "Éteindre la machine", "reboot": "Redémarrer la machine",
                "logout": "Fermer la session", "suspend": "Mettre en veille"}

UPGRADE_TIMEOUT = 3600
DEFAULT_IMAGE_INFO = Path(os.environ.get("NEXUS_IMAGE_INFO", "/usr/share/nexus/image-info.json"))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _tail_file(path: Path, n: int) -> list[str]:
    try:
        with path.open("r", errors="replace") as f:
            return [l.rstrip("\n") for l in deque(f, maxlen=n)]
    except OSError:
        return []


def _os_release(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


class SystemModule(NexusModule):
    name = "system"
    title = "Système"
    description = "Mises à jour atomiques, retour arrière, alimentation, journaux et diagnostics."

    def __init__(self, ctx, *, image_info_path: Path | None = None, os_release_path: Path = Path("/etc/os-release"),
                 base_image_info_path: Path = Path("/usr/share/ublue-os/image-info.json"),
                 disk_paths: list[Path] | None = None, session_id: str | None = None):
        super().__init__(ctx)
        self.image_info_path = image_info_path or DEFAULT_IMAGE_INFO
        self.base_image_info_path = base_image_info_path
        self.os_release_path = os_release_path
        self.disk_paths = disk_paths
        self.session_id = session_id if session_id is not None else os.environ.get("XDG_SESSION_ID", "")

    # ---- helpers -------------------------------------------------------------
    def _order(self) -> list[str]:
        pref = self.ctx.settings.get("system.update_backend", "auto")
        if pref in BACKENDS:
            return [pref]
        return list(BACKENDS)

    def _backends(self) -> list[str]:
        return [b for b in self._order() if self.ctx.runner.available(b)]

    def _run_first(self, argv_for: dict[str, list[str]], *, timeout: float = 120,
                   accept=lambda b, r: r.ok) -> tuple[str, object, list[dict]]:
        """Run the operation with each available backend until one is accepted."""
        tried = []
        backends = self._backends()
        if not backends:
            raise Unavailable("Ni bootc ni rpm-ostree ne sont disponibles : ce système n'est pas une image atomique.",
                              details={"binaries": list(BACKENDS)})
        last = None
        for b in backends:
            r = self.ctx.runner.run(argv_for[b], timeout=timeout)
            if accept(b, r):
                return b, r, tried
            tried.append({"backend": b, "returncode": r.returncode, "stderr": (r.stderr or "")[-1000:]})
            last = r
        raise CommandFailed("La commande système a échoué avec tous les outils disponibles.",
                            details={"tried": tried, "stderr": (last.stderr or "")[-2000:] if last else ""})

    def _deployments(self) -> dict:
        argv = {"bootc": ["bootc", "status", "--format=json"], "rpm-ostree": ["rpm-ostree", "status", "--json"]}
        parsers = {"bootc": parse_bootc_status, "rpm-ostree": parse_rpm_ostree_status}
        parsed: dict = {}

        def accept(b, r):
            if not r.ok:
                return False
            try:
                parsed[b] = parsers[b](r.stdout)
                return True
            except ValueError:
                return False

        b, _, tried = self._run_first(argv, timeout=60, accept=accept)
        out = parsed[b]
        if tried:
            out["fallback_from"] = tried
        return out

    def _nexus_info(self) -> dict:
        info = _read_json(self.image_info_path) or {}
        return {"version": NEXUS_VERSION, "image": info or None}

    # ---- module health (cheap: no command executed) ---------------------------
    def status(self) -> dict:
        avail = {b: self.ctx.runner.available(b) for b in BACKENDS}
        ok = any(avail.values())
        res = {"ok": ok, "nexus_version": NEXUS_VERSION, "backends": avail,
               "flatpak": self.ctx.runner.available("flatpak"),
               "systemctl": self.ctx.runner.available("systemctl")}
        if not ok:
            res["message"] = "bootc et rpm-ostree absents : mises à jour système indisponibles."
        return res

    # ---- actions ----------------------------------------------------------------
    @action("status", Risk.READ, "État du système : image démarrée, mise à jour en attente, déploiement de secours, version NEXUS.")
    def status_action(self):
        d = self._deployments()
        booted = d.get("booted") or {}
        return {
            "backend": d.get("backend"),
            "booted": d.get("booted"),
            "staged": d.get("staged"),
            "rollback": d.get("rollback"),
            "tracking": d.get("tracking"),
            "cached_update": d.get("cached_update"),
            "rollback_queued": d.get("rollback_queued", False),
            "os_version": booted.get("version"),
            "reboot_required": bool(d.get("staged")) or bool(d.get("rollback_queued")),
            "nexus": self._nexus_info(),
            **({"fallback_from": d["fallback_from"]} if d.get("fallback_from") else {}),
        }

    @action("update_check", Risk.READ, "Vérifie si une mise à jour du système (image) et des applications Flatpak est disponible, sans rien installer.")
    def update_check(self):
        out: dict = {"checked_at": time.time()}
        argv = {"bootc": ["bootc", "upgrade", "--check"], "rpm-ostree": ["rpm-ostree", "upgrade", "--check"]}
        parsed: dict = {}

        def accept(b, r):
            try:
                parsed[b] = parse_upgrade_check(b, r.returncode, r.stdout, r.stderr)
                return True
            except RuntimeError:
                return False

        try:
            b, _, tried = self._run_first(argv, timeout=300, accept=accept)
            out["system"] = {"backend": b, **parsed[b], "error": None}
            if tried:
                out["system"]["fallback_from"] = tried
        except NexusError as e:
            out["system"] = {"available": None, "error": e.to_dict()}
        fp = Flatpak(self.ctx.runner)
        if fp.available():
            try:
                apps = fp.updates()
                out["flatpak"] = {"available": bool(apps), "apps": apps, "count": len(apps), "error": None}
            except NexusError as e:
                out["flatpak"] = {"available": None, "apps": [], "count": 0, "error": e.to_dict()}
        else:
            out["flatpak"] = {"available": None, "apps": [], "count": 0,
                              "error": {"error": "unavailable", "message": "Flatpak absent du système"}}
        out["any_available"] = bool(out["system"].get("available")) or bool(out["flatpak"].get("available"))
        return out

    @action("update_apply", Risk.SYSTEM,
            "Télécharge et prépare la mise à jour du système. Elle sera appliquée au prochain redémarrage ; "
            "l'ancienne version reste disponible pour un retour arrière. Ne redémarre jamais automatiquement.",
            summary=lambda p: "Télécharger et préparer la mise à jour du système (appliquée au prochain redémarrage)")
    def update_apply(self):
        if not self._backends():
            raise Unavailable("Ni bootc ni rpm-ostree ne sont disponibles : mise à jour système impossible.")

        def job(task):
            task.update(0.05, "Téléchargement de la nouvelle image système…")
            argv = {"bootc": ["bootc", "upgrade"], "rpm-ostree": ["rpm-ostree", "upgrade"]}
            b, r, tried = self._run_first(argv, timeout=UPGRADE_TIMEOUT)
            text = f"{r.stdout}\n{r.stderr}"
            low = text.lower()
            no_change = any(s in low for s in ("no update available", "no changes in", "no upgrade available"))
            if no_change:
                task.update(1.0, "Le système est déjà à jour.")
                return {"backend": b, "staged": False, "reboot_required": False,
                        "message": "Le système est déjà à jour.", "output": r.stdout[-4000:]}
            msg = ("Mise à jour prête. Redémarrez pour l'appliquer ; la version actuelle reste "
                   "disponible au démarrage (menu GRUB) et via « Retour arrière ».")
            task.update(1.0, msg)
            res = {"backend": b, "staged": True, "reboot_required": True, "message": msg,
                   "output": r.stdout[-4000:]}
            if tried:
                res["fallback_from"] = tried
            return res

        return self.ctx.tasks.submit("Mise à jour du système", job)

    @action("rollback", Risk.SYSTEM,
            "Revient au déploiement précédent du système (conservé sur le disque). Appliqué au prochain redémarrage.",
            summary=lambda p: "Revenir à la version précédente du système (au prochain redémarrage)")
    def rollback(self):
        try:
            d = self._deployments()
        except NexusError:
            d = None
        if d is not None and not d.get("rollback") and not d.get("rollback_queued"):
            raise NotFound("Aucun déploiement précédent n'est disponible pour un retour arrière.")
        argv = {"bootc": ["bootc", "rollback"], "rpm-ostree": ["rpm-ostree", "rollback"]}
        b, r, tried = self._run_first(argv, timeout=300)
        target = (d or {}).get("rollback")
        res = {"backend": b, "reboot_required": True, "target": target,
               "message": "Retour arrière programmé. Redémarrez pour démarrer sur la version précédente.",
               "output": r.stdout[-4000:]}
        if tried:
            res["fallback_from"] = tried
        return res

    def _power_argv(self, act: str) -> list[str]:
        if act == "shutdown":
            return ["systemctl", "poweroff"]
        if act == "reboot":
            return ["systemctl", "reboot"]
        if act == "suspend":
            return ["systemctl", "suspend"]
        if act == "logout":
            if self.session_id:
                return ["loginctl", "terminate-session", self.session_id]
            return ["loginctl", "terminate-user", str(os.getuid())]
        raise InvalidParams(f"Action d'alimentation inconnue: {act}")

    def _do_power(self, act: str) -> dict:
        argv = self._power_argv(act)
        if not self.ctx.runner.available(argv[0]):
            raise Unavailable(f"{argv[0]} absent : action « {POWER_LABELS[act]} » impossible.")
        r = self.ctx.runner.run(argv, timeout=60)
        if not r.ok:
            raise CommandFailed(f"Échec : {POWER_LABELS[act]}", details={"stderr": (r.stderr or "")[-2000:]})
        return {"action": act, "done": True}

    @action("power", Risk.CONTROL,
            "Alimentation : shutdown (éteindre), reboot (redémarrer), suspend (veille), logout (fermer la session). "
            "Éteindre, redémarrer et fermer la session demandent une confirmation.",
            {"action": Param("str", description="shutdown | reboot | suspend | logout", choices=POWER_ACTIONS)})
    def power(self, action):
        if action == "suspend":
            return self._do_power("suspend")
        # Destructive for the running session: requires a SYSTEM confirmation bound to this exact action.
        return self.ctx.call("system.power_system", {"action": action})

    @action("power_system", Risk.SYSTEM,
            "Éteint, redémarre ou ferme la session (toujours confirmé par l'utilisateur).",
            {"action": Param("str", description="shutdown | reboot | logout", choices=["shutdown", "reboot", "logout"])},
            summary=lambda p: POWER_LABELS.get(p.get("action"), "Alimentation") + " (le jeu en cours sera fermé)")
    def power_system(self, action):
        return self._do_power(action)

    @action("logs", Risk.READ, "Dernières lignes du journal NEXUS (fichier nexus.log et journal systemd utilisateur de nexusd).",
            {"lines": Param("int", required=False, default=200, description="Nombre de lignes (1-2000)")})
    def logs(self, lines=200):
        if lines < 1 or lines > 2000:
            raise InvalidParams("lines doit être compris entre 1 et 2000")
        log_file = self.ctx.paths.data / "nexus.log"
        out = {"file": str(log_file), "nexus_log": _tail_file(log_file, lines), "journal": None, "journal_error": None}
        if self.ctx.runner.available("journalctl"):
            r = self.ctx.runner.run(["journalctl", "--user", "-u", "nexusd", "-n", str(lines), "--no-pager",
                                     "-o", "short-iso"], timeout=30)
            if r.ok:
                out["journal"] = r.stdout.splitlines()[-lines:]
            else:
                out["journal_error"] = (r.stderr or "").strip()[-500:] or f"code {r.returncode}"
        else:
            out["journal_error"] = "journalctl absent"
        return out

    def _disk(self) -> list[dict]:
        paths = self.disk_paths or [Path("/"), self.ctx.paths.home, self.ctx.paths.data]
        seen, out = set(), []
        for p in paths:
            try:
                u = shutil.disk_usage(p)
            except OSError:
                continue
            key = (u.total, u.used)
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": str(p), "total": u.total, "used": u.used, "free": u.free,
                        "percent_used": round(100 * u.used / u.total, 1) if u.total else None})
        return out

    def _last_audit(self) -> dict | None:
        mem = getattr(self.ctx.audit, "memory", None)
        if mem:
            return mem[-1]
        path = getattr(self.ctx.audit, "path", None)
        if path:
            last = _tail_file(Path(path), 1)
            if last:
                try:
                    return json.loads(last[0])
                except ValueError:
                    return None
        return None

    @action("diagnostics", Risk.READ, "Rapport de diagnostic (pour un rapport de bug) : état de chaque module, erreurs de chargement, disque, versions, dernier événement d'audit.")
    def diagnostics(self):
        modules = {}
        reg = self.ctx.registry
        for name, m in (reg.modules.items() if reg else []):
            try:
                modules[name] = {"title": m.title, **m.status()}
            except Exception as e:
                modules[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        osr = _os_release(self.os_release_path)
        try:
            deployments = self.status_action()
        except NexusError as e:
            deployments = {"error": e.to_dict()}
        return {
            "generated_at": time.time(),
            "versions": {
                "nexus": NEXUS_VERSION,
                "nexus_image": _read_json(self.image_info_path),
                "base_image": _read_json(self.base_image_info_path),
                "os": osr.get("PRETTY_NAME"),
                "os_version": osr.get("VERSION_ID"),
                "python": sys.version.split()[0],
                "kernel": platform.release(),
                "machine": platform.machine(),
            },
            "system": deployments,
            "modules": modules,
            "load_errors": dict(reg.load_errors) if reg else {},
            "disk": self._disk(),
            "last_audit": self._last_audit(),
            "recent_log": _tail_file(self.ctx.paths.data / "nexus.log", 50),
        }
