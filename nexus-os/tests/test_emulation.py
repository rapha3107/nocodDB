import struct

import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, NotFound, Unavailable
from nexus.modules.emulation.catalog import EMULATORS, SYSTEMS
from nexus.modules.emulation.library import clean_title, parse_sfo
from nexus.security.permissions import Origin, PermissionDenied
from nexus.security.verify import validate_flatpak_id


# ------------------------------------------------------------------ helpers

def make_sfo(entries: dict) -> bytes:
    """Build a minimal valid PARAM.SFO (utf8 strings + u32 ints)."""
    keys = b""
    data = b""
    index = b""
    for k, v in entries.items():
        koff = len(keys)
        keys += k.encode() + b"\x00"
        if isinstance(v, int):
            raw, fmt = struct.pack("<I", v), 0x0404
            maxlen = 4
        else:
            raw, fmt = v.encode() + b"\x00", 0x0204
            maxlen = (len(raw) + 3) // 4 * 4
        doff = len(data)
        data += raw.ljust(maxlen, b"\x00")
        index += struct.pack("<HHIII", koff, fmt, len(raw), maxlen, doff)
    while len(keys) % 4:
        keys += b"\x00"
    key_table = 20 + len(index)
    data_table = key_table + len(keys)
    header = b"\x00PSF" + struct.pack("<IIII", 0x0101, key_table, data_table, len(entries))
    return header + index + keys + data


INSTALLED = ("org.libretro.RetroArch\tRetroArch\t1.21\tflathub\n"
             "net.pcsx2.PCSX2\tPCSX2\t2.2\tflathub\n"
             "net.rpcs3.RPCS3\tRPCS3\t0.0.36\tflathub\n"
             "org.DolphinEmu.dolphin-emu\tDolphin\t2503\tflathub\n"
             "org.ppsspp.PPSSPP\tPPSSPP\t1.18\tflathub\n"
             "info.cemu.Cemu\tCemu\t2.6\tflathub\n"
             "net.kuribo64.melonDS\tmelonDS\t1.0\tflathub\n"
             "io.mgba.mGBA\tmGBA\t0.10\tflathub\n"
             "app.xemu.xemu\txemu\t0.8\tflathub\n"
             "org.flycast.Flycast\tFlycast\t2.4\tflathub\n"
             "net.shadps4.shadPS4\tshadPS4\t0.6\tflathub\n")


@pytest.fixture
def emu(make_ctx, runner, tmp_path):
    ctx = make_ctx(modules=["emulation"])
    m = ctx.module("emulation")
    m.cpuinfo_path = tmp_path / "cpuinfo"
    m.meminfo_path = tmp_path / "meminfo"
    m.flatpak_system_dir = tmp_path / "flatpak"
    return ctx, m


@pytest.fixture
def roms(emu):
    ctx, m = emu
    rd = m.rom_dir()
    def touch(rel, content=b"x"):
        p = rd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p
    touch("ps1/Final Fantasy VII (Europe) (Disc 1).cue",
          b'FILE "Final Fantasy VII (Europe) (Disc 1).bin" BINARY\n  TRACK 01 MODE2/2352\n')
    touch("ps1/Final Fantasy VII (Europe) (Disc 1).bin")
    touch("ps1/Crash Bandicoot (USA).chd")
    touch("ps1/notes.txt")
    touch("ps2/Shadow of the Colossus (Europe) [!].iso")
    touch("ps2/Shadow of the Colossus (Europe) [!].png", b"\x89PNG")
    touch("ps2/media/covers/Ico (Europe).jpg", b"jpg")
    touch("ps2/sub/Ico (Europe).chd")
    touch("snes/Legend of Zelda, The - A Link to the Past (USA).sfc")
    touch("gba/Metroid Fusion (Europe) (En,Fr,De,Es,It).gba")
    touch("gamecube/Wind Waker (USA).rvz")
    touch("psp/Patapon (Europe).iso")
    touch("wiiu/Mario Kart 8 (Europe).wua")
    touch("nds/Pokemon Black (Europe).nds")
    touch("xbox/Halo (USA).iso")
    g = rd / "ps3" / "Demon's Souls (Europe)"
    touch(f"ps3/{g.name}/PS3_GAME/PARAM.SFO", make_sfo({"TITLE": "Demon's Souls", "TITLE_ID": "BLES00932",
                                                         "PARENTAL_LEVEL": 5}))
    touch(f"ps3/{g.name}/PS3_GAME/USRDIR/EBOOT.BIN")
    touch(f"ps3/{g.name}/PS3_GAME/ICON0.PNG", b"\x89PNG")
    touch("ps3/NPEB00001/PARAM.SFO", make_sfo({"TITLE": "Jeu PSN", "TITLE_ID": "NPEB00001"}))
    touch("ps3/NPEB00001/USRDIR/EBOOT.BIN")
    touch("ps3/Broken/PS3_GAME/nothing.txt")
    return rd


