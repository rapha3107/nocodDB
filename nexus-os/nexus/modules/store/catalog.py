"""Catalogues curatés NEXUS (identifiants réels uniquement).

Règle : n'apparaissent ici que des identifiants Flathub / Steam dont l'existence
est connue avec certitude. Les métadonnées affichées (licence, résumé,
vérification) ne sont PAS codées en dur : elles viennent de Flathub au moment
de l'affichage. Seuls les drapeaux `proprietary` ci-dessous sont des faits
stables connus (clients propriétaires redistribués sur Flathub).

Exclusions volontaires :
  * Veloren : l'identifiant Flathub exact n'a pas pu être confirmé avec
    certitude (application vs lanceur Airshipper) → non listé.
  * Minetest a été renommé Luanti ; l'identifiant historique
    `net.minetest.Minetest` a été remplacé sur Flathub par `org.luanti.luanti`.
"""

from __future__ import annotations

FEATURED: list[dict] = [
    {
        "id": "launchers",
        "title": "Lanceurs de jeux",
        "apps": [
            {"app_id": "com.valvesoftware.Steam", "name": "Steam", "proprietary": True,
             "note": "Client propriétaire de Valve. Bazzite inclut déjà Steam en natif : "
                     "la version Flatpak est facultative."},
            {"app_id": "com.heroicgameslauncher.hgl", "name": "Heroic Games Launcher", "proprietary": False},
            {"app_id": "net.lutris.Lutris", "name": "Lutris", "proprietary": False},
            {"app_id": "com.usebottles.bottles", "name": "Bottles", "proprietary": False},
        ],
    },
    {
        "id": "gaming_tools",
        "title": "Outils gaming",
        "apps": [
            {"app_id": "net.davidotek.pupgui2", "name": "ProtonUp-Qt", "proprietary": False},
            {"app_id": "com.github.Matoking.protontricks", "name": "Protontricks", "proprietary": False},
            {"app_id": "org.freedesktop.Platform.VulkanLayer.MangoHud", "name": "MangoHud (couche Vulkan Flatpak)",
             "proprietary": False, "kind": "extension", "installable": False,
             "note": "Extension Flatpak (pas une application) : elle doit correspondre à la branche du "
                     "runtime de chaque jeu Flatpak. NEXUS ne l'installe pas automatiquement ; "
                     "MangoHud natif est déjà fourni par Bazzite (overlay du module hardware)."},
        ],
    },
    {
        "id": "multimedia",
        "title": "Multimédia",
        "apps": [
            {"app_id": "org.videolan.VLC", "name": "VLC", "proprietary": False},
            {"app_id": "com.spotify.Client", "name": "Spotify", "proprietary": True,
             "note": "Client propriétaire (compte Spotify requis)."},
            {"app_id": "com.obsproject.Studio", "name": "OBS Studio", "proprietary": False},
        ],
    },
    {
        "id": "communication",
        "title": "Communication",
        "apps": [
            {"app_id": "com.discordapp.Discord", "name": "Discord", "proprietary": True,
             "note": "Client propriétaire, empaqueté sur Flathub."},
        ],
    },
    {
        "id": "browsers",
        "title": "Navigateurs",
        "apps": [
            {"app_id": "org.mozilla.firefox", "name": "Firefox", "proprietary": False},
        ],
    },
]

# Jeux libres et gratuits disponibles sur Flathub.
FREE_FLATHUB_GAMES: list[dict] = [
    {"app_id": "net.supertuxkart.SuperTuxKart", "name": "SuperTuxKart", "genre": "Course"},
    {"app_id": "com.play0ad.zeroad", "name": "0 A.D.", "genre": "Stratégie temps réel"},
    {"app_id": "org.xonotic.Xonotic", "name": "Xonotic", "genre": "FPS arène"},
    {"app_id": "com.github.Anuken.Mindustry", "name": "Mindustry", "genre": "Tower defense / automatisation"},
    {"app_id": "org.openttd.OpenTTD", "name": "OpenTTD", "genre": "Gestion / transport"},
    {"app_id": "org.luanti.luanti", "name": "Luanti (ex-Minetest)", "genre": "Bac à sable voxel"},
    {"app_id": "org.wesnoth.Wesnoth", "name": "The Battle for Wesnoth", "genre": "Stratégie tour par tour"},
    {"app_id": "org.hedgewars.Hedgewars", "name": "Hedgewars", "genre": "Artillerie tour par tour"},
    {"app_id": "org.supertuxproject.SuperTux", "name": "SuperTux", "genre": "Plateforme"},
]

# Jeux free-to-play officiels sur Steam (installés via le client Steam / module games).
FREE_STEAM_GAMES: list[dict] = [
    {"appid": "570", "name": "Dota 2", "genre": "MOBA"},
    {"appid": "440", "name": "Team Fortress 2", "genre": "FPS"},
    {"appid": "730", "name": "Counter-Strike 2", "genre": "FPS"},
]


def curated_apps() -> dict[str, dict]:
    out = {}
    for cat in FEATURED:
        for a in cat["apps"]:
            out[a["app_id"]] = {**a, "category": cat["id"]}
    return out


def curated_installable_ids() -> set[str]:
    ids = {a for a, e in curated_apps().items() if e.get("installable", True)}
    ids |= {g["app_id"] for g in FREE_FLATHUB_GAMES}
    return ids


def free_game_ids() -> set[str]:
    return {g["app_id"] for g in FREE_FLATHUB_GAMES}
