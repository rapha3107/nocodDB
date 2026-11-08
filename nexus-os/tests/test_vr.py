import json
from pathlib import Path

import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, Unavailable
from nexus.modules.vr.module import identify_headset
from nexus.security.permissions import Origin


def w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def usb(sys: Path, name, vid, pid, product=None):
    d = sys / "bus/usb/devices" / name
    w(d / "idVendor", vid + "\n")
    w(d / "idProduct", pid + "\n")
    if product:
        w(d / "product", product + "\n")
    (sys / "bus/usb/devices" / f"{name}:1.0").mkdir(parents=True, exist_ok=True)  # interface, no idVendor


def manifest(steamapps: Path, appid, name):
    w(steamapps / f"appmanifest_{appid}.acf",
      f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"installdir"\t\t"{name}"\n}}\n')


@pytest.fixture
def vr(make_ctx, tmp_path, runner):
    ctx = make_ctx(modules=["vr", "hardware"])
    mod = ctx.module("vr")
    mod.sys = tmp_path / "sys"
    mod.etc = tmp_path / "etc"
    mod.home = tmp_path / "home"
    mod.sys.mkdir()
    runner.binaries.discard("monado-service")
    return ctx, mod


def test_detect_index(vr):
    ctx, mod = vr
    usb(mod.sys, "1-1", "28de", "2300", "Index HMD")
    usb(mod.sys, "1-2", "046d", "c52b", "USB Receiver")
    st = ctx.call("vr.status")
    assert [h["model"] for h in st["headsets"]] == ["Valve Index"]
    h = st["headsets"][0]
    assert h["match"] == "vid_pid" and h["support"] == "bon" and h["notes"]
    assert st["support"] == "bon" and "Lighthouse" in st["recommendation"]


def test_detect_vive_and_vive_pro(vr):
    ctx, mod = vr
    usb(mod.sys, "1-1", "0bb4", "2c87", "HTC Vive")
    usb(mod.sys, "2-1", "0bb4", "0309")
    models = sorted(h["model"] for h in ctx.call("vr.status")["headsets"])
    assert models == ["HTC Vive", "HTC Vive Pro"]


def test_unknown_device_not_a_headset(vr):
    ctx, mod = vr
    usb(mod.sys, "1-1", "1234", "abcd", "Mystery")
    usb(mod.sys, "1-2", "28de", "1142", "Steam Controller")  # Valve but not a headset
    st = ctx.call("vr.status")
    assert st["headsets"] == [] and st["support"] == "inconnu"
    assert "WiVRn" in st["recommendation"]


def test_quest_vendor_match_and_name_match(vr):
    assert identify_headset("2833", "9999", None)["match"] == "vendor"
    assert identify_headset("2833", "9999", None)["family"] == "quest"
    assert identify_headset("2833", "0031", None)["model"] == "Oculus Rift CV1"
    assert identify_headset("0483", "0101", "Pimax P2")["match"] == "name"
    assert identify_headset("054c", "0ce6", "DualSense Wireless Controller") is None
    ctx, mod = vr
    usb(mod.sys, "1-1", "2833", "0137", "Quest")
    h = ctx.call("vr.status")["headsets"][0]
    assert h["family"] == "quest" and h["support"] == "limité" and "WiVRn" in h["notes"]


def test_rift_cv1_sensors_not_reported_as_quest(vr):
    ctx, mod = vr
    usb(mod.sys, "1-1", "2833", "0031", "Rift")
    usb(mod.sys, "1-2", "2833", "0211", "Rift Sensor")
    hs = ctx.call("vr.status")["headsets"]
    assert [(h["model"], h["support"]) for h in hs] == [("Oculus Rift CV1", "expérimental")]


def test_active_runtime_user_then_system(vr):
    ctx, mod = vr
    sysfile = mod.etc / "xdg/openxr/1/active_runtime.json"
    w(sysfile, json.dumps({"file_format_version": "1.0.0",
                           "runtime": {"name": "SteamVR", "library_path": "/x/vrclient.so"}}))
    a = ctx.call("vr.status")["active_runtime"]
    assert a["runtime"] == "steamvr" and a["scope"] == "système"
    w(mod.home / ".config/openxr/1/active_runtime.json",
      json.dumps({"file_format_version": "1.0.0",
                  "runtime": {"name": "Monado", "library_path": "/usr/lib64/libopenxr_monado.so"}}))
    a = ctx.call("vr.status")["active_runtime"]
    assert a["runtime"] == "monado" and a["scope"] == "utilisateur"