def cpuinfo(threads: int, avx2: bool) -> str:
    flags = "fpu sse sse2 sse4_1 sse4_2 avx" + (" avx2 fma bmi2" if avx2 else "")
    return "".join(f"processor\t: {i}\nmodel name\t: Test CPU\nflags\t\t: {flags}\n\n" for i in range(threads))


# ------------------------------------------------------------------ catalogue

def test_catalog_ids_valid_and_honest():
    for key, e in EMULATORS.items():
        if e["flatpak_id"]:
            validate_flatpak_id(e["flatpak_id"])
        assert e["compatibility"]["level"] in {"stable", "bon", "expérimental", "limité", "non supporté"}
        assert e["license"]
        for s in e["systems"]:
            assert s in SYSTEMS
    assert EMULATORS["shadps4"]["compatibility"]["level"] == "expérimental"
    assert EMULATORS["xenia"]["installable"] is False
    assert EMULATORS["duckstation"]["installable"] is False
    for s in SYSTEMS.values():
        for e in s["emulators"]:
            assert e in EMULATORS
    assert SYSTEMS["xbox360"]["support"] == "non supporté"


def test_systems_and_emulators_installed_status(emu, runner):
    ctx, _ = emu
    runner.script(["flatpak", "list"], stdout=INSTALLED)
    systems = {s["id"]: s for s in ctx.call("emulation.systems")}
    assert systems["ps2"]["recommended_emulator"] == "pcsx2" and systems["ps2"]["installed"] is True
    assert systems["3ds"]["installed"] is False
    assert systems["ps1"]["recommended_emulator"] == "retroarch"
    assert systems["xbox360"]["installed"] is False
    emus = {e["id"]: e for e in ctx.call("emulation.emulators")["emulators"]}
    assert emus["rpcs3"]["installed"] is True and emus["azahar"]["installed"] is False
    assert "rom_args" not in emus["rpcs3"] and emus["shadps4"]["direct_launch"] is False


def test_emulators_without_flatpak(make_ctx, runner):
    runner.binaries.discard("flatpak")
    ctx = make_ctx(modules=["emulation"])
    res = ctx.call("emulation.emulators")
    assert res["flatpak_available"] is False
    assert all(e["installed"] is None for e in res["emulators"])
    st = ctx.module("emulation").status()
    assert st["ok"] is False and "Flatpak" in st["message"]


