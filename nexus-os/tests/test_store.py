import json
import re
import urllib.error

import pytest

from nexus.core.errors import CommandFailed, ConfirmationRequired, InvalidParams, NotFound, PermissionDenied, Unavailable
from nexus.modules.store import catalog
from nexus.modules.store.flathub import is_open_source
from nexus.security.permissions import Origin
from nexus.security.verify import FLATPAK_ID_RE, STEAM_APPID_RE

API = "https://flathub.org/api/v2"

SEARCH_RESPONSE = {
    "query": "kart",
    "hits": [
        {"app_id": "net.supertuxkart.SuperTuxKart", "name": "SuperTuxKart", "summary": "Kart racing game",
         "developer_name": "SuperTuxKart Team", "project_license": "GPL-3.0-or-later",
         "icon": "https://dl.flathub.org/media/net/supertuxkart/icon.png", "verification_verified": True},
        {"id": "com.example.Proprio", "name": "Proprio", "summary": "x",
         "project_license": "LicenseRef-proprietary", "verification_verified": "false"},
        {"app_id": "--from=https://evil/x.flatpakref", "name": "Evil"},
        "garbage",
    ],
    "totalHits": 4,
}

APPSTREAM_VLC = {"type": "desktop-application", "id": "org.videolan.VLC", "name": "VLC",
                 "summary": "VLC media player", "developer_name": "VideoLAN",
                 "project_license": "GPL-2.0+", "urls": {"homepage": "https://www.videolan.org/vlc/"}}


def http_404(url):
    err = CommandFailed("Requête réseau échouée: HTTPError", code="network_error")
    err.__cause__ = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return err


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(modules=["store"])


def wait(ctx, res):
    t = ctx.tasks.wait(res["task_id"], timeout=10)
    return t


# ------------------------------------------------------------------ search
def test_search_flathub_and_offline_cache(ctx, net):
    net.routes[f"{API}/search"] = SEARCH_RESPONSE
    r = ctx.call("store.search", {"query": "kart"})
    assert r["stale"] is False and r["source"] == "flathub"
    ids = [h["app_id"] for h in r["results"]]
    assert ids == ["net.supertuxkart.SuperTuxKart", "com.example.Proprio"]  # malformed hits dropped
    stk, prop = r["results"]
    assert stk["verified"] is True and stk["open_source"] is True and stk["license"] == "GPL-3.0-or-later"
    assert stk["url"] == "https://flathub.org/apps/net.supertuxkart.SuperTuxKart" and stk["source"] == "flathub"
    assert prop["verified"] is False and prop["open_source"] is False
    req = net.requests[-1]
    assert req["method"] == "POST" and json.loads(req["data"]) == {"query": "kart"}

    # Offline + TTL expired: last known data with stale flag.
    net.routes.clear()
    mod = ctx.module("store")
    mod.flathub.clock = lambda: 10 ** 12
    r2 = ctx.call("store.search", {"query": "kart"})
    assert r2["stale"] is True and [h["app_id"] for h in r2["results"]] == ids
    assert mod.status()["flathub_reachable"] is False


def test_search_offline_falls_back_to_flatpak(ctx, runner):
    runner.script(["flatpak", "search"], stdout=(
        "net.supertuxkart.SuperTuxKart\tSuperTuxKart\tKart racing\tflathub\n"
        "org.other.App\tOther\tFrom elsewhere\tsomeremote\n"))
    r = ctx.call("store.search", {"query": "--kart"})
    assert r["source"] == "flatpak-local" and r["stale"] is True
    assert [h["app_id"] for h in r["results"]] == ["net.supertuxkart.SuperTuxKart"]
    assert runner.calls[-1] == ["flatpak", "search", "--columns=application,name,description,remotes", "--", "kart"]


def test_search_offline_no_cache_no_flatpak(make_ctx, runner):
    runner.binaries.discard("flatpak")
    ctx = make_ctx(modules=["store"])
    with pytest.raises(CommandFailed) as e:
        ctx.call("store.search", {"query": "vlc"})
    assert e.value.code == "network_error"
    with pytest.raises(InvalidParams):
        ctx.call("store.search", {"query": "   "})


