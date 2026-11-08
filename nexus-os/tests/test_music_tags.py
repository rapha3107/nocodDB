"""Pure-Python tag parsers: real tiny binary files are generated here."""

import struct
from pathlib import Path

from nexus.modules.music.tags import from_filename, read_tags


def syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3_frame(fid: str, text: str, major: int = 3, enc: int | None = None) -> bytes:
    if enc is None:
        enc = 1 if major == 3 else 3
    if enc == 0:
        body = b"\x00" + text.encode("latin-1")
    elif enc == 1:
        body = b"\x01" + text.encode("utf-16")          # with BOM
    elif enc == 2:
        body = b"\x02" + text.encode("utf-16-be")
    else:
        body = b"\x03" + text.encode("utf-8")
    if major == 2:
        return fid.encode() + len(body).to_bytes(3, "big") + body
    size = syncsafe(len(body)) if major == 4 else struct.pack(">I", len(body))
    return fid.encode() + size + b"\x00\x00" + body


def id3_tag(frames: bytes, major: int = 3, padding: int = 64) -> bytes:
    payload = frames + b"\x00" * padding
    return b"ID3" + bytes([major, 0, 0]) + syncsafe(len(payload)) + payload


def make_mp3(path: Path, tag: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 400)  # fake MPEG frame data
    return path


def vorbis_comment(comments: dict, vendor: str = "nexus-test") -> bytes:
    out = struct.pack("<I", len(vendor)) + vendor.encode() + struct.pack("<I", len(comments))
    for k, v in comments.items():
        c = f"{k}={v}".encode()
        out += struct.pack("<I", len(c)) + c
    return out


def make_flac(path: Path, comments: dict, rate: int = 44100, samples: int = 44100 * 125) -> Path:
    v = (rate << 44) | (1 << 41) | (15 << 36) | samples
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + v.to_bytes(8, "big") + b"\x00" * 16
    assert len(streaminfo) == 34
    picture = b"\x00" * 100  # some block we must skip
    vc = vorbis_comment(comments)
    data = b"fLaC"
    data += bytes([0]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    data += bytes([6]) + len(picture).to_bytes(3, "big") + picture
    data += bytes([0x80 | 4]) + len(vc).to_bytes(3, "big") + vc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + b"\xff\xf8" + b"\x00" * 64)
    return path


def ogg_page(packet: bytes, seq: int, htype: int = 0) -> bytes:
    lacing = [255] * (len(packet) // 255) + [len(packet) % 255]
    hdr = struct.pack("<4sBBqIIIB", b"OggS", 0, htype, 0, 1234, seq, 0, len(lacing))
    return hdr + bytes(lacing) + packet


def make_ogg(path: Path, comments: dict, opus: bool = False) -> Path:
    if opus:
        ident = b"OpusHead" + b"\x01\x02" + b"\x00" * 9
        tags = b"OpusTags" + vorbis_comment(comments)
    else:
        ident = b"\x01vorbis" + b"\x00" * 23
        tags = b"\x03vorbis" + vorbis_comment(comments) + b"\x01"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ogg_page(ident, 0, 2) + ogg_page(tags, 1))
    return path


def test_id3v23_utf16(tmp_path):
    tag = id3_tag(id3_frame("TIT2", "Été indien") + id3_frame("TPE1", "Joe Dassin")
                  + id3_frame("TALB", "Le Jardin du Luxembourg") + id3_frame("TCON", "(12)Chanson"))
    t = read_tags(make_mp3(tmp_path / "a.mp3", tag))
    assert t["title"] == "Été indien"
    assert t["artist"] == "Joe Dassin"
    assert t["album"] == "Le Jardin du Luxembourg"
    assert t["genre"] == "Chanson"


def test_id3v24_utf8_and_utf16be(tmp_path):
    tag = id3_tag(id3_frame("TIT2", "Ça plane pour moi", 4) + id3_frame("TPE1", "Plastic Bertrand", 4, enc=2)
                  + id3_frame("TALB", "AN1", 4, enc=0), major=4)
    t = read_tags(make_mp3(tmp_path / "b.mp3", tag))
    assert (t["title"], t["artist"], t["album"]) == ("Ça plane pour moi", "Plastic Bertrand", "AN1")


def test_id3v22(tmp_path):
    tag = id3_tag(id3_frame("TT2", "Old", 2, enc=0) + id3_frame("TP1", "Format", 2, enc=0), major=2)
    t = read_tags(make_mp3(tmp_path / "c.mp3", tag))
    assert t["title"] == "Old" and t["artist"] == "Format"


def test_id3v1_fallback(tmp_path):
    p = make_mp3(tmp_path / "d.mp3")
    v1 = b"TAG" + b"Titre v1".ljust(30, b"\x00") + b"Artiste v1".ljust(30, b"\x00") + \
        b"Album v1".ljust(30, b"\x00") + b"2001" + b"\x00" * 30 + b"\x00"
    p.write_bytes(p.read_bytes() + v1)
    t = read_tags(p)
    assert (t["title"], t["artist"], t["album"]) == ("Titre v1", "Artiste v1", "Album v1")


def test_flac_vorbis_comments_and_duration(tmp_path):
    p = make_flac(tmp_path / "x.flac", {"TITLE": "Clair de lune", "ARTIST": "Claude Debussy",
                                         "album": "Suite bergamasque", "GENRE": "Classique"})
    t = read_tags(p)
    assert t["title"] == "Clair de lune"
    assert t["artist"] == "Claude Debussy"
    assert t["album"] == "Suite bergamasque"   # keys are case-insensitive
    assert t["genre"] == "Classique"
    assert t["duration"] == 125.0


def test_ogg_vorbis_and_opus(tmp_path):
    t = read_tags(make_ogg(tmp_path / "v.ogg", {"TITLE": "Vorbis song", "ARTIST": "Xiph"}))
    assert t["title"] == "Vorbis song" and t["artist"] == "Xiph"
    t = read_tags(make_ogg(tmp_path / "o.opus", {"TITLE": "Opus song", "ARTIST": "Xiph"}, opus=True))
    assert t["title"] == "Opus song"


def test_filename_fallback():
    assert from_filename(Path("03 - Daft Punk - One More Time.mp3")) == {"artist": "Daft Punk", "title": "One More Time"}
    assert from_filename(Path("Stromae - Alors on danse.wav")) == {"artist": "Stromae", "title": "Alors on danse"}
    assert from_filename(Path("07. Intro.flac")) == {"title": "Intro"}


def test_untagged_file_uses_filename(tmp_path):
    p = tmp_path / "Angèle - Balance ton quoi.m4a"
    p.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 100)
    t = read_tags(p)
    assert t["artist"] == "Angèle" and t["title"] == "Balance ton quoi"


def test_malformed_files_never_raise(tmp_path):
    cases = {
        "trunc.mp3": b"ID3\x03\x00\x00\x00\x00\x10\x00TIT2\x00\x00\xff\xff",  # frame size beyond data
        "garbage.mp3": b"ID3\x09\x00\x00\xff\xff\xff\xff",
        "bad.flac": b"fLaC\x84\xff\xff\xff\x01\x02",
        "empty.ogg": b"",
        "notogg.opus": b"OggS\x00",
        "random.flac": bytes(range(256)) * 4,
    }
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        t = read_tags(p)
        assert t["title"]  # falls back to the file name
