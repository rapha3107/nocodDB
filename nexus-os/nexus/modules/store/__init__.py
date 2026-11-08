"""NEXUS Store — boutique centralisée et sécurisée (Flathub uniquement en v0.1).

Sécurité / provenance
---------------------
* Source de vérité : Flathub (API publique v2 via ctx.net, hôte flathub.org).
* Installations exclusivement via `nexus.core.flatpak.Flatpak` (installation
  système, remote `flathub` seulement ; identifiants validés par regex).
* Intégrité : Flatpak/OSTree vérifie la signature GPG de chaque commit téléchargé
  avec la clé du remote Flathub configurée sur le système. NEXUS n'utilise jamais
  `--no-gpg-verify`, `--from=`, ni de fichier .flatpakref/.flatpakrepo (refusés
  par le validateur du runner).
* `install` (Risk.INSTALL_VERIFIED) n'accepte que les apps existant sur Flathub
  ET vérifiées par Flathub ou présentes dans la liste curatée NEXUS. Sinon :
  PermissionDenied(code="requires_unverified_flow") → passer par
  `install_unverified` (Risk.UNKNOWN_SOURCE : refusé à l'IA, confirmation
  explicite de l'utilisateur obligatoire).
* Sources hors Flathub (autres remotes, .flatpakref, AppImage, RPM) : NON
  supportées en v0.1, même via `install_unverified`.
* Chaque installation/désinstallation est journalisée dans
  `<data>/store_history.json` (app_id, remote, verified, date).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from nexus.core.config import load_json, save_json
from nexus.core.errors import CommandFailed, InvalidParams, NexusError, NotFound, PermissionDenied, Unavailable
from nexus.core.flatpak import Flatpak
from nexus.core.module import NexusModule, Param, action
from nexus.security.permissions import Risk
from nexus.security.verify import FLATPAK_ID_RE, check_remote, validate_flatpak_id

from . import catalog
from .flathub import APP_PAGE, FlathubClient, NotOnFlathub, normalize_appstream

HISTORY_MAX = 1000
_APP_ID = Param("str", True, "Identifiant Flatpak (ex. org.videolan.VLC)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreModule(NexusModule):
    name = "store"
    title = "NEXUS Store"
    description = "Boutique d'applications et de jeux libres, basée sur Flathub (vérifiée)."

    def __init__(self, ctx, flatpak: Flatpak | None = None, clock=time.time):
        super().__init__(ctx)
        self.fp = flatpak or Flatpak(ctx.runner)
        self.flathub = FlathubClient(ctx.net, ctx.paths.cache / "store", clock=clock)
        self.history_path = ctx.paths.data / "store_history.json"
        self._hist_lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def _require_flatpak(self) -> None:
        if not self.fp.available():
            raise Unavailable("Flatpak n'est pas disponible sur ce système.", details={"binary": "flatpak"})

    def _installed_ids(self) -> set[str] | None:
        if not self.fp.available():
            return None
        try:
            return {a.app_id for a in self.fp.installed()}
        except NexusError:
            return None

    def _record(self, action_name: str, app_id: str, remote: str, verified, curated: bool) -> None:
        entry = {"action": action_name, "app_id": app_id, "remote": remote, "verified": verified,
                 "curated": curated, "date": _now_iso()}
        with self._hist_lock:
            hist = load_json(self.history_path, [])
            if not isinstance(hist, list):
                hist = []
            hist.append(entry)
            save_json(self.history_path, hist[-HISTORY_MAX:])

    def _remote_has(self, app_id: str) -> bool | None:
        """Existence via les métadonnées locales du remote flathub (repli hors-ligne)."""
        if not self.fp.available():
            return None
        try:
            r = self.ctx.runner.run(["flatpak", "remote-info", "flathub", app_id], timeout=60)
        except NexusError:
            return None
        return True if r.ok else None

    def _resolve(self, app_id: str) -> dict:
        """exists: True/False/None (inconnu, hors-ligne) ; verified: True/False/None."""
        info = {"app_id": app_id, "exists": None, "verified": None, "name": app_id, "stale": False}
        try:
            a, stale = self.flathub.appstream(app_id)
            info.update(exists=True, name=a.get("name") or app_id, stale=stale)
        except NotOnFlathub:
            info["exists"] = False
            return info
        except NexusError as e:
            if e.code != "network_error":
                raise
            info["exists"] = self._remote_has(app_id)
        try:
            v, vstale = self.flathub.verified(app_id)
            info["verified"] = v
            info["stale"] = info["stale"] or vstale
        except NexusError as e:
            if e.code != "network_error":
                raise
        return info

    def _submit_install(self, app_id: str, remote: str, *, verified, curated: bool, kind: str, title_name: str):
        fp = self.fp

        def job(task):
            task.update(0.05, f"Téléchargement de {title_name} depuis {remote} (signatures GPG vérifiées par Flatpak)")
            fp.install(app_id, remote)
            self._record("install", app_id, remote, verified, curated)
            payload = {"app_id": app_id, "remote": remote, "verified": verified, "curated": curated, "kind": kind}
            self.ctx.events.publish("store.installed", payload)
            task.update(1.0, f"{title_name} installé")
            return payload

        return self.ctx.tasks.submit(f"Installation de {title_name}", job)

    # ---------------------------------------------------------------- actions
    @action("featured", Risk.READ, "Sélection curatée NEXUS par catégorie (lanceurs, outils gaming, multimédia…).")
    def featured(self):
        installed = self._installed_ids()
        cats = []
        for cat in catalog.FEATURED:
            apps = []
            for a in cat["apps"]:
                cached = self.flathub.cache.get(f"verification:{a['app_id']}")
                apps.append({
                    **a,
                    "kind": a.get("kind", "app"),
                    "installable": a.get("installable", True),
                    "source": "flathub",
                    "url": APP_PAGE + a["app_id"],
                    "open_source": False if a.get("proprietary") else None,
                    "verified": (cached or {}).get("data", {}).get("verified") if cached else None,
                    "installed": (a["app_id"] in installed) if installed is not None else None,
                })
            cats.append({"id": cat["id"], "title": cat["title"], "apps": apps})
        return {"categories": cats,
                "note": "Licence, résumé et statut vérifié détaillés : action store.details."}

    @action("search", Risk.READ, "Rechercher une application sur Flathub.",
            {"query": Param("str", True, "Texte recherché")})
    def search(self, query: str):
        q = query.strip()
        if not q or len(q) > 200:
            raise InvalidParams("La recherche doit contenir entre 1 et 200 caractères.")
        try:
            return self.flathub.search(q)
        except NexusError as e:
            if e.code != "network_error":
                raise
            if not self.fp.available():
                raise CommandFailed("Flathub est injoignable et aucune donnée en cache n'est disponible.",
                                    code="network_error") from e
        return {"results": self._flatpak_search(q), "stale": True, "cached_at": None, "source": "flatpak-local"}

    def _flatpak_search(self, q: str) -> list[dict]:
        term = q.lstrip("-").strip()
        if not term:
            return []
        r = self.ctx.runner.run(["flatpak", "search", "--columns=application,name,description,remotes", "--", term],
                                timeout=60)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            app_id, name, desc, remotes = (p.strip() for p in parts[:4])
            if not FLATPAK_ID_RE.match(app_id) or "flathub" not in remotes.split(","):
                continue
            out.append({"app_id": app_id, "name": name or app_id, "summary": desc or None, "developer": None,
                        "license": None, "open_source": None, "verified": None, "icon": None,
                        "source": "flathub", "url": APP_PAGE + app_id})
        return out

    @action("details", Risk.READ, "Fiche détaillée d'une application Flathub (licence, développeur, vérification, permissions).",
            {"app_id": _APP_ID})
    def details(self, app_id: str):
        validate_flatpak_id(app_id)
        try:
            raw, stale = self.flathub.appstream(app_id)
        except NotOnFlathub as e:
            raise NotFound(f"Application introuvable sur Flathub: {app_id}") from e
        except NexusError as e:
            if e.code == "network_error":
                raise CommandFailed("Flathub est injoignable et cette fiche n'est pas en cache.",
                                    code="network_error") from e
            raise
        card = normalize_appstream(app_id, raw)
        try:
            verified, vstale = self.flathub.verified(app_id)
        except NexusError as e:
            if e.code != "network_error":
                raise
            verified, vstale = None, True
        curated = catalog.curated_apps().get(app_id)
        installed = self._installed_ids()
        perms = self.flathub.permissions(app_id)
        card.update(
            verified=verified,
            permissions=perms,
            permissions_note=None if perms is not None else "Permissions non communiquées par Flathub.",
            curated=app_id in catalog.curated_installable_ids(),
            proprietary=(curated or {}).get("proprietary", card["open_source"] is False),
            note=(curated or {}).get("note"),
            installable=(curated or {}).get("installable", True) and card.get("type") != "addon",
            installed=(app_id in installed) if installed is not None else None,
            stale=stale or vstale,
            integrity="Signatures GPG OSTree vérifiées par Flatpak (clé du remote flathub).",
        )
        if card["open_source"] is None and card["proprietary"]:
            card["open_source"] = False
        return card

    @action("install", Risk.INSTALL_VERIFIED,
            "Installer une application Flathub vérifiée ou de la sélection NEXUS.",
            {"app_id": _APP_ID}, summary=lambda p: f"Installer {p['app_id']} depuis Flathub")
    def install(self, app_id: str):
        validate_flatpak_id(app_id)
        self._require_flatpak()
        curated_entry = catalog.curated_apps().get(app_id)
        if curated_entry and not curated_entry.get("installable", True):
            raise InvalidParams(f"{app_id} n'est pas installable automatiquement : {curated_entry.get('note', '')}",
                                code="not_installable")
        curated = app_id in catalog.curated_installable_ids()
        info = self._resolve(app_id)
        if info["exists"] is False:
            raise NotFound(f"Application introuvable sur Flathub: {app_id}")
        if not curated and (info["exists"] is None or info["verified"] is None):
            raise CommandFailed("Flathub est injoignable : impossible de vérifier cette application.",
                                code="network_error")
        if not (info["verified"] is True or curated):
            raise PermissionDenied(
                f"{app_id} n'est pas vérifiée par Flathub et ne fait pas partie de la sélection NEXUS. "
                "Utilisez « Installer quand même » (store.install_unverified) : une confirmation explicite "
                "de l'utilisateur sera demandée.",
                code="requires_unverified_flow",
                details={"app_id": app_id, "verified": info["verified"], "next_action": "store.install_unverified"})
        return self._submit_install(app_id, "flathub", verified=info["verified"], curated=curated,
                                    kind="verified" if info["verified"] else "curated", title_name=info["name"])

    @action("install_unverified", Risk.UNKNOWN_SOURCE,
            "Installer une application Flathub NON vérifiée (confirmation utilisateur obligatoire, interdit à l'IA). "
            "Seul le dépôt flathub est accepté en v0.1.",
            {"app_id": _APP_ID,
             "remote": Param("str", False, "Dépôt Flatpak (seul « flathub » est supporté)", default="flathub")},
            summary=lambda p: f"Installer {p['app_id']} (NON vérifiée) depuis {p.get('remote', 'flathub')}")
    def install_unverified(self, app_id: str, remote: str = "flathub"):
        validate_flatpak_id(app_id)
        try:
            check_remote(remote)
        except PermissionDenied as e:
            raise PermissionDenied(
                f"Dépôt « {remote} » refusé : seules les applications Flathub sont supportées en v0.1.",
                details={"remote": remote}) from e
        self._require_flatpak()
        info = self._resolve(app_id)
        if info["exists"] is False:
            raise NotFound(f"Application introuvable sur Flathub: {app_id}")
        return self._submit_install(app_id, remote, verified=info["verified"],
                                    curated=app_id in catalog.curated_installable_ids(),
                                    kind="unverified", title_name=info["name"])

    @action("install_free_game", Risk.INSTALL_FREE_GAME,
            "Installer un jeu libre et gratuit de la sélection NEXUS (Flathub).",
            {"app_id": _APP_ID}, summary=lambda p: f"Installer le jeu gratuit {p['app_id']}")
    def install_free_game(self, app_id: str):
        validate_flatpak_id(app_id)
        if app_id not in catalog.free_game_ids():
            raise PermissionDenied(f"{app_id} ne fait pas partie des jeux gratuits sélectionnés par NEXUS.",
                                   code="not_curated_free_game")
        self._require_flatpak()
        info = self._resolve(app_id)
        if info["exists"] is False:
            raise NotFound(f"Jeu introuvable sur Flathub: {app_id}")
        return self._submit_install(app_id, "flathub", verified=info["verified"], curated=True,
                                    kind="free_game", title_name=info["name"])

    @action("uninstall", Risk.DELETE_DATA, "Désinstaller une application Flatpak.",
            {"app_id": _APP_ID,
             "delete_data": Param("bool", False, "Supprimer aussi les données de l'application", default=False)},
            summary=lambda p: f"Désinstaller {p['app_id']}" + (" et supprimer ses données" if p.get("delete_data") else ""))
    def uninstall(self, app_id: str, delete_data: bool = False):
        validate_flatpak_id(app_id)
        self._require_flatpak()
        if not self.fp.is_installed(app_id):
            raise NotFound(f"{app_id} n'est pas installée.")
        fp = self.fp

        def job(task):
            task.update(0.1, f"Désinstallation de {app_id}")
            fp.uninstall(app_id, delete_data=delete_data)
            self._record("uninstall", app_id, "flathub", None, app_id in catalog.curated_installable_ids())
            self.ctx.events.publish("store.uninstalled", {"app_id": app_id, "delete_data": delete_data})
            return {"app_id": app_id, "uninstalled": True}

        return self.ctx.tasks.submit(f"Désinstallation de {app_id}", job)

    @action("installed", Risk.READ, "Applications Flatpak installées.")
    def installed(self):
        self._require_flatpak()
        curated = catalog.curated_apps()
        apps = []
        for a in self.fp.installed():
            d = a.to_dict()
            d["curated"] = a.app_id in catalog.curated_installable_ids()
            d["proprietary"] = curated.get(a.app_id, {}).get("proprietary")
            d["url"] = APP_PAGE + a.app_id if a.origin == "flathub" else None
            apps.append(d)
        return {"apps": apps, "count": len(apps)}

    @action("updates", Risk.READ, "Mises à jour Flatpak disponibles.")
    def updates(self):
        self._require_flatpak()
        ids = self.fp.updates()
        return {"updates": ids, "count": len(ids)}

    @action("update_all", Risk.INSTALL_VERIFIED, "Mettre à jour toutes les applications Flatpak (Flathub).",
            summary=lambda p: "Mettre à jour toutes les applications Flatpak")
    def update_all(self):
        self._require_flatpak()
        fp = self.fp

        def job(task):
            task.update(0.05, "Mise à jour des applications (signatures GPG vérifiées par Flatpak)")
            fp.update_all()
            self.ctx.events.publish("store.updated", {"date": _now_iso()})
            return {"updated": True}

        return self.ctx.tasks.submit("Mise à jour des applications", job)

    @action("free_games", Risk.READ, "Jeux gratuits et légaux : jeux libres Flathub et free-to-play Steam.")
    def free_games(self):
        installed = self._installed_ids()
        flathub = [{**g, "source": "flathub", "url": APP_PAGE + g["app_id"],
                    "install_action": "store.install_free_game",
                    "installed": (g["app_id"] in installed) if installed is not None else None}
                   for g in catalog.FREE_FLATHUB_GAMES]
        steam = [{**g, "source": "steam", "install_url": f"steam://install/{g['appid']}",
                  "store_url": f"https://store.steampowered.com/app/{g['appid']}/",
                  "handled_by": "games", "note": "Free-to-play : installation via le client Steam (compte requis)."}
                 for g in catalog.FREE_STEAM_GAMES]
        return {"flathub": flathub, "steam": steam}

    @action("history", Risk.READ, "Historique des installations/désinstallations faites par NEXUS Store.")
    def history(self):
        hist = load_json(self.history_path, [])
        return {"entries": hist if isinstance(hist, list) else []}

    def status(self) -> dict:
        avail = self.fp.available()
        count = None
        if avail:
            try:
                count = len(self.fp.installed())
            except NexusError:
                count = None
        reach = self.flathub.reachable()
        return {
            "ok": avail,
            "flatpak_available": avail,
            "flathub_reachable": reach,
            "flathub_last_ok": self.flathub.last_ok,
            "flathub_last_error": self.flathub.last_error,
            "installed_count": count,
            "message": None if avail else "Flatpak est absent : installation d'applications impossible.",
        }


MODULE = StoreModule