# ----------------------------------------------------------------- details
def test_details_card(ctx, net):
    net.routes[f"{API}/appstream/org.videolan.VLC"] = APPSTREAM_VLC
    net.routes[f"{API}/verification/org.videolan.VLC/status"] = {"verified": True, "method": "website"}
    net.routes[f"{API}/summary/org.videolan.VLC"] = {"metadata": {"permissions": {"shared": ["network", "ipc"],
                                                                                   "devices": ["dri"]}}}
    d = ctx.call("store.details", {"app_id": "org.videolan.VLC"})
    assert d["name"] == "VLC" and d["developer"] == "VideoLAN" and d["license"] == "GPL-2.0+"
    assert d["verified"] is True and d["open_source"] is True and d["source"] == "flathub"
    assert d["permissions"] == {"shared": ["network", "ipc"], "devices": ["dri"]}
    assert d["url"] == "https://flathub.org/apps/org.videolan.VLC" and d["stale"] is False


def test_details_proprietary_and_404(ctx, net):
    net.routes[f"{API}/appstream/com.spotify.Client"] = {"name": "Spotify", "project_license": "LicenseRef-proprietary"}
    d = ctx.call("store.details", {"app_id": "com.spotify.Client"})
    assert d["open_source"] is False and d["proprietary"] is True and d["permissions"] is None
    net.routes[f"{API}/appstream/org.nope.Nope"] = http_404("x")
    with pytest.raises(NotFound):
        ctx.call("store.details", {"app_id": "org.nope.Nope"})


def test_open_source_detection():
    assert is_open_source("GPL-3.0-or-later AND MIT") is True
    assert is_open_source("LicenseRef-proprietary") is False
    assert is_open_source("GPL-2.0 AND LicenseRef-proprietary=https://x") is False
    assert is_open_source("LicenseRef-something") is None
    assert is_open_source(None) is None


# ----------------------------------------------------------------- install
def test_install_unverified_non_curated_refused(ctx, net, runner):
    net.routes[f"{API}/appstream/com.example.Random"] = {"name": "Random"}
    net.routes[f"{API}/verification/com.example.Random/status"] = {"verified": False}
    with pytest.raises(PermissionDenied) as e:
        ctx.call("store.install", {"app_id": "com.example.Random"})
    assert e.value.code == "requires_unverified_flow"
    assert e.value.details["next_action"] == "store.install_unverified"
    assert not any(c[:2] == ["flatpak", "install"] for c in runner.calls)


def test_install_not_on_flathub(ctx, net):
    net.routes[f"{API}/appstream/com.example.Ghost"] = http_404("x")
    with pytest.raises(NotFound):
        ctx.call("store.install", {"app_id": "com.example.Ghost"})


def test_install_verified_argv_task_history(ctx, net, runner):
    net.routes[f"{API}/appstream/org.example.Verified"] = {"name": "Verified App"}
    net.routes[f"{API}/verification/org.example.Verified/status"] = {"verified": True}
    q = ctx.events.subscribe()
    res = ctx.call("store.install", {"app_id": "org.example.Verified"}, origin=Origin.AI)  # auto_install_verified
    t = wait(ctx, res)
    assert t.state == "done", t.error
    assert runner.calls[-1] == ["flatpak", "install", "--system", "-y", "--noninteractive", "flathub",
                                "org.example.Verified"]
    hist = json.loads((ctx.paths.data / "store_history.json").read_text())
    assert hist[-1]["app_id"] == "org.example.Verified" and hist[-1]["remote"] == "flathub"
    assert hist[-1]["verified"] is True and hist[-1]["action"] == "install" and hist[-1]["date"]
    topics = []
    while not q.empty():
        topics.append(q.get_nowait()["topic"])
    assert "store.installed" in topics


def test_install_curated_unverified_allowed(ctx, net, runner):
    net.routes[f"{API}/appstream/com.discordapp.Discord"] = {"name": "Discord"}
    net.routes[f"{API}/verification/com.discordapp.Discord/status"] = {"verified": False}
    t = wait(ctx, ctx.call("store.install", {"app_id": "com.discordapp.Discord"}))
    assert t.state == "done" and t.result["curated"] is True
    assert runner.calls[-1][-2:] == ["flathub", "com.discordapp.Discord"]


