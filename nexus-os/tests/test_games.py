import json
import sqlite3
from collections import namedtuple
from pathlib import Path

import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, NotFound, Unavailable
from nexus.core.net import FakeNet
from nexus.security.permissions import Origin

PROTONDB = "https://www.protondb.com/api/v1/reports/summaries/"


def acf(appid, name, flags=4, size=1000, installdir=None, last_played=None):
    lp = f'\n\t"LastPlayed"\t\t"{last_played}"' if last_played else ""
    return f'''"AppState"
{{
\t"appid"\t\t"{appid}"
\t"Universe"\t\t"1"
\t"name"\t\t"{name}"
\t"StateFlags"\t\t"{flags}"
\t"installdir"\t\t"{installdir or name}"
\t"SizeOnDisk"\t\t"{size}"
\t"buildid"\t\t"777"{lp}
\t"UserConfig"
\t{{
\t\t"language"\t\t"french"
\t}}
}}
'''


def build_steam(home: Path):
    root = home / ".local/share/Steam"
    sa = root / "steamapps"
    sa.mkdir(parents=True)
    ext = home / "ExtDisk/SteamLibrary"
    (ext / "steamapps").mkdir(parents=True)
    (home / ".steam").mkdir()
    (home / ".steam/steam").symlink_to(root)  # same root twice: must be de-duplicated
    (sa / "libraryfolders.vdf").write_text(f'''"libraryfolders"
{{
\t"0"
\t{{
\t\t"path"\t\t"{root}"
\t\t"label"\t\t""
\t\t"apps"
\t\t{{
\t\t\t"570"\t\t"1000"
\t\t}}
\t}}
\t"1"
\t{{
\t\t"path"\t\t"{ext}"
\t}}
}}
''')
    (sa / "appmanifest_570.acf").write_text(acf(570, "Dota 2", size=40_000_000_000, installdir="dota 2 beta",
                                                 last_played=1700000000))
    (sa / "appmanifest_1493710.acf").write_text(acf(1493710, "Proton Experimental"))
    (sa / "appmanifest_1628350.acf").write_text(acf(1628350, "Steam Linux Runtime 3.0 (sniper)"))
    (sa / "appmanifest_228980.acf").write_text(acf(228980, "Steamworks Common Redistributables"))
    (sa / "appmanifest_2805730.acf").write_text(acf(2805730, "Proton 9.0"))
    (sa / "appmanifest_broken.acf").write_text('"AppState" { "appid" ')  # must be skipped
    (sa / "common/dota 2 beta").mkdir(parents=True)
    (sa / "common/Proton - Experimental").mkdir(parents=True)
    (ext / "steamapps/appmanifest_1091500.acf").write_text(acf(1091500, "Cyberpunk 2077", flags=1026, size=0))
    # covers: new and legacy layouts
    lc = root / "appcache/librarycache"
    (lc / "570").mkdir(parents=True)
    (lc / "570/library_600x900.jpg").write_bytes(b"\xff\xd8\xffJPEG570")
    (lc / "1091500_library_600x900.jpg").write_bytes(b"\xff\xd8\xffJPEG1091500")
    # forced compat tool
    (root / "config").mkdir()
    (root / "config/config.vdf").write_text('''"InstallConfigStore" { "Software" { "Valve" { "Steam" {
        "CompatToolMapping" { "1091500" { "name" "GE-Proton9-20" "config" "" "priority" "250" } } } } } }''')
    return root


