import os
from pathlib import Path

import pytest

from nexus.core.errors import InvalidParams, NotFound, PermissionDenied, Unavailable
from nexus.modules.hardware.module import UDEV_LIGHTBAR_RULE, parse_powerprofiles_list
from nexus.security.permissions import Origin
from nexus.core.errors import ConfirmationRequired

MAC = "a0:ab:51:12:34:56"


def w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def add_input(sys: Path, n: int, name, vid, pid, bus="0005", uniq="", js=True):
    d = sys / "class/input" / f"input{n}"
    w(d / "name", name + "\n")
    w(d / "id/vendor", vid + "\n")
    w(d / "id/product", pid + "\n")
    w(d / "id/bustype", bus + "\n")
    w(d / "uniq", uniq + "\n")
    (d / f"event{n}").mkdir()
    if js:
        (d / f"js{n}").mkdir()
    return d


def add_led(sys: Path, n: int, hid_id="0005:0000054C:00000CE6"):
    led = sys / "class/leds" / f"input{n}:rgb:indicator"
    w(led / "multi_index", "red green blue\n")
    w(led / "multi_intensity", "0 0 255\n")
    w(led / "brightness", "0\n")
    w(led / "max_brightness", "255\n")
    w(led / "device/uevent", f"DRIVER=playstation\nHID_ID={hid_id}\nHID_NAME=DualSense Wireless Controller\n")
    return led


@pytest.fixture
def hw(make_ctx, tmp_path):
    ctx = make_ctx(modules=["hardware"])
    mod = ctx.module("hardware")
    mod.sys = tmp_path / "sys"
    mod.proc = tmp_path / "proc"
    mod.sys.mkdir()
    mod.proc.mkdir()
    return ctx, mod


def test_dualsense_detected_with_battery(hw):
    ctx, mod = hw
    add_input(mod.sys, 20, "DualSense Wireless Controller", "054c", "0ce6", "0005", MAC)
    add_input(mod.sys, 21, "DualSense Wireless Controller Motion Sensors", "054c", "0ce6", "0005", MAC, js=False)
    add_input(mod.sys, 22, "AT Translated Set 2 keyboard", "0001", "0001", "0011", js=False)
    bat = mod.sys / "class/power_supply" / f"ps-controller-battery-{MAC}"
    w(bat / "capacity", "70\n")
    w(bat / "status", "Discharging\n")
    add_led(mod.sys, 20)
    out = ctx.call("hardware.controllers")["controllers"]
    assert len(out) == 1
    c = out[0]
    assert c["model"] == "DualSense" and c["connection"] == "bluetooth"
    assert c["battery"] == {"capacity": 70, "status": "Discharging"}
    assert c["lightbar"] == "input20:rgb:indicator"
    assert mod.status()["controllers"] == 1


def test_dualsense_edge_usb_and_proc_fallback(hw):
    ctx, mod = hw
    w(mod.proc / "bus/input/devices",
      'I: Bus=0003 Vendor=054c Product=0df2 Version=8111\n'
      'N: Name="Sony Interactive Entertainment DualSense Edge Wireless Controller"\n'
      'P: Phys=usb-0000:00:14.0-1/input3\n'
      'S: Sysfs=/devices/pci0000:00/usb1/1-1/input/input7\n'
      'U: Uniq=a0:ab:51:00:00:01\n'
      'H: Handlers=event7 js0\n\n'
      'I: Bus=0003 Vendor=046d Product=c52b Version=0111\n'
      'N: Name="Logitech USB Receiver"\n'
      'H: Handlers=sysrq kbd event3\n')
    out = ctx.call("hardware.controllers")["controllers"]
    assert [(c["model"], c["connection"]) for c in out] == [("DualSense Edge", "usb")]


def test_generic_controller(hw):
    ctx, mod = hw
    add_input(mod.sys, 3, "Some Pad", "1234", "5678", "0003")
    c = ctx.call("hardware.controllers")["controllers"][0]
    assert c["model"] == "Manette générique" and c["battery"] is None


def test_lightbar_writes_fake_sysfs(hw):
    ctx, mod = hw
    led = add_led(mod.sys, 20)
    res = ctx.call("hardware.controller_lightbar", {"r": 255, "g": 16, "b": 0})
    assert res["leds"] == ["input20:rgb:indicator"]
    assert (led / "multi_intensity").read_text() == "255 16 0\n"
    assert (led / "brightness").read_text() == "255\n"


def test_lightbar_respects_multi_index_order(hw):
    ctx, mod = hw
    led = add_led(mod.sys, 4)
    (led / "multi_index").write_text("blue green red\n")
    ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3, "controller": "input4:rgb:indicator"})
    assert (led / "multi_intensity").read_text() == "3 2 1\n"


