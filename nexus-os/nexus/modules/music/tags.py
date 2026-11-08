"""Minimal, defensive audio tag readers (pure Python, no dependency).

Supported:
  * ID3v2.2 / v2.3 / v2.4 text frames (title, artist, album, genre) + ID3v1 fallback;
  * FLAC Vorbis comments + duration from STREAMINFO;
  * Ogg Vorbis / Ogg Opus comment headers;
  * fallback on the file name ("Artiste - Titre", leading track numbers stripped).

Every reader is bounded (never reads a whole file into memory) and returns an
empty dict on malformed input instead of raising: a broken file must never
stop a library scan.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

MAX_ID3_BYTES = 4 * 1024 * 1024     # text frames come first; huge embedded art is cut
MAX_BLOCK_BYTES = 1 * 1024 * 1024   # a Vorbis comment block bigger than this is ignored
MAX_OGG_SCAN = 512 * 1024

_ID3_FRAMES = {
    # v2.3/v2.4         v2.2
    "TIT2": "title", "TT2": "title",
    "TPE1": "artist", "TP1": "artist",
    "TALB": "album", "TAL": "album",
    "TCON": "genre", "TCO": "genre",
    "TPE2": "albumartist", "TP2": "albumartist",
    "TRCK": "track", "TRK": "track",
}
_VORBIS_KEYS = {"TITLE": "title", "ARTIST": "artist", "ALBUM": "album", "GENRE": "genre",
                "ALBUMARTIST": "albumartist", "TRACKNUMBER": "track"}


def _syncsafe(b: bytes) -> int:
    return (b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 | (b[2] & 0x7F) << 7 | (b[3] & 0x7F)


def _unsync(data: bytes) -> bytes:
    return data.replace(b"\xff\x00", b"\xff")


def _clean(s: str) -> str:
    s = s.replace("\x00", " ").strip()
    return re.sub(r"\s+", " ", s)[:300]


def _decode_text(data: bytes) -> str:
    if not data:
        return ""
    enc, body = data[0], data[1:]
    try:
        if enc == 0:
            parts = body.split(b"\x00")
            return parts[0].decode("latin-1")
        if enc == 3:
            return body.split(b"\x00")[0].decode("utf-8", "replace")
        # UTF-16 (1 = with BOM, 2 = big-endian): split on aligned double NUL
        codec = "utf-16" if enc == 1 else "utf-16-be"
        for i in range(0, len(body) - 1, 2):
            if body[i:i + 2] == b"\x00\x00":
                body = body[:i]
                break
        if enc == 1 and not body.startswith((b"\xff\xfe", b"\xfe\xff")):
            codec = "utf-16-le"
        return body.decode(codec, "replace").lstrip("﻿")
    except (UnicodeDecodeError, LookupError):
        return ""


def _genre(value: str) -> str:
    # ID3 "(13)" / "(13)Pop" / "13" numeric references: keep only free text
    v = re.sub(r"^\(\d+\)", "", value).strip()
    if re.fullmatch(r"\d+", v) or v in ("(RX)", "(CR)"):
        return ""
    return v


def read_id3v2(f) -> dict:
    f.seek(0)
    head = f.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        return {}
    major, flags = head[3], head[5]
    if major not in (2, 3, 4):
        return {}
    size = _syncsafe(head[6:10])
    data = f.read(min(size, MAX_ID3_BYTES))
    if major < 4 and flags & 0x80:
        data = _unsync(data)
    pos = 0
    if flags & 0x40 and major >= 3:  # extended header
        if len(data) < 4:
            return {}
        ext = _syncsafe(data[:4]) if major == 4 else struct.unpack(">I", data[:4])[0] + 4
        pos = ext
    out: dict = {}
    id_len, hdr_len = (3, 6) if major == 2 else (4, 10)
    while pos + hdr_len <= len(data):
        fid = data[pos:pos + id_len]
        if not fid.strip(b"\x00") or not re.fullmatch(rb"[A-Z0-9]+", fid):
            break  # padding or garbage
        if major == 2:
            fsize = int.from_bytes(data[pos + 3:pos + 6], "big")
            fflags = 0
        elif major == 3:
            fsize = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            fflags = struct.unpack(">H", data[pos + 8:pos + 10])[0]
        else:
            fsize = _syncsafe(data[pos + 4:pos + 8])
            fflags = struct.unpack(">H", data[pos + 8:pos + 10])[0]
        body = data[pos + hdr_len:pos + hdr_len + fsize]
        pos += hdr_len + fsize
        if fsize <= 0:
            continue
        if len(body) < fsize:
            break  # truncated (tag cut at MAX_ID3_BYTES)
        key = _ID3_FRAMES.get(fid.decode("ascii"))
        if not key or key in out:
            continue
        if major == 3 and fflags & 0x00C0:      # compressed / encrypted
            continue
        if major == 4:
            if fflags & 0x000C:                 # compressed / encrypted
                continue
            if fflags & 0x0002:                 # frame-level unsynchronisation
                body = _unsync(body)
            if fflags & 0x0001:                 # data length indicator
                body = body[4:]
        text = _clean(_decode_text(body))
        if key == "genre":
            text = _genre(text)
        if text:
            out[key] = text
    return out


def read_id3v1(f) -> dict:
    try:
        f.seek(-128, 2)
    except OSError:
        return {}
    tag = f.read(128)
    if len(tag) != 128 or tag[:3] != b"TAG":
        return {}
    out = {}
    for key, a, b in (("title", 3, 33), ("artist", 33, 63), ("album", 63, 93)):
        v = _clean(tag[a:b].split(b"\x00")[0].decode("latin-1"))
        if v:
            out[key] = v
    return out


def _parse_vorbis_comment(data: bytes) -> dict:
    out: dict = {}
    try:
        vlen = struct.unpack("<I", data[:4])[0]
        pos = 4 + vlen
        count = struct.unpack("<I", data[pos:pos + 4])[0]
        pos += 4
        for _ in range(min(count, 1000)):
            if pos + 4 > len(data):
                break
            clen = struct.unpack("<I", data[pos:pos + 4])[0]
            pos += 4
            raw = data[pos:pos + clen]
            pos += clen
            if len(raw) < clen:
                break
            k, sep, v = raw.decode("utf-8", "replace").partition("=")
            key = _VORBIS_KEYS.get(k.upper())
            if sep and key and key not in out:
                v = _clean(v)
                if v:
                    out[key] = v
    except struct.error:
        pass
    return out


def read_flac(f) -> dict:
    f.seek(0)
    head = f.read(4)
    if head[:3] == b"ID3":  # ID3 tag before fLaC (rare, non-standard)
        rest = f.read(6)
        f.seek(10 + _syncsafe(rest[2:6]))
        head = f.read(4)
    if head != b"fLaC":
        return {}
    out: dict = {}
    for _ in range(128):
        bh = f.read(4)
        if len(bh) < 4:
            break
        last, btype = bh[0] & 0x80, bh[0] & 0x7F
        blen = int.from_bytes(bh[1:4], "big")
        if btype == 0 and blen >= 18:          # STREAMINFO
            si = f.read(blen)
            if len(si) >= 18:
                v = int.from_bytes(si[10:18], "big")
                rate = v >> 44
                total = v & ((1 << 36) - 1)
                if rate and total:
                    out["duration"] = round(total / rate, 2)
        elif btype == 4 and blen <= MAX_BLOCK_BYTES:  # VORBIS_COMMENT
            out.update({k: v for k, v in _parse_vorbis_comment(f.read(blen)).items() if k not in out})
        else:
            f.seek(blen, 1)
        if last:
            break
    return out


def _ogg_packets(f, wanted: int = 2):
    """Yield the first `wanted` logical packets of the first Ogg stream (bounded)."""
    f.seek(0)
    packets, cur, read = [], b"", 0
    while len(packets) < wanted and read < MAX_OGG_SCAN:
        hdr = f.read(27)
        if len(hdr) < 27 or hdr[:4] != b"OggS":
            break
        nseg = hdr[26]
        table = f.read(nseg)
        read += 27 + nseg
        for lace in table:
            seg = f.read(lace)
            read += lace
            cur += seg
            if lace < 255:
                packets.append(cur)
                cur = b""
                if len(packets) >= wanted:
                    break
            if len(cur) > MAX_BLOCK_BYTES:
                return packets
    return packets


def read_ogg(f) -> dict:
    pkts = _ogg_packets(f)
    if len(pkts) < 2:
        return {}
    p = pkts[1]
    if p.startswith(b"\x03vorbis"):
        return _parse_vorbis_comment(p[7:])
    if p.startswith(b"OpusTags"):
        return _parse_vorbis_comment(p[8:])
    return {}


_TRACKNO = re.compile(r"^\s*(?:\d{1,3}[\s._-]+|\(\d{1,3}\)\s*)")


def from_filename(path: Path) -> dict:
    stem = path.stem.replace("_", " ").strip()
    stem = _TRACKNO.sub("", stem).strip() or path.stem
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        artist, title = _TRACKNO.sub("", artist).strip(), title.strip()
        if artist and title:
            return {"artist": artist, "title": title}
    return {"title": stem}


def read_tags(path: Path) -> dict:
    """Best-effort metadata: {title, artist, album, genre, duration?}. Never raises."""
    ext = path.suffix.lower()
    tags: dict = {}
    try:
        with path.open("rb") as f:
            if ext == ".flac":
                tags = read_flac(f)
            elif ext in (".ogg", ".opus", ".oga"):
                tags = read_ogg(f)
            elif ext in (".mp3", ".wav", ".m4a"):
                # ID3v2 at the start is common for mp3 (and occasionally other containers)
                tags = read_id3v2(f)
                if ext == ".mp3" and not tags.get("title"):
                    tags = {**read_id3v1(f), **tags}
    except (OSError, ValueError, IndexError, struct.error):
        tags = {}
    fallback = from_filename(path)
    if not tags.get("title"):
        tags["title"] = fallback["title"]
        if "artist" in fallback and not tags.get("artist"):
            tags["artist"] = fallback["artist"]
    if not tags.get("artist") and tags.get("albumartist"):
        tags["artist"] = tags["albumartist"]
    return tags
