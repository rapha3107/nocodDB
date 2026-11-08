"""Playback through a fake mpv IPC server on a real UNIX socket, radio, volume, playlists."""

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from nexus.core.errors import CommandFailed, ConfirmationRequired, InvalidParams, NotFound, Unavailable
from nexus.core.net import FakeNet
from nexus.modules.music.mpv import MpvIpc
from nexus.modules.music.testing import FakeMpvServer
from nexus.security.permissions import Origin
from nexus.security.runner import Result
from tests.test_music_tags import id3_frame, id3_tag, make_mp3

UUID = "96202f73-0601-11e8-ae97-52543be04c81"
UUID2 = "9617a958-0601-11e8-ae97-52543be04c81"


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sockdir():
    # sun_path is limited to 108 bytes: pytest's tmp_path can be longer.
    d = tempfile.mkdtemp(prefix="nxm", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def lib(paths):
    d = paths.home / "Music"
    make_mp3(d / "Daft Punk" / "01.mp3", id3_tag(id3_frame("TIT2", "One More Time") + id3_frame("TPE1", "Daft Punk")))
    make_mp3(d / "Daft Punk" / "02.mp3", id3_tag(id3_frame("TIT2", "Aerodynamic") + id3_frame("TPE1", "Daft Punk")))
    make_mp3(d / "Zaz - Je veux.mp3")
    return d


@pytest.fixture
def music(make_ctx, lib, sockdir):
    ctx = make_ctx(modules=["music"], start=False)
    m = ctx.module("music")
    m._scan_task()
    m.player.set_socket_path(sockdir / "mpv.sock")
    m.start()
    return ctx, m


@pytest.fixture
def mpv(music, sockdir):
    srv = FakeMpvServer(sockdir / "mpv.sock").start()
    yield srv
    srv.close()


# -------------------------------------------------------------- IPC protocol
def test_ipc_client_protocol(mpv):
    ipc = MpvIpc(mpv.path)
    ipc.set_property("volume", 42)
    assert ipc.get_property("volume") == 42
    assert ipc.get_property("time-pos") is None        # "property unavailable"
    ipc.loadfile("/music/a.mp3")
    ipc.loadfile("/music/b.mp3", "append")
    ipc.playlist_next()
    ipc.playlist_prev()
    ipc.set_property("pause", True)
    ipc.stop()
    assert mpv.commands() == [
        ["set_property", "volume", 42], ["get_property", "volume"], ["get_property", "time-pos"],
        ["loadfile", "/music/a.mp3", "replace"], ["loadfile", "/music/b.mp3", "append"],
        ["playlist-next"], ["playlist-prev"], ["set_property", "pause", True], ["stop"]]


def test_ipc_wire_format(sockdir):
    """Check the raw bytes: one JSON object per line with a request_id."""
    import socket
    import threading
    path = sockdir / "raw.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    got = {}

    def serve():
        c, _ = srv.accept()
        line = c.makefile("rb").readline()
        got["line"] = line
        req = json.loads(line)
        c.sendall(b'{"event":"idle"}\n' + json.dumps({"request_id": req["request_id"], "error": "success",
                                                       "data": "Titre"}).encode() + b"\n")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    assert MpvIpc(path).get_property("media-title") == "Titre"   # skips unrelated events
    assert got["line"].endswith(b"\n")
    msg = json.loads(got["line"])
    assert msg["command"] == ["get_property", "media-title"] and isinstance(msg["request_id"], int)
    srv.close()


# -------------------------------------------------------------- availability
def test_play_without_mpv_is_unavailable(music, runner):
    ctx, m = music
    runner.binaries.discard("mpv")   # and no io.mpv.Mpv flatpak (flatpak list is empty)
    with pytest.raises(Unavailable) as e:
        ctx.call("music.play", {"query": "daft"})
    assert "mpv" in e.value.message
    assert ctx.call("music.status")["state"] == "stopped"
    assert m.status()["ok"] is False
    with pytest.raises(Unavailable):
        ctx.call("music.radio_play", {"station_id": UUID})


def test_spawns_mpv_with_ipc_args(music, runner, sockdir):
    ctx, m = music
    servers = []

    def spawn(argv, env=None):
        runner.spawned.append(list(argv))
        servers.append(FakeMpvServer(sockdir / "mpv.sock").start())
        return 999

    runner.spawn = spawn
    try:
        ctx.call("music.play", {"query": "zaz"})
        argv = runner.spawned[0]
        assert argv[0] == "mpv"
        assert f"--input-ipc-server={sockdir / 'mpv.sock'}" in argv
        for flag in ("--no-video", "--idle=yes", "--really-quiet", "--ytdl=no"):
            assert flag in argv
        assert "--volume=70" in argv
        assert servers[0].commands("loadfile")[0][1].endswith("Zaz - Je veux.mp3")
        # second call reuses the running instance
        ctx.call("music.pause")
        assert len(runner.spawned) == 1
    finally:
        for s in servers:
            s.close()


def test_flatpak_fallback_argv(music, runner):
    ctx, m = music
    runner.binaries.discard("mpv")
    runner.script(["flatpak", "list"], stdout="io.mpv.Mpv\tmpv\t0.39\tflathub\n")
    assert m.player.backend() == "flatpak"
    argv = m.player.argv("flatpak")
    assert argv[:3] == ["flatpak", "run", "io.mpv.Mpv"]
    from nexus.security.runner import validate_argv
    validate_argv(argv)  # accepted by the core flatpak validator


def test_flatpak_socket_not_reachable(music, runner):
    ctx, m = music
    runner.binaries.discard("mpv")
    runner.script(["flatpak", "list"], stdout="io.mpv.Mpv\tmpv\t0.39\tflathub\n")
    m.player.start_timeout = 0.2
    with pytest.raises(Unavailable) as e:
        ctx.call("music.play", {"query": "zaz"})
    assert e.value.details["reason"] == "no_socket_flatpak"
    assert runner.spawned[0][:3] == ["flatpak", "run", "io.mpv.Mpv"]


# -------------------------------------------------------------- playback
def test_play_query_loads_matching_tracks(music, mpv):
    ctx, m = music
    st = ctx.call("music.play", {"query": "daft punk"}, origin=Origin.AI)
    loads = mpv.commands("loadfile")
    assert [Path(c[1]).name for c in loads] == ["01.mp3", "02.mp3"]
    assert [c[2] for c in loads] == ["replace", "append"]
    assert ["set_property", "pause", False] in mpv.commands()
    assert st["state"] == "playing" and st["source"] == "local"
    assert st["track"]["title"] == "One More Time" and st["track"]["artist"] == "Daft Punk"
    assert st["duration"] == 200.0 and st["queue_length"] == 2


def test_play_no_match_is_not_found(music, mpv):
    ctx, _ = music
    with pytest.raises(NotFound) as e:
        ctx.call("music.play", {"query": "metallica"})
    assert "metallica" in e.value.message and "radio" in e.value.message
    assert mpv.commands("loadfile") == []


def test_play_track_id_and_conflicting_params(music, mpv):
    ctx, m = music
    tid = ctx.call("music.library", {"query": "aerodynamic"})["tracks"][0]["id"]
    ctx.call("music.play", {"track_id": tid})
    assert Path(mpv.commands("loadfile")[0][1]).name == "02.mp3"
    with pytest.raises(NotFound):
        ctx.call("music.play", {"track_id": "nope"})
    with pytest.raises(InvalidParams):
        ctx.call("music.play", {"track_id": tid, "query": "zaz"})


def test_play_without_params_shuffles_all_then_resumes(music, mpv):
    ctx, _ = music
    ctx.call("music.play")
    assert len(mpv.commands("loadfile")) == 3
    ctx.call("music.pause")
    assert mpv.props["pause"] is True
    assert ctx.call("music.status")["state"] == "paused"
    st = ctx.call("music.play")              # resumes, does not reload
    assert len(mpv.commands("loadfile")) == 3
    assert st["state"] == "playing"


def test_empty_library(make_ctx, sockdir):
    ctx = make_ctx(modules=["music"], start=False)
    m = ctx.module("music")
    m.player.set_socket_path(sockdir / "mpv.sock")
    srv = FakeMpvServer(sockdir / "mpv.sock").start()
    try:
        with pytest.raises(NotFound) as e:
            ctx.call("music.play")
        assert "vide" in e.value.message
    finally:
        srv.close()


def test_controls(music, mpv):
    ctx, _ = music
    ctx.call("music.play", {"query": "daft punk"})
    assert ctx.call("music.next")["track"]["title"] == "Aerodynamic"
    with pytest.raises(NotFound):
        ctx.call("music.next")
    assert ctx.call("music.previous")["track"]["title"] == "One More Time"
    ctx.call("music.pause")
    ctx.call("music.resume")
    assert mpv.props["pause"] is False
    st = ctx.call("music.stop")
    assert ["stop"] in mpv.commands() and st["state"] == "stopped"


def test_controls_when_not_running(music):
    ctx, _ = music
    assert ctx.call("music.pause")["state"] == "stopped"
    with pytest.raises(NotFound):
        ctx.call("music.next")


def test_state_events_published(music, mpv):
    ctx, _ = music
    q = ctx.events.subscribe()
    ctx.call("music.play", {"query": "zaz"})
    ctx.call("music.pause")
    states = []
    end = time.time() + 2
    while time.time() < end and "paused" not in states:
        try:
            ev = q.get(timeout=0.1)
        except Exception:
            continue
        if ev["topic"] == "music.state":
            states.append(ev["data"]["state"])
    assert "playing" in states and "paused" in states


def test_mpv_error_maps_to_command_failed(music, mpv):
    ctx, _ = music
    mpv.fail.add("loadfile")
    with pytest.raises(CommandFailed):
        ctx.call("music.play", {"query": "zaz"})


# -------------------------------------------------------------- volume
def test_volume(music, mpv):
    ctx, _ = music
    assert ctx.call("music.volume", {"level": 55}) == {"volume": 55, "applied": True}
    assert ["set_property", "volume", 55] in mpv.commands()
    assert ctx.settings.get("music.volume") == 55
    assert ctx.call("music.volume_step", {"delta": 10})["volume"] == 65
    assert ctx.call("music.volume_step", {"delta": 100})["volume"] == 100
    assert ctx.call("music.volume_step", {"delta": -100})["volume"] == 0


@pytest.mark.parametrize("bad", [101, -1, "50", 50.0, True])
def test_volume_validation(music, bad):
    ctx, _ = music
    for act in ("music.volume", "music.system_volume"):
        with pytest.raises(InvalidParams):
            ctx.call(act, {"level": bad})
    with pytest.raises(InvalidParams):
        ctx.call("music.volume_step", {"delta": 101 if bad != -1 else -101})


def test_volume_without_mpv_is_saved(music):
    ctx, _ = music
    assert ctx.call("music.volume", {"level": 20}) == {"volume": 20, "applied": False}
    assert ctx.call("music.status")["volume"] == 20


def test_system_volume_wpctl(music, runner):
    ctx, _ = music
    assert ctx.call("music.system_volume", {"level": 50}, origin=Origin.AI) == {"system_volume": 50}
    assert runner.calls[-1] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.50"]
    ctx.call("music.system_volume", {"level": 100})
    assert runner.calls[-1][-1] == "1.00"
    ctx.call("music.system_volume", {"level": 5})
    assert runner.calls[-1][-1] == "0.05"
    runner.script(["wpctl", "get-volume"], stdout="Volume: 0.40\n")
    assert ctx.call("music.system_volume_step", {"delta": 10}) == {"system_volume": 50}
    assert runner.calls[-1] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.50"]
    runner.script(["wpctl", "get-volume"], stdout="Volume: 0.40 [MUTED]\n")
    assert ctx.call("music.system_volume_get") == {"system_volume": 40, "muted": True}


def test_system_volume_wpctl_missing(music, runner):
    ctx, _ = music
    runner.binaries.discard("wpctl")
    with pytest.raises(Unavailable):
        ctx.call("music.system_volume", {"level": 30})


def test_duck_on_game_launch(music, mpv):
    ctx, m = music
    ctx.call("music.play", {"query": "zaz"})
    ctx.call("music.volume", {"level": 80})
    ctx.events.publish("games.launched", {"id": "steam:620"})
    assert wait_for(lambda: mpv.props["volume"] == 30)
    assert ctx.call("music.status")["ducked"] is True
    ctx.events.publish("games.exited", {"id": "steam:620"})
    assert wait_for(lambda: mpv.props["volume"] == 80)


def test_duck_disabled(music, mpv):
    ctx, m = music
    ctx.settings.set("music.duck_on_game", False)
    ctx.call("music.play", {"query": "zaz"})
    ctx.call("music.volume", {"level": 80})
    ctx.events.publish("games.launched", {})
    time.sleep(0.5)
    assert mpv.props["volume"] == 80


# -------------------------------------------------------------- radio
def station(uuid=UUID, url="https://icecast.radiofrance.fr/fip-hifi.aac", name="FIP"):
    return {"stationuuid": uuid, "name": name, "url": url, "url_resolved": url, "country": "France",
            "countrycode": "FR", "tags": "jazz,eclectic", "codec": "AAC", "bitrate": 192,
            "favicon": "https://www.radiofrance.fr/fip.png"}


def test_radio_search(music, net):
    ctx, _ = music
    net.routes["https://de1.api.radio-browser.info/json/stations/search"] = [
        station(), station(UUID2, url="file:///etc/passwd", name="Evil"), {"junk": 1}]
    res = ctx.call("music.radio_search", {"query": "fip"}, origin=Origin.AI)
    assert [s["name"] for s in res] == ["FIP"]          # non-http(s) stream dropped
    assert res[0]["id"] == UUID and res[0]["favorite"] is False
    url = net.requests[-1]["url"]
    assert "name=fip" in url and "limit=20" in url and "hidebroken=true" in url


def test_radio_search_falls_back_to_all_host(music, net):
    ctx, _ = music
    net.routes["https://all.api.radio-browser.info/json/stations/search"] = [station()]
    assert len(ctx.call("music.radio_search", {"query": "fip"})) == 1


def test_radio_play(music, mpv, net):
    ctx, _ = music
    net.routes[f"https://de1.api.radio-browser.info/json/stations/byuuid/{UUID}"] = [station()]
    st = ctx.call("music.radio_play", {"station_id": UUID})
    assert mpv.commands("loadfile") == [["loadfile", "https://icecast.radiofrance.fr/fip-hifi.aac", "replace"]]
    assert st["source"] == "radio" and st["station"]["name"] == "FIP" and st["state"] == "playing"


def test_radio_play_refuses_non_http(music, mpv, net):
    ctx, _ = music
    net.routes[f"https://de1.api.radio-browser.info/json/stations/byuuid/{UUID}"] = [
        station(url="rtsp://example.org/stream")]
    with pytest.raises(NotFound):
        ctx.call("music.radio_play", {"station_id": UUID})
    with pytest.raises(InvalidParams):
        ctx.call("music.radio_play", {"station_id": "../../etc"})
    assert mpv.commands("loadfile") == []


def test_radio_favorites(music, mpv, net):
    ctx, m = music
    net.routes[f"https://de1.api.radio-browser.info/json/stations/byuuid/{UUID}"] = [station()]
    favs = ctx.call("music.radio_favorite_add", {"station_id": UUID})
    assert [f["id"] for f in favs] == [UUID]
    assert json.loads(m.favorites_path.read_text())[0]["name"] == "FIP"
    # directory offline: the cached favourite still plays
    net.routes.clear()
    ctx.call("music.radio_play", {"station_id": UUID})
    assert mpv.commands("loadfile")[-1][1] == "https://icecast.radiofrance.fr/fip-hifi.aac"
    assert ctx.call("music.radio_favorite_remove", {"station_id": UUID}) == []
    with pytest.raises(NotFound):
        ctx.call("music.radio_favorite_remove", {"station_id": UUID})


# -------------------------------------------------------------- playlists
def test_playlists_persisted(music, make_ctx, mpv):
    ctx, m = music
    tracks = ctx.call("music.library")["tracks"]
    p = ctx.call("music.playlist_create", {"name": "  Soirée   jeux "})
    assert p["name"] == "Soirée jeux"
    with pytest.raises(InvalidParams):
        ctx.call("music.playlist_create", {"name": "soiree JEUX"})   # duplicate (accents/case ignored)
    with pytest.raises(InvalidParams):
        ctx.call("music.playlist_create", {"name": "   "})
    for t in tracks[:2]:
        ctx.call("music.playlist_add", {"playlist_id": p["id"], "track_id": t["id"]})
    with pytest.raises(NotFound):
        ctx.call("music.playlist_add", {"playlist_id": p["id"], "track_id": "ghost"})
    ctx2 = make_ctx(modules=["music"], start=False)
    lists = ctx2.call("music.playlists")
    assert lists == [{"id": p["id"], "name": "Soirée jeux", "count": 2, "created": p["created"]}]
    detail = ctx2.call("music.playlist", {"playlist_id": p["id"]})
    assert [t["id"] for t in detail["tracks"]] == [t["id"] for t in tracks[:2]]

    ctx.call("music.play", {"playlist_id": p["id"]})
    assert [c[1] for c in mpv.commands("loadfile")] == [m.library.get(t["id"])["path"] for t in tracks[:2]]
    ctx.call("music.play", {"query": "soirée jeux"})       # playlist found by name
    assert len(mpv.commands("loadfile")) == 4

    ctx.call("music.playlist_remove_track", {"playlist_id": p["id"], "track_id": tracks[0]["id"]})
    assert ctx.call("music.playlists")[0]["count"] == 1


def test_playlist_delete_requires_confirmation(music):
    ctx, m = music
    p = ctx.call("music.playlist_create", {"name": "Temp"})
    with pytest.raises(ConfirmationRequired) as e:
        ctx.call("music.playlist_delete", {"playlist_id": p["id"]}, origin=Origin.AI)
    cid = e.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    ctx.call("music.playlist_delete", {"playlist_id": p["id"]}, origin=Origin.AI, confirmation_id=cid)
    assert ctx.call("music.playlists") == []


def test_empty_playlist_play_not_found(music, mpv):
    ctx, _ = music
    p = ctx.call("music.playlist_create", {"name": "Vide"})
    with pytest.raises(NotFound):
        ctx.call("music.play", {"playlist_id": p["id"]})


# -------------------------------------------------------------- services
def test_services(music, runner):
    ctx, _ = music
    runner.script(["flatpak", "list"], stdout="com.spotify.Client\tSpotify\t1.2\tflathub\n")
    s = {x["id"]: x for x in ctx.call("music.services")}
    assert s["spotify"]["app_id"] == "com.spotify.Client" and s["spotify"]["installed"] is True
    assert s["youtube_music"]["url"] == "https://music.youtube.com"
    assert s["youtube_music"]["open"] == {"action": "browser.open", "params": {"url": "https://music.youtube.com"}}