@pytest.mark.parametrize("bad", [256, -1, 1000])
def test_lightbar_rejects_out_of_range(hw, bad):
    ctx, mod = hw
    led = add_led(mod.sys, 20)
    with pytest.raises(InvalidParams):
        ctx.call("hardware.controller_lightbar", {"r": bad, "g": 0, "b": 0})
    with pytest.raises(InvalidParams):
        mod._write_lightbar(led, 0, bad, 0)
    assert (led / "multi_intensity").read_text() == "0 0 255\n"


def test_lightbar_rejects_bool_and_wrong_type(hw):
    ctx, mod = hw
    add_led(mod.sys, 20)
    with pytest.raises(InvalidParams):
        ctx.call("hardware.controller_lightbar", {"r": True, "g": 0, "b": 0})
    with pytest.raises(InvalidParams):
        ctx.call("hardware.controller_lightbar", {"r": "255", "g": 0, "b": 0})


def test_lightbar_refuses_paths_outside_leds(hw, tmp_path):
    ctx, mod = hw
    add_led(mod.sys, 20)
    outside = tmp_path / "etc" / "input20:rgb:indicator"
    w(outside / "multi_intensity", "x")
    for p in [outside, mod.sys / "class/leds/../input/input20:rgb:indicator",
              mod.sys / "class/leds/input20:white:player-1", Path("/etc/passwd")]:
        with pytest.raises(PermissionDenied):
            mod._write_lightbar(p, 1, 2, 3)
    assert (outside / "multi_intensity").read_text() == "x"
    # traversal in the action parameter
    for name in ["../../../etc", "input20:rgb:indicator/../x", "/sys/class/leds/input20:rgb:indicator"]:
        with pytest.raises(InvalidParams):
            ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3, "controller": name})


def test_lightbar_refuses_symlink_escaping_sysfs(hw, tmp_path):
    ctx, mod = hw
    target = tmp_path / "evil"
    w(target / "multi_index", "red green blue\n")
    w(target / "multi_intensity", "x")
    w(target / "brightness", "x")
    w(target / "device/uevent", "HID_ID=0005:0000054C:00000CE6\n")
    (mod.sys / "class/leds").mkdir(parents=True)
    os.symlink(target, mod.sys / "class/leds/input9:rgb:indicator")
    with pytest.raises(PermissionDenied):
        ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3, "controller": "input9:rgb:indicator"})
    assert (target / "multi_intensity").read_text() == "x"


def test_lightbar_refuses_non_dualsense_led(hw):
    ctx, mod = hw
    add_led(mod.sys, 5, hid_id="0003:0000046D:0000C52B")
    with pytest.raises(PermissionDenied):
        ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3, "controller": "input5:rgb:indicator"})
    with pytest.raises(Unavailable):  # no DualSense LED at all when targeting all
        ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3})


def test_lightbar_missing_led(hw):
    ctx, mod = hw
    with pytest.raises(NotFound):
        ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3, "controller": "input1:rgb:indicator"})


def test_lightbar_permission_error_mentions_udev(hw, monkeypatch):
    ctx, mod = hw
    add_led(mod.sys, 20)
    real = Path.write_text

    def deny(self, *a, **k):
        if self.name == "multi_intensity":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", deny)
    with pytest.raises(PermissionDenied) as ei:
        ctx.call("hardware.controller_lightbar", {"r": 1, "g": 2, "b": 3})
    assert "udev" in ei.value.message


def test_udev_rule_content():
    assert 'SUBSYSTEM=="leds"' in UDEV_LIGHTBAR_RULE and "0CE6" in UDEV_LIGHTBAR_RULE
    assert "0DF2" in UDEV_LIGHTBAR_RULE and "multi_intensity" in UDEV_LIGHTBAR_RULE


PPC_LIST = """  performance:
    CpuDriver:\tamd_pstate
    Degraded:   no

* balanced:
    CpuDriver:\tamd_pstate
    PlatformDriver:\tplatform_profile

  power-saver:
    CpuDriver:\tamd_pstate
"""


def test_powerprofiles_list_and_set_argv(hw, runner):
    ctx, mod = hw
    runner.script(["powerprofilesctl", "list"], stdout=PPC_LIST)
    assert parse_powerprofiles_list(PPC_LIST) == (["performance", "balanced", "power-saver"], "balanced")
    out = ctx.call("hardware.performance_profiles")
    assert out == {"profiles": ["performance", "balanced", "power-saver"], "active": "balanced"}
    ctx.call("hardware.set_performance_profile", {"profile": "performance"})
    assert runner.calls[-1] == ["powerprofilesctl", "set", "performance"]


