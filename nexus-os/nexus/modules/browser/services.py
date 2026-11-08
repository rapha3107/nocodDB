"""Catalogue curaté de services de streaming accessibles depuis un navigateur sous Linux.

URL officielles uniquement. La compatibilité n'est jamais garantie : les services
protégés par DRM (Widevine) nécessitent l'activation de « Lire le contenu protégé
par DRM » dans Firefox, et leur résolution est possiblement limitée sous Linux.
"""

from __future__ import annotations

DRM_NOTE = ("Contenu protégé par DRM (Widevine) : activez « Lire le contenu protégé par DRM » "
            "dans les paramètres de Firefox. Résolution possiblement limitée sous Linux.")

SERVICES: list[dict] = [
    {"id": "youtube", "title": "YouTube", "url": "https://www.youtube.com", "category": "vidéo",
     "drm": False, "compatibility": "bon",
     "note": "Lecture sans DRM pour l'essentiel des vidéos."},
    {"id": "twitch", "title": "Twitch", "url": "https://www.twitch.tv", "category": "vidéo",
     "drm": False, "compatibility": "bon",
     "note": "Lecture des directs et rediffusions sans DRM pour l'essentiel des chaînes."},
    {"id": "netflix", "title": "Netflix", "url": "https://www.netflix.com", "category": "vidéo",
     "drm": True, "compatibility": "limité", "note": DRM_NOTE},
    {"id": "disneyplus", "title": "Disney+", "url": "https://www.disneyplus.com", "category": "vidéo",
     "drm": True, "compatibility": "limité", "note": DRM_NOTE},
    {"id": "primevideo", "title": "Prime Video", "url": "https://www.primevideo.com", "category": "vidéo",
     "drm": True, "compatibility": "limité", "note": DRM_NOTE},
    {"id": "crunchyroll", "title": "Crunchyroll", "url": "https://www.crunchyroll.com", "category": "vidéo",
     "drm": True, "compatibility": "limité", "note": DRM_NOTE},
    {"id": "spotify", "title": "Spotify Web", "url": "https://open.spotify.com", "category": "musique",
     "drm": True, "compatibility": "bon",
     "note": "Le lecteur web nécessite la lecture de contenus DRM (Widevine) activée dans Firefox."},
    {"id": "youtubemusic", "title": "YouTube Music", "url": "https://music.youtube.com", "category": "musique",
     "drm": False, "compatibility": "bon",
     "note": "Lecture dans le navigateur, sans application dédiée."},
]

CONTROLLER_NOTE = ("Dans le navigateur externe, la manette pilote un pointeur souris via Steam Input "
                   "(ou le mappage manette→souris du module matériel) : la navigation à la manette "
                   "n'est pas native dans Firefox.")