def test_details_saves_and_bios(emu):
    ctx, m = emu
    (m.home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios").mkdir(parents=True)
    (m.home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios/scph.bin").write_bytes(b"x")
    d = ctx.call("emulation.details", {"emulator": "pcsx2"})
    assert any("memcards" in s for s in d["saves"])
    assert d["bios_dir_has_files"] is True
    d = ctx.call("emulation.details", {"emulator": "rpcs3"})
    assert "Sony" in d["bios"][0]["description"]
    assert d["bios_dir_has_files"] is False


# ------------------------------------------------------------------ titles / SFO

@pytest.mark.parametrize("name,title,region,disc", [
    ("Final Fantasy VII (Europe) (Disc 1).cue", "Final Fantasy VII", "Europe", 1),
    ("Super Mario World (USA) [!].sfc", "Super Mario World", "USA", None),
    ("Legend of Zelda, The - A Link to the Past (USA).sfc", "The Legend of Zelda - A Link to the Past", "USA", None),
    ("Metroid Fusion (Europe) (En,Fr,De,Es,It).gba", "Metroid Fusion", "Europe", None),
    ("Gran_Turismo_4 (Japan) (v1.01).iso", "Gran Turismo 4", "Japon", None),
    ("Tekken 3.chd", "Tekken 3", None, None),
])
def test_clean_title(name, title, region, disc):
    r = clean_title(name)
    assert (r["title"], r["region"], r["disc"]) == (title, region, disc)


def test_parse_sfo():
    sfo = parse_sfo(make_sfo({"CATEGORY": "DG", "TITLE": "Démon’s Souls", "TITLE_ID": "BLES00932",
                              "PARENTAL_LEVEL": 5}))
    assert sfo == {"CATEGORY": "DG", "TITLE": "Démon’s Souls", "TITLE_ID": "BLES00932", "PARENTAL_LEVEL": 5}
    for bad in [b"", b"NOPE" + b"\x00" * 30, b"\x00PSF" + struct.pack("<IIII", 0x101, 9999, 9999, 3)]:
        with pytest.raises(ValueError):
            parse_sfo(bad)


# ------------------------------------------------------------------ scan

def test_scan_library(emu, roms):
    ctx, m = emu
    res = ctx.call("emulation.scan")
    assert res["counts"]["ps1"] == 2          # .bin referenced by .cue hidden, .txt ignored
    assert res["counts"]["ps2"] == 2          # media/ not scanned, sub-folders are
    assert res["counts"]["ps3"] == 2          # Broken/ has no PARAM.SFO
    by_id = {r["id"]: r for r in ctx.call("emulation.roms")}
    ff = by_id["ps1/Final Fantasy VII (Europe) (Disc 1).cue"]
    assert ff["title"] == "Final Fantasy VII" and ff["disc"] == 1 and ff["region"] == "Europe"
    ds = by_id["ps3/Demon's Souls (Europe)"]
    assert ds["title"] == "Demon's Souls" and ds["title_id"] == "BLES00932"
    assert ds["cover"] == "cover/ps3/Demon's Souls (Europe)/PS3_GAME/ICON0.PNG"
    assert by_id["ps3/NPEB00001"]["title"] == "Jeu PSN"
    sotc = by_id["ps2/Shadow of the Colossus (Europe) [!].iso"]
    assert sotc["title"] == "Shadow of the Colossus"
    assert m.serve_file(sotc["cover"]) == (roms / "ps2/Shadow of the Colossus (Europe) [!].png").resolve()
    ico = by_id["ps2/sub/Ico (Europe).chd"]
    assert ico["cover"] == "cover/ps2/media/covers/Ico (Europe).jpg"
    assert ctx.call("emulation.roms", {"system": "snes"})[0]["title"].startswith("The Legend of Zelda")
    st = m.status()
    assert st["roms_per_system"]["ps1"] == 2 and st["rom_dir"] == str(roms)


def test_serve_file_refuses_traversal_and_non_images(emu, roms):
    _, m = emu
    assert m.serve_file("cover/ps1/../../../../etc/passwd") is None
    assert m.serve_file("cover/ps1/%2e%2e/%2e%2e/secret.png") is None
    assert m.serve_file("cover/ps1/Crash Bandicoot (USA).chd") is None
    assert m.serve_file("other/ps1/x.png") is None


def test_rom_dir_setting(emu, tmp_path):
    ctx, m = emu
    custom = tmp_path / "MyRoms"
    (custom / "nds").mkdir(parents=True)
    (custom / "nds" / "Mario Kart DS (Europe).nds").write_bytes(b"x")
    ctx.settings.set("emulation.rom_dir", str(custom))
    assert ctx.call("emulation.scan")["counts"] == {"nds": 1}
    ctx.settings.set("emulation.rom_dir", "~/Jeux")
    assert m.rom_dir() == m.home / "Jeux"


def test_prepare_folders(emu):
    ctx, m = emu
    res = ctx.call("emulation.prepare_folders")
    assert "ps2" in res["created"] and "xbox360" not in res["created"]
    assert (m.rom_dir() / "ps2" / "media" / "covers").is_dir()


# ------------------------------------------------------------------ recommend

def test_recommend_without_avx2(emu, runner, tmp_path):
    ctx, m = emu
    m.cpuinfo_path.write_text(cpuinfo(4, avx2=False))
    m.meminfo_path.write_text("MemTotal:        8000000 kB\n")
    runner.script(["vulkaninfo", "--summary"], stdout="GPU0:\n\tapiVersion = 1.3.280\n\tdeviceName = AMD Radeon RX 6600\n")
    r = ctx.call("emulation.recommend", {"system": "ps3"})
    assert r["recommended"]["id"] == "rpcs3"
    assert any("AVX2" in w and "lent" in w for w in r["warnings"])
    assert any("6" in w and "threads" in w for w in r["warnings"])
    assert r["hardware"]["vulkan"] is True and r["hardware"]["vulkan_api"] == "1.3.280"
    assert r["hardware"]["threads"] == 4 and r["hardware"]["avx2"] is False
    assert ["vulkaninfo", "--summary"] in runner.calls
    ps4 = ctx.call("emulation.recommend", {"system": "ps4"})
    assert ps4["support"] == "expérimental"
    assert any("AVX2" in w and "démarrer" in w for w in ps4["warnings"])


def test_recommend_with_avx2(emu, runner):
    ctx, m = emu
    m.cpuinfo_path.write_text(cpuinfo(16, avx2=True))
    m.meminfo_path.write_text("MemTotal:       32000000 kB\n")
    runner.script(["vulkaninfo", "--summary"], stdout="\tapiVersion = 1.3.280\n\tdeviceName = NVIDIA RTX 4070\n")
    r = ctx.call("emulation.recommend", {"system": "ps3"})
    assert r["warnings"] == []
    assert r["hardware"]["ram_gb"] > 30
    assert ctx.call("emulation.recommend", {"system": "ps1"})["retroarch_cores"][0] == "swanstation"


def test_recommend_no_vulkan_and_unknown_cpu(emu, runner):
    ctx, m = emu
    runner.binaries.discard("vulkaninfo")
    r = ctx.call("emulation.recommend", {"system": "ps3"})
    assert any("Vulkan non vérifié" in w for w in r["warnings"])
    assert any("CPU indisponibles" in w for w in r["warnings"])
    runner.binaries.add("vulkaninfo")
    runner.script(["vulkaninfo"], returncode=1, stderr="ERROR_INITIALIZATION_FAILED")
    r = ctx.call("emulation.recommend", {"system": "ps3"})
    assert any("Aucun périphérique Vulkan" in w for w in r["warnings"])


def test_recommend_xbox360_not_supported(emu):
    ctx, _ = emu
    r = ctx.call("emulation.recommend", {"system": "xbox360"})
    assert r["recommended"] is None and r["support"] == "non supporté"
    assert r["alternatives"][0]["id"] == "xenia" and r["alternatives"][0]["installable"] is False


# ------------------------------------------------------------------ install / uninstall

def test_install_runs_flatpak_in_task(emu, runner):
    ctx, _ = emu
    res = ctx.call("emulation.install", {"emulator": "pcsx2"}, origin=Origin.AI)
    t = ctx.tasks.wait(res["task_id"])
    assert t.state == "done", t.error
    assert ["flatpak", "install", "--system", "-y", "--noninteractive", "flathub", "net.pcsx2.PCSX2"] in runner.calls


def test_install_unverified_availability_checks_remote(emu, runner):
    ctx, _ = emu
    runner.script(["flatpak", "remote-info"], returncode=1, stderr="error: ref not found")
    t = ctx.tasks.wait(ctx.call("emulation.install", {"emulator": "azahar"})["task_id"])
    assert t.state == "failed" and t.error["error"] == "not_found"
    assert not any(c[:2] == ["flatpak", "install"] for c in runner.calls)


def test_install_failure_reported(emu, runner):
    ctx, _ = emu
    runner.script(["flatpak", "install"], returncode=1, stderr="error: network")
    t = ctx.tasks.wait(ctx.call("emulation.install", {"emulator": "rpcs3"})["task_id"])
    assert t.state == "failed" and t.error["error"] == "command_failed"


def test_install_refuses_xbox360_and_duckstation(emu, runner):
    ctx, _ = emu
    for key in ("xenia", "duckstation"):
        with pytest.raises(Unavailable):
            ctx.call("emulation.install", {"emulator": key}, origin=Origin.AI)
    with pytest.raises(InvalidParams):
        ctx.call("emulation.install", {"emulator": "evil; rm -rf"})
    assert not any(c[:2] == ["flatpak", "install"] for c in runner.calls)


def test_uninstall_requires_confirmation(emu, runner):
    ctx, _ = emu
    runner.script(["flatpak", "list"], stdout=INSTALLED)
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("emulation.uninstall", {"emulator": "pcsx2"}, origin=Origin.AI)
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    res = ctx.call("emulation.uninstall", {"emulator": "pcsx2"}, origin=Origin.AI, confirmation_id=cid)
    assert ctx.tasks.wait(res["task_id"]).state == "done"
    assert ["flatpak", "uninstall", "--system", "-y", "--noninteractive", "net.pcsx2.PCSX2"] in runner.calls


# ------------------------------------------------------------------ launch

@pytest.fixture
def launchable(emu, roms, runner):
    runner.script(["flatpak", "list"], stdout=INSTALLED)
    return emu


def test_launch_argv_per_emulator(launchable, runner, roms):
    ctx, m = launchable
    cases = {
        "ps2/Shadow of the Colossus (Europe) [!].iso":
            ["net.pcsx2.PCSX2", "-batch", "-fullscreen", str(roms / "ps2/Shadow of the Colossus (Europe) [!].iso")],
        "ps3/Demon's Souls (Europe)":
            ["net.rpcs3.RPCS3", "--no-gui", str(roms / "ps3/Demon's Souls (Europe)/PS3_GAME/USRDIR/EBOOT.BIN")],
        "ps3/NPEB00001": ["net.rpcs3.RPCS3", "--no-gui", str(roms / "ps3/NPEB00001/USRDIR/EBOOT.BIN")],
        "gamecube/Wind Waker (USA).rvz":
            ["org.DolphinEmu.dolphin-emu", "-b", "-e", str(roms / "gamecube/Wind Waker (USA).rvz")],
        "psp/Patapon (Europe).iso": ["org.ppsspp.PPSSPP", str(roms / "psp/Patapon (Europe).iso")],
        "wiiu/Mario Kart 8 (Europe).wua": ["info.cemu.Cemu", "-f", "-g", str(roms / "wiiu/Mario Kart 8 (Europe).wua")],
        "nds/Pokemon Black (Europe).nds": ["net.kuribo64.melonDS", str(roms / "nds/Pokemon Black (Europe).nds")],
        "gba/Metroid Fusion (Europe) (En,Fr,De,Es,It).gba":
            ["io.mgba.mGBA", str(roms / "gba/Metroid Fusion (Europe) (En,Fr,De,Es,It).gba")],
        "xbox/Halo (USA).iso": ["app.xemu.xemu", "-dvd_path", str(roms / "xbox/Halo (USA).iso")],
    }
    for rom_id, expected in cases.items():
        res = ctx.call("emulation.launch", {"rom_id": rom_id}, origin=Origin.AI)
        assert res["rom_loaded"] is True
        assert runner.spawned[-1] == ["flatpak", "run", *[str(x) for x in expected]], rom_id
        assert isinstance(res["pid"], int)


def test_launch_retroarch_core(launchable, runner, roms):
    ctx, m = launchable
    rom_id = "snes/Legend of Zelda, The - A Link to the Past (USA).sfc"
    with pytest.raises(Unavailable) as ei:
        ctx.call("emulation.launch", {"rom_id": rom_id})
    assert "snes9x_libretro.so" in ei.value.message
    sysdir = m.flatpak_system_dir / "app/org.libretro.RetroArch/current/active/files/lib/libretro"
    sysdir.mkdir(parents=True)
    (sysdir / "bsnes_libretro.so").write_bytes(b"")
    ctx.call("emulation.launch", {"rom_id": rom_id})
    assert runner.spawned[-1] == ["flatpak", "run", "org.libretro.RetroArch", "-f", "-L",
                                  "/app/lib/libretro/bsnes_libretro.so", str(roms / rom_id)]
    user = m.home / ".var/app/org.libretro.RetroArch/config/retroarch/cores"
    user.mkdir(parents=True)
    (user / "snes9x_libretro.so").write_bytes(b"")
    ctx.call("emulation.launch", {"rom_id": rom_id})
    assert runner.spawned[-1][5] == str(user / "snes9x_libretro.so")


def test_launch_path_traversal_refused(launchable, runner, roms, tmp_path):
    ctx, _ = launchable
    outside = tmp_path / "secret.iso"
    outside.write_bytes(b"x")
    for rid in ["ps2/../../../secret.iso", "ps2/" + str(outside), "ps2/../ps1/Crash Bandicoot (USA).chd"]:
        with pytest.raises(PermissionDenied):
            ctx.call("emulation.launch", {"rom_id": rid})
    (roms / "ps2" / "link.iso").symlink_to(outside)
    with pytest.raises(PermissionDenied):
        ctx.call("emulation.launch", {"rom_id": "ps2/link.iso"})
    assert runner.spawned == []


def test_launch_rejects_bad_ids_and_types(launchable, runner, roms):
    ctx, _ = launchable
    with pytest.raises(InvalidParams):
        ctx.call("emulation.launch", {"rom_id": "nope"})
    with pytest.raises(NotFound):
        ctx.call("emulation.launch", {"rom_id": "amiga/x.adf"})
    with pytest.raises(InvalidParams):
        ctx.call("emulation.launch", {"rom_id": "ps1/notes.txt"})
    with pytest.raises(NotFound):
        ctx.call("emulation.launch", {"rom_id": "ps3/Broken"})
    with pytest.raises(NotFound):
        ctx.call("emulation.launch", {"rom_id": "ps2/missing.iso"})
    with pytest.raises(Unavailable):
        ctx.call("emulation.launch", {"rom_id": "xbox360/game.iso"})
    with pytest.raises(InvalidParams):
        ctx.call("emulation.launch", {"rom_id": "ps2/Shadow of the Colossus (Europe) [!].iso", "emulator": "dolphin"})
    assert runner.spawned == []


def test_launch_requires_installed_emulator(emu, roms, runner):
    ctx, _ = emu
    runner.script(["flatpak", "list"], stdout="org.mozilla.firefox\tFirefox\t140\tflathub\n")
    with pytest.raises(Unavailable) as ei:
        ctx.call("emulation.launch", {"rom_id": "ps2/Shadow of the Colossus (Europe) [!].iso"})
    assert ei.value.details["recommended"] == "pcsx2"
    assert runner.spawned == []


def test_launch_without_direct_syntax_opens_emulator(launchable, runner, roms):
    ctx, m = launchable
    d = roms / "ps4" / "CUSA00001"
    (d / "sce_sys").mkdir(parents=True)
    (d / "sce_sys" / "param.sfo").write_bytes(make_sfo({"TITLE": "Jeu PS4", "TITLE_ID": "CUSA00001"}))
    ctx.call("emulation.scan")
    assert ctx.call("emulation.roms", {"system": "ps4"})[0]["title"] == "Jeu PS4"
    res = ctx.call("emulation.launch", {"rom_id": "ps4/CUSA00001"})
    assert res["rom_loaded"] is False and "interface" in res["message"]
    assert runner.spawned[-1] == ["flatpak", "run", "net.shadps4.shadPS4"]


def test_controller_help(emu):
    ctx, _ = emu
    assert "SDL2" in ctx.call("emulation.controller_help")["summary"]