def build_heroic(home: Path):
    base = home / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
    (base / "legendaryConfig/legendary").mkdir(parents=True)
    (base / "legendaryConfig/legendary/installed.json").write_text(json.dumps({
        "Fortnite": {"app_name": "Fortnite", "title": "Fortnite", "install_path": str(home / "Games/Fortnite"),
                     "install_size": 30000, "is_dlc": False, "platform": "Windows"},
        "SomeDLC": {"app_name": "SomeDLC", "title": "Un DLC", "is_dlc": True, "install_size": 1},
    }))
    (base / "store_cache").mkdir()
    (base / "store_cache/legendary_library.json").write_text(json.dumps({"library": [
        {"app_name": "Fortnite", "title": "Fortnite", "is_installed": True,
         "art_square": "https://cdn.example/fn.jpg"},
        {"app_name": "Sugar", "title": "Rocket League", "is_installed": False},
        {"app_name": "../../etc", "title": "évil"},  # invalid appName: ignored
    ]}))
    (base / "gog_store").mkdir()
    (base / "gog_store/installed.json").write_text(json.dumps({"installed": [
        {"appName": "1207658924", "install_path": str(home / "Games/Witcher"), "install_size": "12.5 GB",
         "platform": "windows"}]}))
    (base / "store_cache/gog_library.json").write_text(json.dumps({"games": [
        {"app_name": "1207658924", "title": "The Witcher 3", "is_installed": True}]}))
    (base / "store").mkdir()
    (base / "store/timestamp.json").write_text(json.dumps({"Fortnite": {"lastPlayed": "2026-01-02T03:04:05.000Z"}}))
    (base / "sideload_apps").mkdir()
    (base / "sideload_apps/library.json").write_text("{not json")  # corrupt: ignored
    (home / "Games/Fortnite").mkdir(parents=True)
    return base


def build_lutris(home: Path):
    d = home / ".local/share/lutris"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "pga.db")
    con.execute("""CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, installer_slug TEXT,
        parent_slug TEXT, platform TEXT, runner TEXT, executable TEXT, directory TEXT, updated DATETIME,
        lastplayed INTEGER, installed INTEGER, installed_at INTEGER, year INTEGER, configpath TEXT,
        has_custom_banner INTEGER, has_custom_icon INTEGER, playtime REAL, hidden INTEGER, service TEXT,
        service_id TEXT)""")
    con.execute("INSERT INTO games (id,name,slug,platform,runner,directory,lastplayed,installed,playtime,hidden)"
                " VALUES (12,'Celeste','celeste','Linux','linux',?,1710000000,1,3.5,0)", (str(home / "Games/celeste"),))
    con.execute("INSERT INTO games (id,name,slug,runner,installed,hidden) VALUES (13,'Caché','hidden-game','wine',1,1)")
    con.execute("INSERT INTO games (id,name,slug,runner,installed,hidden) VALUES (14,'Pas installé','nope','wine',0,0)")
    con.commit()
    con.close()
    (d / "coverart").mkdir()
    (d / "coverart/celeste.jpg").write_bytes(b"\xff\xd8\xffCELESTE")
    return d


@pytest.fixture
def home(paths):
    h = paths.home
    build_steam(h)
    build_heroic(h)
    build_lutris(h)
    return h


@pytest.fixture
def ctx(make_ctx, home, tmp_path):
    c = make_ctx(modules=["games"])
    m = c.module("games")
    m.drm_root = tmp_path / "drm"
    m.proc_root = tmp_path / "proc"
    (tmp_path / "proc").mkdir()
    m.watch_interval = 0.01
    return c


def ids(res):
    return {g["id"] for g in res["games"]}


# ------------------------------------------------------------------ library

