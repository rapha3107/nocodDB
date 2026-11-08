from __future__ import annotations

import re
import secrets
import threading
from typing import Any, Callable

from ...core.config import load_json, save_json
from ...core.errors import InvalidParams, NexusError, NotFound
from ...core.module import NexusModule, Param, action
from ...security.permissions import Risk

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLORS = ["#4cc9f0", "#ff6b6b", "#ffb703", "#8e7dff", "#2ec4b6", "#f72585", "#90be6d", "#f9844a"]
DEFAULT_AVATARS = ["🎮", "🚀", "🐉", "⚡", "🌙", "🦊", "👾", "🎧"]
MAX_PROFILES = 12


def _bool(v):
    if not isinstance(v, bool):
        raise InvalidParams("Valeur booléenne attendue")
    return v


def _choice(*choices):
    def f(v):
        if v not in choices:
            raise InvalidParams(f"Valeur non autorisée (attendu : {', '.join(map(str, choices))})")
        return v
    return f


def _int_range(lo, hi):
    def f(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise InvalidParams(f"Entier entre {lo} et {hi} attendu")
        return v
    return f


def _budget(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100000:
        raise InvalidParams("Budget mensuel invalide (nombre entre 0 et 100000)")
    return round(float(v), 2)


# Whitelist of keys the UI (or the AI, with confirmation) may change.
SETTABLE: dict[str, Callable[[Any], Any]] = {
    "language": _choice("fr", "en"),
    "ui.theme": _choice("dark", "oled"),
    "ui.keyboard_layout": _choice("azerty", "qwerty"),
    "ui.sounds": _bool,
    "music.volume": _int_range(0, 100),
    "ai.voice_enabled": _bool,
    "ai.monthly_budget": _budget,
    "ai.share_library_with_remote": _bool,
    "security.auto_install_verified": _bool,
    "security.auto_install_free_games": _bool,
    "input.pointer_speed": _int_range(1, 10),
    "input.keyboard_layout": _choice("azerty", "qwerty"),
}


class ProfilesModule(NexusModule):
    name = "profiles"
    title = "Profils"
    description = "Profils utilisateur et réglages de NEXUS"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file = ctx.paths.data / "profiles.json"
        self._lock = threading.Lock()

    # ---- storage -----------------------------------------------------------
    def _load(self) -> list[dict]:
        data = load_json(self.file, None)
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list) or not profiles:
            profiles = [{"id": "default", "name": "Joueur", "avatar": DEFAULT_AVATARS[0],
                         "color": DEFAULT_COLORS[0]}]
            save_json(self.file, {"profiles": profiles})
        return [p for p in profiles if isinstance(p, dict) and "id" in p]

    def _save(self, profiles: list[dict]) -> None:
        save_json(self.file, {"profiles": profiles})

    def _active_id(self, profiles: list[dict]) -> str:
        active = self.ctx.settings.get("profile.active")
        ids = [p["id"] for p in profiles]
        return active if active in ids else ids[0]

    def status(self) -> dict:
        with self._lock:
            profiles = self._load()
        return {"ok": True, "profiles": len(profiles), "active": self._active_id(profiles)}

    # ---- actions -----------------------------------------------------------
    @action("list", Risk.READ, "Liste les profils utilisateur et indique le profil actif")
    def list(self):
        with self._lock:
            profiles = self._load()
        active = self._active_id(profiles)
        return {"profiles": [{**p, "active": p["id"] == active} for p in profiles], "active": active}

    @action("current", Risk.READ, "Renvoie le profil actif")
    def current(self):
        with self._lock:
            profiles = self._load()
        active = self._active_id(profiles)
        return next(p for p in profiles if p["id"] == active)

    @action("create", Risk.SETTINGS, "Crée un profil utilisateur",
            {"name": Param("str", description="Nom du profil (1-24 caractères)"),
             "avatar": Param("str", required=False, description="Emoji d'avatar"),
             "color": Param("str", required=False, description="Couleur d'accent #rrggbb")},
            summary=lambda p: f"Créer le profil « {p.get('name')} »")
    def create(self, name, avatar=None, color=None):
        name = " ".join(name.split())
        if not 1 <= len(name) <= 24:
            raise InvalidParams("Le nom du profil doit faire entre 1 et 24 caractères")
        if avatar is not None and not 1 <= len(avatar) <= 8:
            raise InvalidParams("Avatar invalide")
        if color is not None and not COLOR_RE.match(color):
            raise InvalidParams("Couleur invalide (format #rrggbb)")
        with self._lock:
            profiles = self._load()
            if len(profiles) >= MAX_PROFILES:
                raise NexusError(f"Nombre maximal de profils atteint ({MAX_PROFILES})", code="limit_reached")
            if any(p["name"].casefold() == name.casefold() for p in profiles):
                raise InvalidParams("Un profil porte déjà ce nom")
            n = len(profiles)
            prof = {"id": "p_" + secrets.token_hex(4), "name": name,
                    "avatar": avatar or DEFAULT_AVATARS[n % len(DEFAULT_AVATARS)],
                    "color": (color or DEFAULT_COLORS[n % len(DEFAULT_COLORS)]).lower()}
            profiles.append(prof)
            self._save(profiles)
        self.ctx.events.publish("profiles.changed", {"created": prof["id"]})
        return prof

    @action("update", Risk.SETTINGS, "Modifie le nom, l'avatar ou la couleur d'un profil",
            {"id": Param("str"), "name": Param("str", required=False),
             "avatar": Param("str", required=False), "color": Param("str", required=False)},
            summary=lambda p: f"Modifier le profil {p.get('id')}")
    def update(self, id, name=None, avatar=None, color=None):
        with self._lock:
            profiles = self._load()
            prof = next((p for p in profiles if p["id"] == id), None)
            if not prof:
                raise NotFound("Profil introuvable")
            if name is not None:
                name = " ".join(name.split())
                if not 1 <= len(name) <= 24:
                    raise InvalidParams("Le nom du profil doit faire entre 1 et 24 caractères")
                if any(p["name"].casefold() == name.casefold() and p["id"] != id for p in profiles):
                    raise InvalidParams("Un profil porte déjà ce nom")
                prof["name"] = name
            if avatar is not None:
                if not 1 <= len(avatar) <= 8:
                    raise InvalidParams("Avatar invalide")
                prof["avatar"] = avatar
            if color is not None:
                if not COLOR_RE.match(color):
                    raise InvalidParams("Couleur invalide (format #rrggbb)")
                prof["color"] = color.lower()
            self._save(profiles)
        self.ctx.events.publish("profiles.changed", {"updated": id})
        return prof

    @action("switch", Risk.CONTROL, "Change de profil actif", {"id": Param("str")})
    def switch(self, id):
        with self._lock:
            profiles = self._load()
        prof = next((p for p in profiles if p["id"] == id), None)
        if not prof:
            raise NotFound("Profil introuvable")
        self.ctx.settings.set("profile.active", id)
        self.ctx.events.publish("profiles.changed", {"active": id})
        return prof

    @action("delete", Risk.DELETE_DATA, "Supprime un profil (le dernier profil ne peut pas être supprimé)",
            {"id": Param("str")}, summary=lambda p: f"Supprimer le profil {p.get('id')}")
    def delete(self, id):
        with self._lock:
            profiles = self._load()
            if not any(p["id"] == id for p in profiles):
                raise NotFound("Profil introuvable")
            if len(profiles) <= 1:
                raise NexusError("Impossible de supprimer le dernier profil", code="last_profile")
            was_active = self._active_id(profiles) == id
            profiles = [p for p in profiles if p["id"] != id]
            self._save(profiles)
        if was_active:
            self.ctx.settings.set("profile.active", profiles[0]["id"])
        self.ctx.events.publish("profiles.changed", {"deleted": id})
        return {"deleted": id, "active": self._active_id(profiles)}

    @action("settings_get", Risk.READ, "Renvoie tous les réglages de NEXUS (avec valeurs par défaut)")
    def settings_get(self):
        return {"settings": self.ctx.settings.all(), "settable": sorted(SETTABLE)}

    @action("settings_set", Risk.SETTINGS,
            "Modifie un réglage autorisé (langue, thème, volume musique, voix IA, budget IA, "
            "politique d'installation automatique)",
            {"key": Param("str", choices=sorted(SETTABLE)), "value": Param("any")},
            summary=lambda p: f"Régler {p.get('key')} = {p.get('value')}")
    def settings_set(self, key, value):
        value = SETTABLE[key](value)
        self.ctx.settings.set(key, value)
        self.ctx.events.publish("settings.changed", {"key": key, "value": value})
        return {"key": key, "value": value}
