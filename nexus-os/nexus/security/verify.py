"""Provenance & integrity helpers used by Store, Emulation and Music downloads."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..core.errors import InvalidParams, PermissionDenied

# Flatpak remotes NEXUS trusts. flathub-beta etc. are deliberately excluded.
TRUSTED_FLATPAK_REMOTES = {"flathub"}

FLATPAK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
STEAM_APPID_RE = re.compile(r"^[0-9]{1,10}$")


def validate_flatpak_id(app_id: str) -> str:
    if not isinstance(app_id, str) or not FLATPAK_ID_RE.match(app_id) or len(app_id) > 255:
        raise InvalidParams(f"Identifiant Flatpak invalide: {app_id!r}")
    return app_id


def validate_steam_appid(appid) -> str:
    s = str(appid)
    if not STEAM_APPID_RE.match(s):
        raise InvalidParams(f"Identifiant Steam invalide: {appid!r}")
    return s


def check_remote(remote: str) -> str:
    if remote not in TRUSTED_FLATPAK_REMOTES:
        raise PermissionDenied(f"Dépôt non approuvé: {remote}")
    return remote


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        path.unlink(missing_ok=True)
        raise PermissionDenied("Somme de contrôle invalide: fichier supprimé",
                               details={"expected": expected, "actual": actual})


def safe_join(base: Path, *parts: str) -> Path:
    """Join user-provided path parts under base; refuse traversal and symlink escapes."""
    base = base.resolve()
    p = base.joinpath(*parts).resolve()
    if p != base and base not in p.parents:
        raise PermissionDenied("Chemin hors de la zone autorisée")
    return p