def test_unified_library(ctx):
    lib = ctx.call("games.library")
    assert ids(lib) == {"steam:570", "steam:1091500", "heroic:legendary:Fortnite", "heroic:legendary:Sugar",
                        "heroic:gog:1207658924", "lutris:12", "lutris:14"}
    by = {g["id"]: g for g in lib["games"]}
    dota = by["steam:570"]
    assert dota["title"] == "Dota 2" and dota["installed"] and dota["size"] == 40_000_000_000
    assert dota["cover"] == "/api/m/games/cover/steam/570"
    assert dota["last_played"] == "2023-11-14T22:13:20Z"
    assert dota["install_path"].endswith("steamapps/common/dota 2 beta")
    cp = by["steam:1091500"]
    assert cp["installed"] is False and cp["size"] is None  # StateFlags 1026 = update required+started
    assert cp["cover"] == "/api/m/games/cover/steam/1091500"
    fn = by["heroic:legendary:Fortnite"]
    assert fn["installed"] and fn["size"] == 30000 and fn["cover"] is None
    assert fn["cover_remote"] == "https://cdn.example/fn.jpg"
    assert fn["last_played"] == "2026-01-02T03:04:05Z"
    w3 = by["heroic:gog:1207658924"]
    assert w3["title"] == "The Witcher 3" and w3["installed"] and w3["size"] == int(12.5 * 1024 ** 3)
    assert by["heroic:legendary:Sugar"]["installed"] is False
    cel = by["lutris:12"]
    assert cel["title"] == "Celeste" and cel["installed"] and cel["cover"] == "/api/m/games/cover/lutris/12"
    assert cel["last_played"] is not None
    # installed games sorted first
    flags = [g["installed"] for g in lib["games"]]
    assert flags == sorted(flags, reverse=True)
    # internal fields are not leaked
    assert "manifest" not in dota and "state_flags" not in dota


def test_library_filters_and_cache(ctx, paths):
    assert ids(ctx.call("games.library", {"source": "lutris"})) == {"lutris:12", "lutris:14"}
    inst = ctx.call("games.library", {"installed_only": True})
    assert all(g["installed"] for g in inst["games"])
    assert ids(ctx.call("games.library", {"query": "dota"})) == {"steam:570"}
    cache = json.loads((paths.cache / "games/library.json").read_text())
    assert len(cache["games"]) == 7


def test_refresh_picks_up_new_game(ctx, home):
    ctx.call("games.library")
    sa = home / ".local/share/Steam/steamapps"
    (sa / "appmanifest_620.acf").write_text(acf(620, "Portal 2"))
    assert "steam:620" not in ids(ctx.call("games.library"))  # cached
    r = ctx.call("games.refresh")
    assert r["counts"]["steam"] == 3
    assert "steam:620" in ids(ctx.call("games.library"))
    assert ctx.events.recent("games.library_updated")


def test_flatpak_steam_root(make_ctx, paths):
    root = paths.home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps"
    root.mkdir(parents=True)
    (root / "appmanifest_440.acf").write_text(acf(440, "Team Fortress 2"))
    c = make_ctx(modules=["games"])
    assert ids(c.call("games.library")) == {"steam:440"}
    st = c.module("games").status()
    assert st["launchers"]["steam"]["flatpak"] is True


