import pytest

from nexus.core.errors import CommandFailed, InvalidParams, Unavailable
from nexus.core.flatpak import Flatpak
from nexus.security.permissions import PermissionDenied
from nexus.security.runner import FakeRunner


def test_installed_parsing():
    r = FakeRunner()
    r.script(["flatpak", "list"], stdout="net.rpcs3.RPCS3\tRPCS3\t0.0.33\tflathub\norg.mozilla.firefox\tFirefox\t140\tflathub\n")
    apps = Flatpak(r).installed()
    assert [a.app_id for a in apps] == ["net.rpcs3.RPCS3", "org.mozilla.firefox"]
    assert Flatpak(r).is_installed("net.rpcs3.RPCS3")


def test_install_argv_and_failures():
    r = FakeRunner()
    fp = Flatpak(r)
    fp.install("net.pcsx2.PCSX2")
    assert r.calls[-1] == ["flatpak", "install", "--system", "-y", "--noninteractive", "flathub", "net.pcsx2.PCSX2"]
    with pytest.raises(PermissionDenied):
        fp.install("net.pcsx2.PCSX2", remote="evilrepo")
    with pytest.raises(InvalidParams):
        fp.install("--from=http://x")
    r.script(["flatpak", "install"], returncode=1, stderr="error: No remote refs found")
    with pytest.raises(CommandFailed):
        fp.install("org.example.Missing")


def test_missing_flatpak_binary():
    r = FakeRunner(binaries=set())
    with pytest.raises(Unavailable):
        Flatpak(r).installed()