def test_active_runtime_corrupt(vr):
    ctx, mod = vr
    w(mod.home / ".config/openxr/1/active_runtime.json", "{not json")
    assert ctx.call("vr.status")["active_runtime"]["runtime"] == "inconnu"


def test_steamvr_appmanifest_and_flatpaks(vr, runner):
    ctx, mod = vr
    manifest(mod.home / ".steam/steam/steamapps", "250820", "SteamVR")
    runner.script(["flatpak", "list"], stdout="io.github.wivrn.wivrn\tWiVRn\t0.22\tflathub\n")
    rt = ctx.call("vr.status")["runtimes"]
    assert rt["steamvr"]["installed"] and rt["wivrn"]["installed"]
    assert not rt["monado"]["installed"] and not rt["alvr"]["installed"]


def test_steamvr_absent(vr):
    ctx, mod = vr
    manifest(mod.home / ".steam/steam/steamapps", "620980", "Beat Saber")
    assert ctx.call("vr.status")["runtimes"]["steamvr"]["installed"] is False


def test_apps_known_and_unknown(vr):
    ctx, mod = vr
    lib = mod.home / ".local/share/Steam/steamapps"
    manifest(lib, "546560", "Half-Life: Alyx")
    manifest(lib, "250820", "SteamVR")
    extra = mod.home / "games/steamapps"
    w(lib / "libraryfolders.vdf", f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t\t"{extra.parent}"\n\t}}\n}}\n')
    manifest(extra, "620980", "Beat Saber")
    manifest(extra, "570", "Dota 2")
    out = ctx.call("vr.apps")
    assert sorted(a["appid"] for a in out["apps"]) == ["546560", "620980"]
    assert all(a["vr"] == "oui" for a in out["apps"]) and out["unknown_count"] == 1
    allapps = ctx.call("vr.apps", {"include_unknown": True})["apps"]
    assert {a["appid"]: a["vr"] for a in allapps}["570"] == "inconnu"
    assert "notes" in out and "support" in out


def test_start_runtime_without_monado_unavailable(vr, runner):
    ctx, mod = vr
    with pytest.raises(Unavailable):
        ctx.call("vr.start_runtime")
    assert runner.spawned == []


def test_start_runtime_spawns_monado(vr, runner, monkeypatch):
    ctx, mod = vr
    runner.binaries.add("monado-service")
    res = ctx.call("vr.start_runtime")
    assert runner.spawned == [["monado-service"]] and res["pid"] == 4242
    assert "support" in res and "Monado" in res["notes"]
    assert not any(c and c[0] == "steam" for c in runner.spawned + runner.calls)


def test_stop_runtime_when_not_running(vr, runner):
    ctx, mod = vr
    runner.binaries.add("monado-service")
    assert ctx.call("vr.stop_runtime")["was_running"] is False


def test_set_performance_applies_via_hardware(vr, runner):
    ctx, mod = vr
    out = ctx.call("vr.set_performance", {"level": "performance"})
    assert out["applied"] is True and out["power_profile"] == "performance"
    assert runner.calls[-1] == ["powerprofilesctl", "set", "performance"]
    assert ctx.settings.get("vr.performance") == "performance"
    ctx.call("vr.set_performance", {"level": "economie"})
    assert runner.calls[-1] == ["powerprofilesctl", "set", "power-saver"]


def test_set_performance_without_powerprofilesctl(vr, runner):
    ctx, mod = vr
    runner.binaries.discard("powerprofilesctl")
    out = ctx.call("vr.set_performance", {"level": "equilibre"})
    assert out["applied"] is False and ctx.settings.get("vr.performance") == "equilibre"


def test_set_performance_validation_and_ai(vr):
    ctx, mod = vr
    with pytest.raises(InvalidParams):
        ctx.call("vr.set_performance", {"level": "ultra"})
    with pytest.raises(ConfirmationRequired):
        ctx.call("vr.set_performance", {"level": "performance"}, origin=Origin.AI)


def test_set_performance_without_hardware_module(make_ctx, tmp_path):
    ctx = make_ctx(modules=["vr"])
    out = ctx.call("vr.set_performance", {"level": "performance"})
    assert out["applied"] is False and "indisponible" in out["notes"]


def test_module_status(vr):
    ctx, mod = vr
    usb(mod.sys, "1-1", "03f0", "0580", "HP Reverb Virtual Reality Headset G2")
    usb(mod.sys, "1-2", "045e", "0659", "HoloLens Sensors")
    st = mod.status()
    assert st["ok"] and st["headsets"] == ["HP Reverb G2"] and st["monado_installed"] is False