def test_legacy_libraryfolders_format(make_ctx, paths):
    h = paths.home
    root = h / ".local/share/Steam"
    (root / "steamapps").mkdir(parents=True)
    other = h / "lib2"
    (other / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"LibraryFolders"\n{{\n\t"TimeNextStatsReport"\t"123"\n\t"ContentStatsID"\t"-1"\n\t"1"\t"{other}"\n}}\n')
    (other / "steamapps/appmanifest_400.acf").write_text(acf(400, "Portal"))
    c = make_ctx(modules=["games"])
    assert ids(c.call("games.library")) == {"steam:400"}


def test_status(ctx):
    st = ctx.module("games").status()
    assert st["counts"] == {"steam": 2, "heroic": 3, "lutris": 2}
    assert st["launchers"]["steam"]["detected"] and st["launchers"]["heroic"]["detected"]
    assert st["launchers"]["heroic"]["flatpak"] is True
    assert st["launchers"]["lutris"]["detected"]
    assert st["ok"] is True


def test_status_nothing_installed(make_ctx, runner):
    runner.binaries -= {"steam", "lutris"}
    c = make_ctx(modules=["games"])
    st = c.module("games").status()
    assert st["ok"] is False and st["total"] == 0 and "Aucun lanceur" in st["message"]


# ------------------------------------------------------------------ covers

def test_serve_file(ctx):
    m = ctx.module("games")
    assert m.serve_file("cover/steam/570").read_bytes() == b"\xff\xd8\xffJPEG570"
    assert m.serve_file("cover/steam/1091500").name == "1091500_library_600x900.jpg"
    assert m.serve_file("cover/lutris/12").name == "celeste.jpg"
    for bad in ("cover/steam/999", "cover/steam/../../etc/passwd", "../etc/passwd", "cover/heroic/x",
                "cover/steam/570;rm", "cover/lutris/abc", ""):
        assert m.serve_file(bad) is None


# ------------------------------------------------------------------ launch / install

def test_launch_steam_exact_argv(ctx, runner):
    r = ctx.call("games.launch", {"id": "steam:570"})
    assert runner.spawned == [["xdg-open", "steam://rungameid/570"]]
    assert r["launched"] and r["title"] == "Dota 2"
    ev = ctx.events.recent("games.launched")
    assert ev and ev[-1]["data"] == {"id": "steam:570", "title": "Dota 2", "source": "steam",
                                  "exit_detection": True}


def test_launch_heroic_and_lutris_argv(ctx, runner):
    ctx.call("games.launch", {"id": "heroic:legendary:Fortnite"})
    ctx.call("games.launch", {"id": "lutris:12"})
    assert runner.spawned == [["xdg-open", "heroic://launch/legendary/Fortnite"],
                              ["xdg-open", "lutris:rungameid/12"]]


def test_launch_ai_allowed_and_records_last_played(ctx, runner, paths):
    ctx.call("games.launch", {"id": "lutris:12"}, origin=Origin.AI)
    data = json.loads((paths.data / "games/launches.json").read_text())
    assert "lutris:12" in data


def test_launch_not_installed_non_steam(ctx, runner):
    with pytest.raises(InvalidParams):
        ctx.call("games.launch", {"id": "lutris:14"})
    assert runner.spawned == []


def test_launch_unknown_game(ctx, runner):
    with pytest.raises(NotFound):
        ctx.call("games.launch", {"id": "steam:12345"})


def test_launch_without_xdg_open(ctx, runner):
    runner.binaries.discard("xdg-open")
    with pytest.raises(Unavailable):
        ctx.call("games.launch", {"id": "steam:570"})


@pytest.mark.parametrize("bad", ["570; rm -rf /", "steam:570; rm -rf", "steam:570 && reboot", "steam:-1",
                                 "heroic:legendary:../x", "heroic:evil:Fortnite", "lutris:1;id",
                                 "steam:570\n", "steam://rungameid/570", "steam:12345678901"])
def test_invalid_ids_rejected(ctx, runner, bad):
    for act in ("launch", "install", "details", "compatibility", "diagnose", "optimize"):
        with pytest.raises(InvalidParams):
            ctx.call(f"games.{act}", {"id": bad})
    assert runner.spawned == [] and runner.calls == []


def test_install_steam(ctx, runner):
    r = ctx.call("games.install", {"id": "steam:1091500"}, origin=Origin.AI)  # INSTALL_OWNED: AI allowed
    assert r["started"]
    r2 = ctx.call("games.install", {"id": "steam:620"})  # owned but not on disk
    assert r2["started"]
    assert runner.spawned == [["xdg-open", "steam://install/1091500"], ["xdg-open", "steam://install/620"]]
    r3 = ctx.call("games.install", {"id": "steam:570"})
    assert r3["started"] is False


def test_install_heroic_unavailable(ctx):
    with pytest.raises(Unavailable) as ei:
        ctx.call("games.install", {"id": "heroic:legendary:Sugar"})
    assert ei.value.details["hint"] == "open_launcher"


def test_uninstall_requires_confirmation(ctx, runner):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("games.uninstall", {"id": "steam:570"}, origin=Origin.AI)
    assert runner.spawned == []
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    r = ctx.call("games.uninstall", {"id": "steam:570"}, origin=Origin.AI, confirmation_id=cid)
    assert r["started"]
    assert runner.spawned == [["xdg-open", "steam://uninstall/570"]]


def test_open_launcher(ctx, runner):
    ctx.call("games.open_launcher", {"launcher": "heroic"})
    ctx.call("games.open_launcher", {"launcher": "lutris"})
    ctx.call("games.open_launcher", {"launcher": "steam"})
    assert runner.spawned == [["flatpak", "run", "com.heroicgameslauncher.hgl"], ["lutris"],
                              ["xdg-open", "steam://open/games"]]


def test_details(ctx):
    d = ctx.call("games.details", {"id": "steam:1091500"})
    assert d["compat_tool"] == "GE-Proton9-20"
    assert "mise à jour requise" in d["state"]
    assert d["links"]["protondb"] == "https://www.protondb.com/app/1091500"
    with pytest.raises(NotFound):
        ctx.call("games.details", {"id": "lutris:99"})


# ------------------------------------------------------------------ ProtonDB

def test_compatibility_protondb_success(ctx, net, paths):
    net.routes[PROTONDB + "570.json"] = {"bestReportedTier": "platinum", "confidence": "strong", "score": 0.9,
                                         "tier": "gold", "total": 321, "trendingTier": "platinum"}
    r = ctx.call("games.compatibility", {"id": "steam:570"})
    assert r["tier"] == "bon" and r["protondb_tier"] == "gold" and r["total_reports"] == 321
    assert r["source"] == "protondb" and "garantie" in r["note"]
    assert net.requests[-1]["url"] == PROTONDB + "570.json"
    # cached: no new request
    n = len(net.requests)
    r2 = ctx.call("games.compatibility", {"id": "steam:570"})
    assert r2["tier"] == "bon" and r2["cached"] and len(net.requests) == n


def test_compatibility_offline(ctx, net):
    r = ctx.call("games.compatibility", {"id": "steam:570"})
    assert r["tier"] == "inconnu" and r["source"] == "protondb" and r["protondb_tier"] is None
    assert "error" in r


def test_compatibility_unknown_tier_value(ctx, net):
    net.routes[PROTONDB + "570.json"] = {"tier": "pending", "total": 1}
    assert ctx.call("games.compatibility", {"id": "steam:570"})["tier"] == "inconnu"


def test_compatibility_stale_cache_when_offline(ctx, net, paths):
    import time
    f = paths.cache / "games/protondb/570.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"tier": "stable", "protondb_tier": "platinum", "source": "protondb",
                             "_ts": time.time() - 10 * 86400}))
    r = ctx.call("games.compatibility", {"id": "steam:570"})
    assert r["tier"] == "stable" and r["stale"] is True


