"""Steam library scanner (read-only, local files only).

Reads, for every Steam root found:
  * ``steamapps/libraryfolders.vdf`` (modern ``"libraryfolders" { "0" { "path" ... } }``
    and legacy ``"LibraryFolders" { "1" "/path" }`` formats);
  * ``<library>/steamapps/appmanifest_<appid>.acf`` (appid, name, StateFlags,
    SizeOnDisk, installdir, LastPlayed);
  * ``userdata/*/config/localconfig.vdf`` for LastPlayed when the manifest has none;
  * ``config/config.vdf`` → ``CompatToolMapping`` (Proton forced per game);
  * ``appcache/librarycache`` covers (``<appid>/library_600x900.jpg``, possibly in
    a hashed sub-folder, or legacy ``<appid>_library_600x900.jpg``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import vdf

log = logging.getLogger("nexus.games.steam")

# Known non-game Steam "apps" (runtimes / compatibility tools). Appids below are
# the well-known ones; the name patterns catch the other Proton versions.
TOOL_APPIDS = {
    "228980",   # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "1493710",  # Proton Experimental
    "2180100",  # Proton Hotfix
    "1826330",  # Proton EasyAntiCheat Runtime
    "1161040",  # Proton BattlEye Runtime
}
TOOL_NAME_RE = re.compile(
    r"^(Proton(\s|$|-)|Proton\s*\d|Steam Linux Runtime|Steamworks Common Redistributables"
    r"|Steamworks SDK Redist|SteamVR Performance Test$)", re.I)

STATE_INSTALLED = 4  # StateFlags bit: fully installed


def default_roots(home: Path) -> list[Path]:
    return [
        home / ".steam/steam",
        home / ".local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]


def find_roots(home: Path) -> list[Path]:
    """Existing Steam roots, de-duplicated (~/.steam/steam is usually a symlink)."""
    seen, out = set(), []
    for r in default_roots(home):
        try:
            if not (r / "steamapps").is_dir():
                continue
            key = r.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def is_tool(appid: str, name: str) -> bool:
    return appid in TOOL_APPIDS or bool(TOOL_NAME_RE.match(name or ""))


def library_paths(root: Path) -> list[Path]:
    """Library folders declared in libraryfolders.vdf, plus the root itself."""
    paths = [root]
    f = root / "steamapps/libraryfolders.vdf"
    if f.is_file():
        try:
            doc = vdf.load_file(f)
        except (OSError, vdf.VDFError) as e:
            log.warning("libraryfolders.vdf illisible (%s): %s", f, e)
            doc = {}
        lf = vdf.get(doc, "libraryfolders", default=None)
        if not isinstance(lf, dict):
            lf = vdf.get(doc, "LibraryFolders", default={})
        for k, v in (lf or {}).items():
            if not k.isdigit():
                continue
            p = v.get("path") if isinstance(v, dict) else v
            if isinstance(p, str) and p:
                paths.append(Path(p))
    seen, out = set(), []
    for p in paths:
        try:
            key = p.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def parse_manifest(path: Path) -> dict | None:
    try:
        doc = vdf.load_file(path)
    except (OSError, vdf.VDFError) as e:
        log.warning("manifeste illisible %s: %s", path, e)
        return None
    st = vdf.get(doc, "AppState")
    if not isinstance(st, dict):
        return None
    appid = str(vdf.get(st, "appid", default="") or "")
    if not appid.isdigit():
        return None

    def _int(key):
        try:
            return int(vdf.get(st, key, default="0") or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "appid": appid,
        "name": str(vdf.get(st, "name", default="") or f"Steam {appid}"),
        "state_flags": _int("StateFlags"),
        "size": _int("SizeOnDisk") or None,
        "installdir": str(vdf.get(st, "installdir", default="") or ""),
        "last_played": _int("LastPlayed") or None,
        "last_updated": _int("LastUpdated") or None,
        "buildid": str(vdf.get(st, "buildid", default="") or ""),
    }


def _localconfig_last_played(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    ud = root / "userdata"
    if not ud.is_dir():
        return out
    for f in ud.glob("*/config/localconfig.vdf"):
        try:
            doc = vdf.load_file(f)
        except (OSError, vdf.VDFError):
            continue
        apps = vdf.get(doc, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", default={})
        if not isinstance(apps, dict):
            apps = vdf.get(doc, "UserLocalConfigStore", "Software", "Valve", "Steam", "Apps", default={})
        for appid, a in (apps or {}).items():
            if isinstance(a, dict):
                try:
                    ts = int(vdf.get(a, "LastPlayed", default="0") or 0)
                except ValueError:
                    continue
                if ts > out.get(appid, 0):
                    out[appid] = ts
    return out


def compat_tool_mapping(root: Path) -> dict[str, str]:
    """appid -> compat tool name forced by the user ("0" = default tool)."""
    f = root / "config/config.vdf"
    if not f.is_file():
        return {}
    try:
        doc = vdf.load_file(f)
    except (OSError, vdf.VDFError):
        return {}
    m = vdf.get(doc, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", default={})
    out = {}
    for appid, v in (m or {}).items():
        if isinstance(v, dict) and vdf.get(v, "name"):
            out[appid] = str(vdf.get(v, "name"))
    return out


def find_cover(root: Path, appid: str) -> Path | None:
    lc = root / "appcache/librarycache"
    candidates = [lc / appid / "library_600x900.jpg", lc / f"{appid}_library_600x900.jpg",
                  lc / appid / "library_600x900_2x.jpg", lc / f"{appid}_library_600x900_2x.jpg"]
    for c in candidates:
        if c.is_file():
            return c
    d = lc / appid
    if d.is_dir():  # newer clients may store art in a hashed sub-folder
        for c in sorted(d.glob("*/library_600x900.jpg")):
            if c.is_file():
                return c
    return None


def compat_tools(root: Path, libraries: list[Path]) -> list[str]:
    """Installed Proton/compat tools: official (steamapps/common/Proton*) and custom (compatibilitytools.d)."""
    names = set()
    for base in [root / "compatibilitytools.d", root.parent / "root/compatibilitytools.d"]:
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir():
                    names.add(d.name)
    for lib in libraries:
        common = lib / "steamapps/common"
        if common.is_dir():
            for d in common.glob("Proton*"):
                if d.is_dir():
                    names.add(d.name)
    return sorted(names)


def scan(home: Path) -> dict:
    """Returns {"roots", "libraries", "games": [raw dicts], "covers": {appid: Path}}."""
    roots = find_roots(home)
    games: dict[str, dict] = {}
    covers: dict[str, Path] = {}
    libs_all: list[str] = []
    for root in roots:
        libs = library_paths(root)
        lp = _localconfig_last_played(root)
        mapping = compat_tool_mapping(root)
        for lib in libs:
            libs_all.append(str(lib))
            sa = lib / "steamapps"
            if not sa.is_dir():
                continue
            for mf in sorted(sa.glob("appmanifest_*.acf")):
                m = parse_manifest(mf)
                if not m or is_tool(m["appid"], m["name"]):
                    continue
                appid = m["appid"]
                if appid in games:
                    continue
                m["installed"] = bool(m["state_flags"] & STATE_INSTALLED)
                m["library"] = str(lib)
                m["install_path"] = str(sa / "common" / m["installdir"]) if m["installdir"] else None
                m["manifest"] = str(mf)
                m["root"] = str(root)
                m["compat_tool"] = mapping.get(appid)
                if not m["last_played"] and lp.get(appid):
                    m["last_played"] = lp[appid]
                games[appid] = m
                cov = find_cover(root, appid)
                if cov:
                    covers[appid] = cov
    return {"roots": [str(r) for r in roots], "libraries": libs_all,
            "games": list(games.values()), "covers": covers}
