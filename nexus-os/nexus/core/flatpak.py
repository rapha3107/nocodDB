"""Shared Flatpak operations (system installation, Flathub only).

Store, Emulation, Music and Browser all install software through this helper so
the provenance rules live in exactly one place:
  * only remotes in TRUSTED_FLATPAK_REMOTES (flathub);
  * app ids validated against a strict regex (no flag/argument injection);
  * `--` is never needed because ids cannot start with '-';
  * installs run as the desktop user through flatpak's own polkit policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..security.verify import check_remote, validate_flatpak_id
from .errors import CommandFailed


@dataclass
class InstalledApp:
    app_id: str
    name: str
    version: str
    origin: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Flatpak:
    def __init__(self, runner):
        self.runner = runner

    def available(self) -> bool:
        return self.runner.available("flatpak")

    def installed(self) -> list[InstalledApp]:
        r = self.runner.run(["flatpak", "list", "--app", "--columns=application,name,version,origin"], timeout=60)
        apps = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[0] and parts[0] != "Application ID":
                apps.append(InstalledApp(parts[0], parts[1], parts[2], parts[3]))
        return apps

    def is_installed(self, app_id: str) -> bool:
        validate_flatpak_id(app_id)
        return any(a.app_id == app_id for a in self.installed())

    def install(self, app_id: str, remote: str = "flathub", timeout: float = 3600) -> None:
        validate_flatpak_id(app_id)
        check_remote(remote)
        r = self.runner.run(["flatpak", "install", "--system", "-y", "--noninteractive", remote, app_id],
                            timeout=timeout)
        if not r.ok:
            raise CommandFailed(f"Installation de {app_id} échouée",
                                details={"stderr": r.stderr[-2000:], "app_id": app_id})

    def uninstall(self, app_id: str, delete_data: bool = False) -> None:
        validate_flatpak_id(app_id)
        argv = ["flatpak", "uninstall", "--system", "-y", "--noninteractive", app_id]
        if delete_data:
            argv.insert(-1, "--delete-data")
        r = self.runner.run(argv, timeout=600)
        if not r.ok:
            raise CommandFailed(f"Désinstallation de {app_id} échouée", details={"stderr": r.stderr[-2000:]})

    def updates(self) -> list[str]:
        r = self.runner.run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application"], timeout=120)
        return [l.strip() for l in r.stdout.splitlines() if l.strip() and l.strip() != "Application ID"]

    def update_all(self, timeout: float = 7200) -> None:
        r = self.runner.run(["flatpak", "update", "--system", "-y", "--noninteractive"], timeout=timeout)
        if not r.ok:
            raise CommandFailed("Mise à jour Flatpak échouée", details={"stderr": r.stderr[-2000:]})

    def run_argv(self, app_id: str, *args: str) -> list[str]:
        """argv to pass to runner.spawn(). Extra args must be validated by the caller."""
        validate_flatpak_id(app_id)
        return ["flatpak", "run", app_id, *args]