def test_compatibility_non_steam(ctx, net):
    r = ctx.call("games.compatibility", {"id": "lutris:12"})
    assert r["tier"] == "inconnu" and r["source"] == "aucune"
    assert net.requests == []


# ------------------------------------------------------------------ diagnose

def _codes(r):
    return {f["code"]: f for f in r["findings"]}


DiskUsage = namedtuple("DiskUsage", "total used free")


def test_diagnose_steam_full(ctx, net, runner, home):
    net.routes[PROTONDB + "1091500.json"] = {"tier": "borked", "total": 50}
    runner.script(["vulkaninfo", "--summary"], stdout="GPU0:\n\tdeviceName = AMD Radeon RX 7800 XT (RADV NAVI32)\n")
    (home / "steam-1091500.log").write_text(
        "1234.567:0020:0024:err:module:import_dll Library d3d12.dll not found\n"
        "1234.568:0020:0024:fixme:ntdll:stuff\n"
        "wine: Unhandled page fault on read access\n")
    m = ctx.module("games")
    m.disk_usage = lambda p: DiskUsage(100, 99, 2 * 1024 ** 3)
    r = ctx.call("games.diagnose", {"id": "steam:1091500"})
    c = _codes(r)
    assert c["not_installed"]["level"] == "error"
    assert c["steam_state"]["level"] == "warning"
    assert c["protondb"]["level"] == "error"
    assert c["proton_present"]["level"] == "ok" and "Proton - Experimental" in c["proton_present"]["message"]
    assert c["compat_tool_missing"]["level"] == "error"  # GE-Proton9-20 not installed
    assert c["disk_low"]["level"] == "warning"
    assert c["vulkan_ok"]["details"]["devices"] == ["AMD Radeon RX 7800 XT (RADV NAVI32)"]
    errs = c["proton_log"]["details"]["errors"]
    assert any("d3d12.dll" in e for e in errs) and any("Unhandled" in e for e in errs)
    assert not any("fixme" in e for e in errs)
    assert ["vulkaninfo", "--summary"] in runner.calls
    assert r["suggestions"] and "bloquant" in r["verdict"]


