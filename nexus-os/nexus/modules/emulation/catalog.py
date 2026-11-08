"""Curated emulator catalogue and platform table.

Rules followed here (see docs/AGENT_BRIEF.md):
  * only open-source emulators, Flathub ids we are sure of; anything we could
    not confirm is flagged `availability: "à vérifier"` (install then checks
    `flatpak remote-info flathub <id>` first and fails cleanly if absent);
  * compatibility levels are honest and never presented as a guarantee
    (`stable`, `bon`, `expérimental`, `limité`, `non supporté`);
  * BIOS/firmware/keys are never downloaded: the user provides dumps from
    their own hardware (or, for PS3, the official firmware file from Sony);
  * command-line options are only those documented by each project; when we
    are not sure of a "boot this file" syntax, `launch` opens the emulator
    without a ROM (`rom_args = None`) instead of guessing.

Save paths assume the Flathub build (data under ~/.var/app/<flatpak id>/).
Paths marked "(à vérifier)" depend on the emulator version/configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

USER_DUMP = "fourni par l'utilisateur à partir de son propre matériel (aucun téléchargement par NEXUS)"

# --------------------------------------------------------------------------- argv builders
# Each builder receives the absolute ROM path (a file, or a game folder for PS3)
# and returns the arguments placed AFTER the flatpak app id. ROM paths are
# absolute (start with "/") so they can never be parsed as an option.

def _retroarch_args(rom: Path, core: str | None) -> list[str]:
    # RetroArch: -f/--fullscreen, -L/--libretro <core.so> <content>
    if not core:
        raise ValueError("core required")
    return ["-f", "-L", core, str(rom)]


def _pcsx2_args(rom: Path, core=None) -> list[str]:
    # PCSX2 (Qt): -batch (quit when the game stops), -fullscreen, then the file.
    return ["-batch", "-fullscreen", str(rom)]


def _duckstation_args(rom: Path, core=None) -> list[str]:
    # DuckStation (Qt): -batch, -fullscreen, then the file.
    return ["-batch", "-fullscreen", str(rom)]


def _rpcs3_args(rom: Path, core=None) -> list[str]:
    # RPCS3: --no-gui <EBOOT.BIN | game folder>.
    for eboot in (rom / "PS3_GAME" / "USRDIR" / "EBOOT.BIN", rom / "USRDIR" / "EBOOT.BIN"):
        if eboot.is_file():
            return ["--no-gui", str(eboot)]
    return ["--no-gui", str(rom)]


def _dolphin_args(rom: Path, core=None) -> list[str]:
    # Dolphin: -b/--batch (no main window), -e/--exec=<file>.
    return ["-b", "-e", str(rom)]


def _cemu_args(rom: Path, core=None) -> list[str]:
    # Cemu: -f/--fullscreen, -g/--game <file>.
    return ["-f", "-g", str(rom)]


def _xemu_args(rom: Path, core=None) -> list[str]:
    # xemu: -dvd_path <xiso>.
    return ["-dvd_path", str(rom)]


def _positional(rom: Path, core=None) -> list[str]:
    # PPSSPP, melonDS, mGBA, Flycast, Azahar: the file as positional argument.
    return [str(rom)]


ArgBuilder = Callable[[Path, "str | None"], list[str]]

# --------------------------------------------------------------------------- emulators

EMULATORS: dict[str, dict] = {
    "retroarch": {
        "name": "RetroArch",
        "flatpak_id": "org.libretro.RetroArch",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-3.0-or-later",
        "systems": ["ps1", "nes", "snes", "n64", "megadrive", "gb", "gbc", "gba"],
        "compatibility": {"level": "stable",
                          "note": "Dépend du cœur libretro choisi ; les cœurs se téléchargent depuis "
                                  "RetroArch (Mise à jour en ligne > Téléchargeur de cœurs)."},
        "requirements": {"vulkan": "recommended"},
        "bios": [
            {"system": "ps1", "required": True,
             "description": "BIOS PlayStation (ex. scph5500/5501/5502.bin pour Beetle PSX) à placer dans "
                            "~/.var/app/org.libretro.RetroArch/config/retroarch/system/ — " + USER_DUMP},
        ],
        "bios_dir": ".var/app/org.libretro.RetroArch/config/retroarch/system",
        "saves": ["~/.var/app/org.libretro.RetroArch/config/retroarch/saves",
                  "~/.var/app/org.libretro.RetroArch/config/retroarch/states"],
        "launch": "flatpak run org.libretro.RetroArch -f -L <cœur.so> <rom>",
        "rom_args": _retroarch_args,
    },
    "duckstation": {
        "name": "DuckStation",
        "flatpak_id": "org.duckstation.DuckStation",
        "availability": "retiré de Flathub",
        "installable": False,
        "license": "CC-BY-NC-ND-4.0 (depuis 2024 ; auparavant GPL-3.0)",
        "systems": ["ps1"],
        "compatibility": {"level": "stable",
                          "note": "Le développeur a changé de licence (non libre) et retiré la version "
                                  "Flathub en 2024. NEXUS ne l'installe pas : utilisez RetroArch avec le "
                                  "cœur SwanStation (fork libre de DuckStation) ou Beetle PSX HW. Si "
                                  "DuckStation est déjà installé, NEXUS peut le lancer."},
        "requirements": {},
        "bios": [{"system": "ps1", "required": True, "description": "BIOS PlayStation — " + USER_DUMP}],
        "saves": ["~/.var/app/org.duckstation.DuckStation/config/duckstation/memcards (à vérifier)"],
        "launch": "flatpak run org.duckstation.DuckStation -batch -fullscreen <rom>",
        "rom_args": _duckstation_args,
    },
    "pcsx2": {
        "name": "PCSX2",
        "flatpak_id": "net.pcsx2.PCSX2",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-3.0-or-later",
        "systems": ["ps2"],
        "compatibility": {"level": "stable",
                          "note": "La grande majorité du catalogue PS2 est jouable ; quelques jeux "
                                  "nécessitent des correctifs spécifiques."},
        "requirements": {"vulkan": "recommended", "min_cores": 4, "min_ram_gb": 4},
        "bios": [{"system": "ps2", "required": True,
                  "description": "BIOS PS2 à placer dans ~/.var/app/net.pcsx2.PCSX2/config/PCSX2/bios — "
                                 + USER_DUMP}],
        "bios_dir": ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios",
        "saves": ["~/.var/app/net.pcsx2.PCSX2/config/PCSX2/memcards",
                  "~/.var/app/net.pcsx2.PCSX2/config/PCSX2/sstates"],
        "launch": "flatpak run net.pcsx2.PCSX2 -batch -fullscreen <rom>",
        "rom_args": _pcsx2_args,
    },
    "rpcs3": {
        "name": "RPCS3",
        "flatpak_id": "net.rpcs3.RPCS3",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-2.0-only",
        "systems": ["ps3"],
        "compatibility": {"level": "bon",
                          "note": "Bon mais exigeant : une large part du catalogue est jouable, mais les "
                                  "performances dépendent fortement du CPU. Consultez la liste de "
                                  "compatibilité officielle de RPCS3 pour chaque jeu."},
        "requirements": {"avx2": "recommended", "vulkan": "required", "min_cores": 6, "min_ram_gb": 8},
        "bios": [{"system": "ps3", "required": True,
                  "description": "Firmware officiel PS3 (PS3UPDAT.PUP) téléchargé par l'utilisateur sur le "
                                 "site de Sony, puis installé via RPCS3 > Fichier > Installer le firmware. "
                                 "Jeux : dumps de vos propres disques/achats."}],
        "bios_dir": ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_flash",
        "saves": ["~/.var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/home/00000001/savedata"],
        "launch": "flatpak run net.rpcs3.RPCS3 --no-gui <dossier du jeu>/PS3_GAME/USRDIR/EBOOT.BIN",
        "rom_args": _rpcs3_args,
    },
    "shadps4": {
        "name": "shadPS4",
        "flatpak_id": "net.shadps4.shadPS4",
        "availability": "à vérifier",
        "installable": True,
        "license": "GPL-2.0-or-later",
        "systems": ["ps4"],
        "compatibility": {"level": "expérimental",
                          "note": "Émulation PS4 en développement actif : seule une minorité de jeux "
                                  "démarre ou est jouable. Aucune garantie."},
        "requirements": {"avx2": "required", "vulkan": "required", "min_cores": 6, "min_ram_gb": 8},
        "bios": [{"system": "ps4", "required": False,
                  "description": "Certains jeux demandent des modules système (sys_modules) extraits de "
                                 "votre propre console — " + USER_DUMP}],
        "saves": ["~/.var/app/net.shadps4.shadPS4/ (dossier user/savedata, à vérifier)"],
        "launch": "Ouvre shadPS4 ; choisissez le jeu dans son interface (syntaxe de lancement direct non "
                  "confirmée, donc non utilisée).",
        "rom_args": None,
    },
    "ppsspp": {
        "name": "PPSSPP",
        "flatpak_id": "org.ppsspp.PPSSPP",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-2.0-or-later",
        "systems": ["psp"],
        "compatibility": {"level": "stable", "note": "Très large compatibilité, faibles exigences."},
        "requirements": {},
        "bios": [],
        "saves": ["~/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SAVEDATA",
                  "~/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/PPSSPP_STATE"],
        "launch": "flatpak run org.ppsspp.PPSSPP <rom>",
        "rom_args": _positional,
    },
    "dolphin": {
        "name": "Dolphin",
        "flatpak_id": "org.DolphinEmu.dolphin-emu",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-2.0-or-later",
        "systems": ["gamecube", "wii"],
        "compatibility": {"level": "stable", "note": "Excellente compatibilité GameCube et Wii."},
        "requirements": {"vulkan": "recommended"},
        "bios": [{"system": "gamecube", "required": False,
                  "description": "IPL GameCube facultatif (Dolphin émule le démarrage en HLE) — " + USER_DUMP}],
        "saves": ["~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC",
                  "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title"],
        "launch": "flatpak run org.DolphinEmu.dolphin-emu -b -e <rom>",
        "rom_args": _dolphin_args,
    },
    "cemu": {
        "name": "Cemu",
        "flatpak_id": "info.cemu.Cemu",
        "availability": "flathub",
        "installable": True,
        "license": "MPL-2.0",
        "systems": ["wiiu"],
        "compatibility": {"level": "bon", "note": "Beaucoup de jeux Wii U jouables ; compilation des "
                                                  "shaders au premier lancement."},
        "requirements": {"vulkan": "recommended", "min_cores": 4, "min_ram_gb": 8},
        "bios": [{"system": "wiiu", "required": False,
                  "description": "keys.txt requis uniquement pour les images chiffrées (.wud/.wux) — clés "
                                 "issues de votre propre console. Les formats .wua/.rpx décryptés n'en "
                                 "ont pas besoin."}],
        "saves": ["~/.var/app/info.cemu.Cemu/data/Cemu/mlc01/usr/save (à vérifier)"],
        "launch": "flatpak run info.cemu.Cemu -f -g <rom>",
        "rom_args": _cemu_args,
    },
    "melonds": {
        "name": "melonDS",
        "flatpak_id": "net.kuribo64.melonDS",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-3.0-or-later",
        "systems": ["nds"],
        "compatibility": {"level": "stable", "note": "Très bonne compatibilité Nintendo DS."},
        "requirements": {},
        "bios": [{"system": "nds", "required": False,
                  "description": "BIOS/firmware DS facultatifs (BIOS libre intégré) ; requis pour le mode "
                                 "DSi — " + USER_DUMP}],
        "saves": ["Fichier .sav à côté de la ROM (comportement par défaut de melonDS)"],
        "launch": "flatpak run net.kuribo64.melonDS <rom>",
        "rom_args": _positional,
    },
    "mgba": {
        "name": "mGBA",
        "flatpak_id": "io.mgba.mGBA",
        "availability": "flathub",
        "installable": True,
        "license": "MPL-2.0",
        "systems": ["gb", "gbc", "gba"],
        "compatibility": {"level": "stable", "note": "Référence pour la GBA, gère aussi GB/GBC."},
        "requirements": {},
        "bios": [{"system": "gba", "required": False,
                  "description": "BIOS GBA facultatif (BIOS HLE intégré) — " + USER_DUMP}],
        "saves": ["Fichier .sav à côté de la ROM (comportement par défaut de mGBA)"],
        "launch": "flatpak run io.mgba.mGBA <rom>",
        "rom_args": _positional,
    },
    "xemu": {
        "name": "xemu",
        "flatpak_id": "app.xemu.xemu",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-2.0-or-later",
        "systems": ["xbox"],
        "compatibility": {"level": "bon", "note": "Une bonne partie du catalogue Xbox d'origine est "
                                                  "jouable ; images au format XISO uniquement."},
        "requirements": {"min_cores": 4},
        "bios": [{"system": "xbox", "required": True,
                  "description": "MCPX Boot ROM, BIOS flash et image disque dur Xbox, à configurer dans "
                                 "xemu — " + USER_DUMP}],
        "saves": ["Dans l'image disque dur Xbox (qcow2) configurée dans xemu"],
        "launch": "flatpak run app.xemu.xemu -dvd_path <xiso>",
        "rom_args": _xemu_args,
    },
    "flycast": {
        "name": "Flycast",
        "flatpak_id": "org.flycast.Flycast",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-2.0-only",
        "systems": ["dreamcast"],
        "compatibility": {"level": "bon", "note": "Très bonne compatibilité Dreamcast."},
        "requirements": {"vulkan": "recommended"},
        "bios": [{"system": "dreamcast", "required": False,
                  "description": "dc_boot.bin / dc_flash.bin recommandés (BIOS HLE disponible) — "
                                 + USER_DUMP}],
        "saves": ["~/.var/app/org.flycast.Flycast/data/flycast (cartes VMU, à vérifier)"],
        "launch": "flatpak run org.flycast.Flycast <rom>",
        "rom_args": _positional,
    },
    "azahar": {
        "name": "Azahar",
        "flatpak_id": "org.azahar_emu.Azahar",
        "availability": "à vérifier",
        "installable": True,
        "license": "GPL-2.0-or-later",
        "systems": ["3ds"],
        "compatibility": {"level": "bon", "note": "Successeur libre de Citra/Lime3DS. ROMs décryptées "
                                                  "uniquement (dumps de vos propres cartouches)."},
        "requirements": {"vulkan": "recommended"},
        "bios": [{"system": "3ds", "required": False,
                  "description": "Fichiers système facultatifs pour certains jeux — " + USER_DUMP}],
        "saves": ["~/.var/app/org.azahar_emu.Azahar/data/azahar-emu/sdmc (à vérifier)"],
        "launch": "flatpak run org.azahar_emu.Azahar <rom>",
        "rom_args": _positional,
    },
    "retrodeck": {
        "name": "RetroDECK",
        "flatpak_id": "net.retrodeck.retrodeck",
        "availability": "flathub",
        "installable": True,
        "license": "GPL-3.0-or-later",
        "systems": [],
        "compatibility": {"level": "stable",
                          "note": "Suite tout-en-un (frontend ES-DE + émulateurs) avec sa propre "
                                  "bibliothèque ; NEXUS l'ouvre mais ne lui passe pas de ROM."},
        "requirements": {},
        "bios": [],
        "saves": ["~/retrodeck/saves (emplacement par défaut de RetroDECK, configurable)"],
        "launch": "flatpak run net.retrodeck.retrodeck",
        "rom_args": None,
    },
    "xenia": {
        "name": "Xenia (Xbox 360)",
        "flatpak_id": None,
        "availability": "non disponible (Windows uniquement)",
        "installable": False,
        "license": "BSD-3-Clause",
        "systems": ["xbox360"],
        "compatibility": {"level": "non supporté",
                          "note": "Xenia est un programme Windows : sur Linux, il ne fonctionne que via "
                                  "Proton/Wine, de façon expérimentale. NEXUS ne l'installe pas "
                                  "automatiquement et ne le lance pas."},
        "requirements": {"vulkan": "required"},
        "bios": [],
        "saves": [],
        "launch": "Non pris en charge par NEXUS.",
        "rom_args": None,
    },
}

# --------------------------------------------------------------------------- RetroArch cores
# Core file stems as published by libretro (<stem>_libretro.so), in order of preference.
RETROARCH_CORES: dict[str, list[str]] = {
    "ps1": ["swanstation", "mednafen_psx_hw", "mednafen_psx", "pcsx_rearmed"],
    "nes": ["mesen", "fceumm", "nestopia"],
    "snes": ["snes9x", "bsnes"],
    "n64": ["mupen64plus_next", "parallel_n64"],
    "megadrive": ["genesis_plus_gx", "picodrive"],
    "gb": ["gambatte", "sameboy", "mgba"],
    "gbc": ["gambatte", "sameboy", "mgba"],
    "gba": ["mgba"],
}

# --------------------------------------------------------------------------- platforms
# kind = "file": scanned by extension; "ps3dir"/"ps4dir": game folders.
SYSTEMS: dict[str, dict] = {
    "ps1": {"name": "PlayStation", "kind": "file",
            "extensions": [".cue", ".chd", ".pbp", ".m3u", ".iso", ".bin", ".img"],
            "emulators": ["retroarch", "duckstation"], "support": "stable",
            "note": "Via RetroArch (cœur SwanStation ou Beetle PSX HW). BIOS requis."},
    "ps2": {"name": "PlayStation 2", "kind": "file", "extensions": [".iso", ".chd", ".cso", ".gz"],
            "emulators": ["pcsx2"], "support": "stable", "note": "BIOS PS2 requis."},
    "ps3": {"name": "PlayStation 3", "kind": "ps3dir", "extensions": [],
            "emulators": ["rpcs3"], "support": "bon",
            "note": "Bon mais exigeant (CPU 6 cœurs+, AVX2 recommandé). Firmware officiel requis. "
                    "Jeux en dossiers (PS3_GAME/PARAM.SFO)."},
    "ps4": {"name": "PlayStation 4", "kind": "ps4dir", "extensions": [],
            "emulators": ["shadps4"], "support": "expérimental",
            "note": "Expérimental : peu de jeux jouables."},
    "psp": {"name": "PSP", "kind": "file", "extensions": [".iso", ".cso", ".pbp"],
            "emulators": ["ppsspp"], "support": "stable", "note": ""},
    "nes": {"name": "NES", "kind": "file", "extensions": [".nes", ".unf", ".unif", ".fds", ".zip", ".7z"],
            "emulators": ["retroarch"], "support": "stable", "note": ""},
    "snes": {"name": "Super Nintendo", "kind": "file", "extensions": [".sfc", ".smc", ".zip", ".7z"],
             "emulators": ["retroarch"], "support": "stable", "note": ""},
    "n64": {"name": "Nintendo 64", "kind": "file", "extensions": [".z64", ".n64", ".v64", ".zip", ".7z"],
            "emulators": ["retroarch"], "support": "bon",
            "note": "Compatibilité variable selon les jeux (cœur Mupen64Plus-Next)."},
    "gamecube": {"name": "GameCube", "kind": "file",
                 "extensions": [".rvz", ".iso", ".gcm", ".gcz", ".ciso"],
                 "emulators": ["dolphin"], "support": "stable", "note": ""},
    "wii": {"name": "Wii", "kind": "file", "extensions": [".rvz", ".iso", ".wbfs", ".gcz", ".ciso"],
            "emulators": ["dolphin"], "support": "stable", "note": ""},
    "wiiu": {"name": "Wii U", "kind": "file", "extensions": [".wua", ".wux", ".wud", ".rpx"],
             "emulators": ["cemu"], "support": "bon", "note": "Clés requises pour les images chiffrées."},
    "nds": {"name": "Nintendo DS", "kind": "file", "extensions": [".nds"],
            "emulators": ["melonds"], "support": "stable", "note": ""},
    "3ds": {"name": "Nintendo 3DS", "kind": "file", "extensions": [".3ds", ".cci", ".cxi", ".3dsx"],
            "emulators": ["azahar"], "support": "bon", "note": "ROMs décryptées uniquement."},
    "gb": {"name": "Game Boy", "kind": "file", "extensions": [".gb", ".zip"],
           "emulators": ["mgba", "retroarch"], "support": "stable", "note": ""},
    "gbc": {"name": "Game Boy Color", "kind": "file", "extensions": [".gbc", ".zip"],
            "emulators": ["mgba", "retroarch"], "support": "stable", "note": ""},
    "gba": {"name": "Game Boy Advance", "kind": "file", "extensions": [".gba", ".zip"],
            "emulators": ["mgba", "retroarch"], "support": "stable", "note": ""},
    "megadrive": {"name": "Mega Drive / Genesis", "kind": "file",
                  "extensions": [".md", ".gen", ".smd", ".bin", ".zip", ".7z"],
                  "emulators": ["retroarch"], "support": "stable", "note": ""},
    "dreamcast": {"name": "Dreamcast", "kind": "file", "extensions": [".chd", ".gdi", ".cdi"],
                  "emulators": ["flycast"], "support": "bon", "note": ""},
    "xbox": {"name": "Xbox", "kind": "file", "extensions": [".iso", ".xiso"],
             "emulators": ["xemu"], "support": "bon",
             "note": "Images XISO uniquement ; BIOS/MCPX/disque dur de votre console requis."},
    "xbox360": {"name": "Xbox 360", "kind": "none", "extensions": [],
                "emulators": ["xenia"], "support": "non supporté",
                "note": "Non supporté nativement : Xenia est Windows uniquement (Proton, expérimental). "
                        "NEXUS ne propose ni installation ni lancement."},
    "switch": {"name": "Nintendo Switch", "kind": "none", "extensions": [], "emulators": [],
               "support": "non supporté", "note": "Aucun émulateur proposé par NEXUS."},
}


def public_emulator(key: str) -> dict:
    """Catalogue entry without the internal callable."""
    e = EMULATORS[key]
    return {"id": key, **{k: v for k, v in e.items() if k != "rom_args"},
            "direct_launch": e["rom_args"] is not None}