def test_install_curated_offline_uses_remote_info(ctx, runner):
    t = wait(ctx, ctx.call("store.install", {"app_id": "org.videolan.VLC"}))
    assert t.state == "done"
    assert ["flatpak", "remote-info", "flathub", "org.videolan.VLC"] in runner.calls


def test_install_non_curated_offline_refused(ctx):
    with pytest.raises(CommandFailed) as e:
        ctx.call("store.install", {"app_id": "com.example.Unknown"})
    assert e.value.code == "network_error"


def test_install_extension_not_installable(ctx):
    with pytest.raises(InvalidParams) as e:
        ctx.call("store.install", {"app_id": "org.freedesktop.Platform.VulkanLayer.MangoHud"})
    assert e.value.code == "not_installable"


def test_install_failure_marks_task_failed(ctx, net, runner):
    net.routes[f"{API}/appstream/org.example.Verified"] = {"name": "V"}
    net.routes[f"{API}/verification/org.example.Verified/status"] = {"verified": True}
    runner.script(["flatpak", "install"], returncode=1, stderr="error: boom")
    t = wait(ctx, ctx.call("store.install", {"app_id": "org.example.Verified"}))
    assert t.state == "failed" and t.error["error"] == "command_failed"
    assert not (ctx.paths.data / "store_history.json").exists()


def test_install_without_flatpak(make_ctx, runner):
    runner.binaries.discard("flatpak")
    ctx = make_ctx(modules=["store"])
    with pytest.raises(Unavailable):
        ctx.call("store.install", {"app_id": "org.videolan.VLC"})
    st = ctx.module("store").status()
    assert st["flatpak_available"] is False and st["ok"] is False


@pytest.mark.parametrize("bad", ["--from=https://evil/x.flatpakref", "org.example", "org.example.App; rm -rf /",
                                 "../../etc/passwd", "-y.a.b", "org.example.App\x00x", "a" * 300 + ".b.c"])
def test_malicious_app_id_rejected(ctx, runner, bad):
    for act in ("store.install", "store.details", "store.install_free_game"):
        with pytest.raises(InvalidParams):
            ctx.call(act, {"app_id": bad})
    # Confirmed actions: the policy asks first, but even an approved call is rejected.
    for act in ("store.install_unverified", "store.uninstall"):
        with pytest.raises(ConfirmationRequired) as e:
            ctx.call(act, {"app_id": bad})
        cid = e.value.details["confirmation_id"]
        ctx.permissions.approve(cid, Origin.USER)
        with pytest.raises(InvalidParams):
            ctx.call(act, {"app_id": bad}, confirmation_id=cid)
    assert not any(c[:2] == ["flatpak", "install"] for c in runner.calls)


# ------------------------------------------------------- install_unverified
def test_install_unverified_denied_for_ai(ctx, runner):
    with pytest.raises(PermissionDenied):
        ctx.call("store.install_unverified", {"app_id": "com.example.Random", "remote": "flathub"}, origin=Origin.AI)
    assert runner.calls == []


def test_install_unverified_user_confirmation_flow(ctx, net, runner):
    net.routes[f"{API}/appstream/com.example.Random"] = {"name": "Random"}
    net.routes[f"{API}/verification/com.example.Random/status"] = {"verified": False}
    params = {"app_id": "com.example.Random", "remote": "flathub"}
    with pytest.raises(ConfirmationRequired) as e:
        ctx.call("store.install_unverified", params)
    assert not any(c[:2] == ["flatpak", "install"] for c in runner.calls)  # never silent
    cid = e.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    t = wait(ctx, ctx.call("store.install_unverified", params, confirmation_id=cid))
    assert t.state == "done"
    assert runner.calls[-1][-2:] == ["flathub", "com.example.Random"]
    hist = json.loads((ctx.paths.data / "store_history.json").read_text())
    assert hist[-1]["verified"] is False