def test_diagnose_installed_ok(ctx, net, runner):
    net.routes[PROTONDB + "570.json"] = {"tier": "platinum", "total": 999}
    runner.binaries.discard("vulkaninfo")
    ctx.module("games").disk_usage = lambda p: DiskUsage(100, 1, 500 * 1024 ** 3)
    r = ctx.call("games.diagnose", {"id": "steam:570"})
    c = _codes(r)
    assert c["installed"]["level"] == "ok" and c["disk_ok"]["level"] == "ok"
    assert c["vulkan_unchecked"]["level"] == "info"
    assert "PROTON_LOG=1" in " ".join(r["suggestions"])
    assert not [f for f in r["findings"] if f["level"] == "error"]


def test_diagnose_software_vulkan_and_offline(ctx, net, runner):
    runner.script(["vulkaninfo", "--summary"], stdout="\tdeviceName = llvmpipe (LLVM 17.0.6, 256 bits)\n")
    r = ctx.call("games.diagnose", {"id": "steam:570"})
    c = _codes(r)
    assert c["vulkan_software"]["level"] == "error"
    assert "inconnue" in c["protondb"]["message"]


def test_diagnose_unknown_game_and_missing_dir(ctx, net):
    r = ctx.call("games.diagnose", {"id": "steam:99999"})
    assert _codes(r)["not_in_library"]["level"] == "error"
    r = ctx.call("games.diagnose", {"id": "heroic:gog:1207658924"})  # install dir does not exist
    assert _codes(r)["install_dir_missing"]["level"] == "error"


# ------------------------------------------------------------------ optimize

def test_optimize_nvidia(ctx, tmp_path, net, home):
    d = tmp_path / "drm/card0/device"
    d.mkdir(parents=True)
    (d / "vendor").write_text("0x10de\n")
    ctx.settings.set("hardware.mangohud", True)
    before = (home / ".local/share/Steam/config/config.vdf").read_text()
    r = ctx.call("games.optimize", {"id": "steam:570"})
    assert r["gpu"] == ["nvidia"]
    assert r["steam_launch_options"] == "PROTON_ENABLE_NVAPI=1 gamemoderun mangohud %command%"
    assert r["applied"] is False and "n'écrit pas" in r["note"]
    titles = {x["title"] for x in r["recommendations"]}
    assert {"GameMode", "MangoHud", "NVAPI (NVIDIA)"} <= titles
    assert (home / ".local/share/Steam/config/config.vdf").read_text() == before


def test_optimize_without_tools(ctx, runner):
    runner.binaries -= {"gamemoderun", "mangohud"}
    r = ctx.call("games.optimize", {"id": "lutris:12"})
    assert r["gpu"] == [] and "steam_launch_options" not in r
    gm = next(x for x in r["recommendations"] if x["title"] == "GameMode")
    assert gm["available"] is False
    assert "Lutris" in r["how_to_apply"]


def test_optimize_mangohud_setting_off(ctx, tmp_path):
    r = ctx.call("games.optimize", {"id": "steam:570"})  # hardware.mangohud defaults to False
    assert r["steam_launch_options"] == "gamemoderun %command%"
    mh = next(x for x in r["recommendations"] if x["title"] == "MangoHud")
    assert mh["enabled_in_settings"] is False and mh["option"] == "mangohud %command%"
    assert "options de lancement" in r["note"]