@pytest.mark.parametrize("bad", ["turbo", "performance; rm -rf /", "--help", "Performance"])
def test_set_performance_profile_strict(hw, runner, bad):
    ctx, mod = hw
    with pytest.raises(InvalidParams):
        ctx.call("hardware.set_performance_profile", {"profile": bad})
    assert not any(c[:2] == ["powerprofilesctl", "set"] for c in runner.calls)


def test_set_performance_profile_ai_needs_confirmation(hw):
    ctx, mod = hw
    with pytest.raises(ConfirmationRequired):
        ctx.call("hardware.set_performance_profile", {"profile": "balanced"}, origin=Origin.AI)


def test_powerprofilesctl_missing(hw, runner):
    ctx, mod = hw
    runner.binaries.discard("powerprofilesctl")
    with pytest.raises(Unavailable):
        ctx.call("hardware.performance_profiles")
    assert mod.status()["performance_profile"] is None


def test_overlay_stores_setting(hw):
    ctx, mod = hw
    out = ctx.call("hardware.overlay", {"enabled": True})
    assert out["enabled"] is True and ctx.settings.get("hardware.mangohud") is True
    ctx.call("hardware.overlay", {"enabled": False})
    assert ctx.settings.get("hardware.mangohud") is False


def test_info_reads_fake_proc_and_sysfs(hw, runner):
    ctx, mod = hw
    w(mod.proc / "cpuinfo",
      "processor\t: 0\nmodel name\t: AMD Ryzen 7 5800X 8-Core Processor\nphysical id\t: 0\ncpu cores\t: 8\n"
      "flags\t\t: fpu sse2 avx avx2\n\nprocessor\t: 1\nmodel name\t: AMD Ryzen 7 5800X 8-Core Processor\n"
      "physical id\t: 0\ncpu cores\t: 8\nflags\t\t: fpu sse2 avx avx2\n")
    w(mod.proc / "meminfo", "MemTotal:       16303428 kB\nMemFree:  1 kB\nMemAvailable:   8000000 kB\n")
    w(mod.sys / "class/drm/card1/device/vendor", "0x1002\n")
    w(mod.sys / "class/drm/card1/device/device", "0x73bf\n")
    w(mod.sys / "class/drm/card1-DP-1/status", "connected\n")
    runner.script(["lspci", "-nn"], stdout="03:00.0 VGA compatible controller [0300]: Advanced Micro Devices, "
                  "Inc. [AMD/ATI] Navi 21 [Radeon RX 6800] [1002:73bf] (rev c1)\n")
    runner.script(["vulkaninfo", "--summary"], stdout="GPU0:\n\tapiVersion = 1.3.274\n\tdeviceName = AMD Radeon RX 6800 (RADV NAVI21)\n")
    info = ctx.call("hardware.info")
    assert info["cpu"] == {"model": "AMD Ryzen 7 5800X 8-Core Processor", "threads": 2, "cores": 8, "avx2": True}
    assert info["memory"]["total_bytes"] == 16303428 * 1024
    assert len(info["gpus"]) == 1
    g = info["gpus"][0]
    assert g["vendor"] == "AMD" and g["name"].endswith("Navi 21 [Radeon RX 6800]")
    assert info["vulkan"]["available"] and info["vulkan"]["devices"][0]["api_version"] == "1.3.274"
    assert info["disk"]["free_bytes"] is not None


def test_info_without_tools(hw, runner):
    ctx, mod = hw
    runner.binaries.difference_update({"lspci", "vulkaninfo"})
    w(mod.sys / "class/drm/card0/device/vendor", "0x10de\n")
    info = ctx.call("hardware.info")
    assert info["gpus"][0]["vendor"] == "NVIDIA" and info["gpus"][0]["name"] is None
    assert info["vulkan"]["available"] is False


def test_controller_setup_bluetoothctl(hw, runner):
    ctx, mod = hw
    runner.script(["bluetoothctl", "show"], stdout="Controller 00:1A:7D:DA:71:13 (public)\n\tName: pc\n"
                  "\tPowered: yes\n\tDiscoverable: no\n\tPairable: yes\n")
    out = ctx.call("hardware.controller_setup")
    assert out["bluetooth"] == {"available": True, "address": "00:1A:7D:DA:71:13", "powered": True,
                                "discoverable": False, "pairable": True}
    assert out["automatic_pairing"] is False and any("PS" in s and "Create" in s for s in out["steps"])
    assert runner.calls == [["bluetoothctl", "show"]]


def test_controller_setup_no_bluetooth(hw, runner):
    ctx, mod = hw
    runner.binaries.discard("bluetoothctl")
    assert ctx.call("hardware.controller_setup")["bluetooth"]["available"] is False