def test_install_unverified_other_remote_refused(ctx, runner):
    params = {"app_id": "com.example.Random", "remote": "evilrepo"}
    with pytest.raises(ConfirmationRequired) as e:
        ctx.call("store.install_unverified", params)
    cid = e.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    with pytest.raises(PermissionDenied):
        ctx.call("store.install_unverified", params, confirmation_id=cid)
    assert runner.calls == []


# -------------------------------------------------------- other actions
def test_uninstall_requires_confirmation_and_runs(ctx, runner):
    runner.script(["flatpak", "list"], stdout="org.videolan.VLC\tVLC\t3.0\tflathub\n")
    params = {"app_id": "org.videolan.VLC", "delete_data": True}
    with pytest.raises(ConfirmationRequired) as e:
        ctx.call("store.uninstall", params, origin=Origin.AI)
    cid = e.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    t = wait(ctx, ctx.call("store.uninstall", params, origin=Origin.AI, confirmation_id=cid))
    assert t.state == "done"
    assert runner.calls[-1] == ["flatpak", "uninstall", "--system", "-y", "--noninteractive", "--delete-data",
                                "org.videolan.VLC"]


def test_installed_updates_update_all(ctx, runner):
    runner.script(["flatpak", "list"], stdout="org.videolan.VLC\tVLC\t3.0\tflathub\n")
    runner.script(["flatpak", "remote-ls"], stdout="org.videolan.VLC\n")
    inst = ctx.call("store.installed")
    assert inst["count"] == 1 and inst["apps"][0]["curated"] is True
    assert ctx.call("store.updates") == {"updates": ["org.videolan.VLC"], "count": 1}
    t = wait(ctx, ctx.call("store.update_all"))
    assert t.state == "done"
    assert runner.calls[-1] == ["flatpak", "update", "--system", "-y", "--noninteractive"]
    assert ctx.module("store").status()["installed_count"] == 1


def test_featured_and_curated_ids_valid(ctx):
    f = ctx.call("store.featured")
    ids = [a["app_id"] for c in f["categories"] for a in c["apps"]]
    assert "com.valvesoftware.Steam" in ids and "org.mozilla.firefox" in ids
    assert all(FLATPAK_ID_RE.match(i) for i in ids)
    by_id = {a["app_id"]: a for c in f["categories"] for a in c["apps"]}
    assert by_id["com.spotify.Client"]["proprietary"] is True
    assert by_id["com.discordapp.Discord"]["proprietary"] is True
    assert by_id["org.freedesktop.Platform.VulkanLayer.MangoHud"]["installable"] is False


def test_free_games_valid(ctx):
    g = ctx.call("store.free_games")
    assert g["flathub"] and g["steam"]
    for e in g["flathub"]:
        assert FLATPAK_ID_RE.match(e["app_id"]) and e["source"] == "flathub"
        assert e["url"] == "https://flathub.org/apps/" + e["app_id"]
    for e in g["steam"]:
        assert STEAM_APPID_RE.match(e["appid"])
        assert re.fullmatch(r"steam://install/[0-9]+", e["install_url"])
    assert {e["appid"] for e in g["steam"]} == {"570", "440", "730"}
    assert len({e["app_id"] for e in g["flathub"]}) == len(g["flathub"])


def test_install_free_game(ctx, runner):
    t = wait(ctx, ctx.call("store.install_free_game", {"app_id": "net.supertuxkart.SuperTuxKart"}, origin=Origin.AI))
    assert t.state == "done" and t.result["kind"] == "free_game"
    assert runner.calls[-1][-2:] == ["flathub", "net.supertuxkart.SuperTuxKart"]
    with pytest.raises(PermissionDenied) as e:
        ctx.call("store.install_free_game", {"app_id": "org.videolan.VLC"})
    assert e.value.code == "not_curated_free_game"


def test_curated_catalog_consistency():
    for i in catalog.curated_installable_ids():
        assert FLATPAK_ID_RE.match(i)
    assert "org.freedesktop.Platform.VulkanLayer.MangoHud" not in catalog.curated_installable_ids()