def test_malformed_id_rejected_before_confirmation(ctx, runner):
    # validator runs before the permission check: no confirmation is ever created
    with pytest.raises(InvalidParams):
        ctx.call("games.uninstall", {"id": "570; rm -rf"}, origin=Origin.AI)
    assert ctx.permissions.pending() == []
    assert runner.spawned == []


# ------------------------------------------------------------------ hardware integration

from nexus.core.module import NexusModule, action  # noqa: E402
from nexus.security.permissions import Risk  # noqa: E402


class FakeHardware(NexusModule):
    name = "hardware"
    info_data: dict = {}

    @action("info", Risk.READ, "fake")
    def info(self):
        return self.info_data


def test_diagnose_uses_hardware_info(ctx, runner, net):
    hw = FakeHardware(ctx)
    hw.info_data = {"cpu": {"avx2": False},
                    "gpus": [{"vendor": "nvidia", "name": "GeForce RTX 3060", "driver": "nvidia"}],
                    "vulkan": {"available": True, "devices": [{"name": "NVIDIA GeForce RTX 3060"}]}}
    ctx.registry.register(hw)
    r = ctx.call("games.diagnose", {"id": "steam:570"})
    c = _codes(r)
    assert c["cpu_no_avx2"]["level"] == "warning"
    assert "GeForce RTX 3060" in c["gpu"]["message"]
    assert c["vulkan_ok"]["details"]["devices"] == ["NVIDIA GeForce RTX 3060"]
    assert ["vulkaninfo", "--summary"] not in runner.calls  # not run twice
    # optimize falls back on hardware.info when sysfs gives nothing
    assert ctx.call("games.optimize", {"id": "steam:570"})["gpu"] == ["nvidia"]


def test_diagnose_hardware_info_failing(ctx, runner):
    from nexus.core.errors import Unavailable as U

    class Broken(FakeHardware):
        def info(self):
            raise U("pas de matériel")
    Broken.info = action("info", Risk.READ, "fake")(Broken.info)
    ctx.registry.register(Broken(ctx))
    r = ctx.call("games.diagnose", {"id": "steam:570"})
    assert "hardware_unchecked" in _codes(r)


def test_diagnose_without_hardware_module(ctx):
    r = ctx.call("games.diagnose", {"id": "lutris:12"})
    assert "hardware_unchecked" in _codes(r)


def _wait_event(ctx, topic, timeout=3):
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end:
        ev = ctx.events.recent(topic)
        if ev:
            return ev[-1]
        _t.sleep(0.01)
    return None


def test_steam_game_exit_detected(ctx, tmp_path):
    proc = tmp_path / "proc" / "777"
    proc.mkdir()
    (proc / "cmdline").write_bytes(b"/home/u/.steam/ubuntu12_32/reaper\x00SteamLaunch\x00AppId=570\x00--\x00game\x00")
    ctx.call("games.launch", {"id": "steam:570"})
    import time as _t
    _t.sleep(0.1)
    assert ctx.events.recent("games.exited") == []      # still running
    (proc / "cmdline").unlink()
    proc.rmdir()
    ev = _wait_event(ctx, "games.exited")
    assert ev and ev["data"] == {"id": "steam:570", "title": "Dota 2", "reason": "exited"}


def test_steam_game_never_started_reports_not_detected(ctx):
    ctx.module("games").watch_start_timeout = 0.05
    ctx.call("games.launch", {"id": "steam:570"})
    ev = _wait_event(ctx, "games.exited")
    assert ev and ev["data"]["reason"] == "not_detected"


def test_other_appid_does_not_count(ctx, tmp_path):
    proc = tmp_path / "proc" / "778"
    proc.mkdir()
    (proc / "cmdline").write_bytes(b"reaper\x00SteamLaunch\x00AppId=5700\x00")
    assert ctx.module("games")._steam_game_running("570") is False
    assert ctx.module("games")._steam_game_running("5700") is True
