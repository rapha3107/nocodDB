"""Library scan, index, covers and accent-insensitive fuzzy search."""

import pytest

from nexus.core.errors import InvalidParams
from nexus.modules.music.library import normalize
from tests.test_music_tags import id3_frame, id3_tag, make_flac, make_mp3


@pytest.fixture
def music_dir(paths):
    d = paths.home / "Music"
    make_mp3(d / "Daft Punk" / "Discovery" / "01.mp3",
             id3_tag(id3_frame("TIT2", "One More Time") + id3_frame("TPE1", "Daft Punk")
                     + id3_frame("TALB", "Discovery") + id3_frame("TCON", "Électro")))
    make_mp3(d / "Daft Punk" / "Discovery" / "02.mp3",
             id3_tag(id3_frame("TIT2", "Aerodynamic") + id3_frame("TPE1", "Daft Punk")
                     + id3_frame("TALB", "Discovery")))
    (d / "Daft Punk" / "Discovery" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    make_flac(d / "Classique" / "debussy.flac", {"TITLE": "Clair de lune", "ARTIST": "Claude Debussy",
                                                "ALBUM": "Suite bergamasque", "GENRE": "Classique"})
    make_mp3(d / "Céline Dion - Pour que tu m'aimes encore.mp3")
    (d / "notes.txt").write_text("not audio")
    (d / ".hidden").mkdir()
    make_mp3(d / ".hidden" / "secret.mp3")
    return d


@pytest.fixture
def music(make_ctx, music_dir):
    ctx = make_ctx(modules=["music"], start=False)
    m = ctx.module("music")
    m._scan_task()
    return ctx, m


def test_normalize():
    assert normalize("Éléphant  Café!") == "elephant cafe"
    assert normalize("Cœur") == "coeur"


def test_scan_indexes_audio_only(music):
    ctx, m = music
    lib = ctx.call("music.library")
    titles = sorted(t["title"] for t in lib["tracks"])
    assert titles == ["Aerodynamic", "Clair de lune", "One More Time", "Pour que tu m'aimes encore"]
    assert lib["total"] == 4
    celine = next(t for t in lib["tracks"] if t["title"].startswith("Pour"))
    assert celine["artist"] == "Céline Dion"
    assert m.library.index_path.is_file()
    assert m.library.index_path.is_relative_to(ctx.paths.cache)


def test_index_persisted_and_reused(music, make_ctx):
    ctx, m = music
    ctx2 = make_ctx(modules=["music"], start=False)
    assert len(ctx2.module("music").library.tracks) == 4


def test_custom_dirs_setting(make_ctx, paths, tmp_path):
    other = tmp_path / "ailleurs"
    make_mp3(other / "Artiste - Morceau.mp3")
    ctx = make_ctx(modules=["music"], start=False)
    ctx.settings.set("music.dirs", [str(other)])
    m = ctx.module("music")
    res = m._scan_task()
    assert res["tracks"] == 1


def test_scan_action_runs_as_task(music):
    ctx, m = music
    r = ctx.call("music.scan")
    t = ctx.tasks.wait(r["task_id"])
    assert t.state == "done" and t.result["tracks"] == 4


def test_cover_served(music):
    ctx, m = music
    lib = ctx.call("music.library", {"query": "one more time"})
    t = lib["tracks"][0]
    assert t["cover_url"] == f"/api/m/music/cover/{t['id']}"
    p = m.serve_file(f"cover/{t['id']}")
    assert p and p.name == "cover.jpg"
    assert m.serve_file("cover/../../etc/passwd") is None
    assert m.serve_file("cover/0000000000000000") is None
    flac = ctx.call("music.library", {"query": "debussy"})["tracks"][0]
    assert flac["cover_url"] is None and m.serve_file(f"cover/{flac['id']}") is None


@pytest.mark.parametrize("query,expected", [
    ("daft punk", {"One More Time", "Aerodynamic"}),
    ("DAFT", {"One More Time", "Aerodynamic"}),
    ("celine dion", {"Pour que tu m'aimes encore"}),     # accents ignored
    ("Céline", {"Pour que tu m'aimes encore"}),
    ("electro", {"One More Time"}),                       # genre « Électro »
    ("classique", {"Clair de lune"}),
    ("debusy", {"Clair de lune"}),                        # typo tolerated
    ("discovery aerodynamic", {"Aerodynamic"}),           # every word must match
    ("metallica", set()),
])
def test_fuzzy_search(music, query, expected):
    ctx, m = music
    assert {t["title"] for t in m.library.search(query)} == expected


def test_search_ranks_exact_title_first(music):
    ctx, m = music
    assert m.library.search("one more time")[0]["title"] == "One More Time"


def test_library_limit_validation(music):
    ctx, _ = music
    with pytest.raises(InvalidParams):
        ctx.call("music.library", {"limit": 0})
