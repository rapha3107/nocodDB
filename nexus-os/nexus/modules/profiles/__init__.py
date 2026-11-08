"""NEXUS profiles + user settings.

Profiles (id, name, avatar emoji, accent color) are stored in
``<data>/profiles.json``; the active one is the ``profile.active`` setting.

Settings: ``settings_get`` returns ``ctx.settings.all()``; ``settings_set``
only accepts keys from ``SETTABLE`` with a per-key validator.

Security note: ``settings_set`` is ``Risk.SETTINGS``. When NEXUS AI calls it
(``Origin.AI``) the permission manager always requires a confirmation that
only the UI endpoint (session token, ``Origin.USER``) can approve. Therefore
``security.*`` keys (auto-install policy) can only ever be changed by the
human, never silently by the AI. ``profile.active`` is not settable here: use
``profiles.switch``.
"""

from .module import ProfilesModule, SETTABLE

MODULE = ProfilesModule
__all__ = ["MODULE", "ProfilesModule", "SETTABLE"]
