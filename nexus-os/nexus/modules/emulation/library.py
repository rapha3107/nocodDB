"""ROM library: scanning, title cleaning, PARAM.SFO parsing, cover lookup.

Layout: <rom_dir>/<system>/... (sub-folders allowed). `<rom_dir>/<system>/media/`
is reserved for artwork and never scanned as games.
A ROM id is "<system>/<path relative to <rom_dir>/<system>>" so that `launch`
can re-validate it with `safe_join` without trusting any cached state.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path

from .catalog import SYSTEMS

COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MAX_DEPTH = 4
MAX_ENTRIES = 50_000

# ------------------------------------------------------------------ titles

_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_REGIONS = {"europe": "Europe", "usa": "USA", "japan": "Japon", "world": "Monde", "france": "France",
            "germany": "Allemagne", "spain": "Espagne", "italy": "Italie", "korea": "Corée",
            "asia": "Asie", "australia": "Australie", "brazil": "Brésil", "eu": "Europe", "us": "USA",
            "jp": "Japon", "pal": "PAL", "ntsc-u": "USA", "ntsc-j": "Japon"}
_DISC_RE = re.compile(r"\((?:disc|disk|cd)\s*(\d+)(?:\s*of\s*\d+)?\)", re.I)


def clean_title(filename: str) -> dict:
    """'Final Fantasy VII (Europe) (Disc 1) [!].chd' -> title/region/disc."""
    stem = filename
    for ext in sorted({e for s in SYSTEMS.values() for e in s["extensions"]}, key=len, reverse=True):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    region = None
    for tag in re.findall(r"\(([^)]*)\)", stem):
        for part in re.split(r"\s*,\s*", tag):
            if part.lower() in _REGIONS:
                region = region or _REGIONS[part.lower()]
    m = _DISC_RE.search(stem)
    disc = int(m.group(1)) if m else None
    title = _TAG_RE.sub("", stem).replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip(" -.")
    # "Legend of Zelda, The - ..." -> "The Legend of Zelda - ..."
    m2 = re.match(r"^(.*?), (The|A|An|Le|La|Les|L')(\s+-\s+.*)?$", title)
    if m2:
        art = m2.group(2)
        title = f"{art}{'' if art.endswith(chr(39)) else ' '}{m2.group(1)}{m2.group(3) or ''}"
    return {"title": title or filename, "region": region, "disc": disc}


# ------------------------------------------------------------------ PARAM.SFO

SFO_MAGIC = b"\x00PSF"


def parse_sfo(data: bytes) -> dict:
    """Parse a PlayStation PARAM.SFO (PS3/PS4/PSP/Vita) into {key: value}.

    Header (little-endian): magic "\\0PSF", version u32, key_table u32,
    data_table u32, entries u32. Index entries (16 bytes): key_offset u16,
    fmt u16 (0x0004 utf8-S, 0x0204 utf8 NUL-terminated, 0x0404 u32),
    len u32, max_len u32, data_offset u32.
    """
    if len(data) < 20 or data[:4] != SFO_MAGIC:
        raise ValueError("PARAM.SFO invalide (signature)")
    _ver, key_table, data_table, count = struct.unpack_from("<IIII", data, 4)
    if count > 1024 or 20 + count * 16 > len(data) or key_table > len(data) or data_table > len(data):
        raise ValueError("PARAM.SFO invalide (en-tête)")
    out: dict = {}
    for i in range(count):
        koff, fmt, length, _maxlen, doff = struct.unpack_from("<HHIII", data, 20 + i * 16)
        ks = key_table + koff
        ke = data.find(b"\x00", ks)
        if ks >= len(data) or ke < 0:
            raise ValueError("PARAM.SFO invalide (clé)")
        key = data[ks:ke].decode("utf-8", "replace")
        vs = data_table + doff
        raw = data[vs: vs + length]
        if len(raw) != length:
            raise ValueError("PARAM.SFO invalide (donnée)")
        if fmt == 0x0404:
            out[key] = struct.unpack("<I", raw[:4])[0] if length >= 4 else 0
        else:
            out[key] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return out


def read_sfo(path: Path) -> dict:
    try:
        if path.stat().st_size > 1 << 20:
            return {}
        return parse_sfo(path.read_bytes())
    except (OSError, ValueError, struct.error):
        return {}


# ------------------------------------------------------------------ scanning

def _cue_refs(cue: Path) -> set[str]:
    refs = set()
    try:
        for line in cue.read_text(errors="replace").splitlines()[:500]:
            m = re.match(r'\s*FILE\s+"?(.+?)"?\s+\w+\s*$', line, re.I)
            if m:
                refs.add(m.group(1).lower())
    except OSError:
        pass
    return refs


def _m3u_refs(m3u: Path) -> set[str]:
    try:
        return {l.strip().lower() for l in m3u.read_text(errors="replace").splitlines()[:200]
                if l.strip() and not l.startswith("#")}
    except OSError:
        return set()


def find_cover(system_dir: Path, rom: Path, stem: str) -> Path | None:
    cands = []
    for ext in COVER_EXTS:
        cands += [rom.with_suffix(ext), rom.parent / (rom.name + ext), system_dir / "media" / "covers" / (stem + ext)]
    if rom.is_dir():
        cands += [rom / "PS3_GAME" / "ICON0.PNG", rom / "ICON0.PNG", rom / "sce_sys" / "icon0.png"]
    for c in cands:
        if c.is_file():
            return c
    return None


def _console_dir_entry(system: str, game_dir: Path, sfo_path: Path) -> dict:
    sfo = read_sfo(sfo_path)
    title = sfo.get("TITLE") or clean_title(game_dir.name)["title"]
    return {"title": title, "title_id": sfo.get("TITLE_ID") or None, "region": None, "disc": None,
            "format": "dossier"}


def _detect_dir_game(system: str, d: Path) -> Path | None:
    if system == "ps3":
        for sfo in (d / "PS3_GAME" / "PARAM.SFO", d / "PARAM.SFO"):
            if sfo.is_file() and (sfo.parent / "USRDIR").is_dir():
                return sfo
    elif system == "ps4":
        sfo = d / "sce_sys" / "param.sfo"
        if sfo.is_file():
            return sfo
    return None


def scan_system(rom_dir: Path, system: str) -> list[dict]:
    spec = SYSTEMS[system]
    sdir = rom_dir / system
    if spec["kind"] == "none" or not sdir.is_dir():
        return []
    base = sdir.resolve()
    exts = set(spec["extensions"])
    found: list[tuple[Path, dict]] = []
    seen = 0

    def walk(d: Path, depth: int):
        nonlocal seen
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        files = []
        for e in entries:
            seen += 1
            if seen > MAX_ENTRIES or e.name.startswith("."):
                continue
            p = Path(e.path)
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp != base and base not in rp.parents:
                continue  # symlink escaping the ROM folder
            if e.is_dir():
                if depth == 0 and e.name == "media":
                    continue
                if spec["kind"] in ("ps3dir", "ps4dir"):
                    sfo = _detect_dir_game(system, p)
                    if sfo:
                        found.append((p, _console_dir_entry(system, p, sfo)))
                        continue
                if depth < MAX_DEPTH:
                    walk(p, depth + 1)
            elif e.is_file() and spec["kind"] == "file" and p.suffix.lower() in exts:
                files.append(p)
        # Hide tracks referenced by a .cue / discs referenced by a .m3u.
        hidden: set[str] = set()
        for f in files:
            if f.suffix.lower() == ".cue":
                hidden |= _cue_refs(f)
            elif f.suffix.lower() == ".m3u":
                hidden |= _m3u_refs(f)
        for f in files:
            if f.name.lower() in hidden:
                continue
            found.append((f, {**clean_title(f.name), "title_id": None, "format": f.suffix.lower().lstrip(".")}))

    walk(sdir, 0)
    out = []
    for p, meta in found:
        rel = p.relative_to(sdir).as_posix()
        cover = find_cover(sdir, p, p.stem if p.is_file() else p.name)
        try:
            size = p.stat().st_size if p.is_file() else None
        except OSError:
            size = None
        out.append({
            "id": f"{system}/{rel}", "system": system, "path": str(p), "file": rel, "size": size,
            **meta,
            "cover": f"cover/{system}/{cover.relative_to(sdir).as_posix()}" if cover else None,
        })
    return out
