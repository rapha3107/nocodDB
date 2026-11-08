"""Tests for nexus.modules.system (bootc / rpm-ostree / power / logs / diagnostics)."""

import json

import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, NotFound, PermissionDenied, Unavailable
from nexus.modules.system.parse import parse_bootc_status, parse_rpm_ostree_status, parse_upgrade_check
from nexus.security.permissions import Origin
from nexus.security.runner import validate_argv

# Shape of `bootc status --format=json` (bootc 1.x, BootcHost org.containers.bootc/v1).
BOOTC_STATUS = {
    "apiVersion": "org.containers.bootc/v1",
    "kind": "BootcHost",
    "metadata": {"name": "host"},
    "spec": {"image": {"image": "ghcr.io/acme/nexus-os:stable", "transport": "registry"},
             "bootOrder": "default"},
    "status": {
        "staged": {
            "image": {"image": {"image": "ghcr.io/acme/nexus-os:stable", "transport": "registry"},
                      "version": "45.20261108", "timestamp": "2026-11-08T04:12:00Z",
                      "imageDigest": "sha256:" + "c" * 64},
            "cachedUpdate": None, "incompatible": False, "pinned": False,
            "store": "ostreeContainer", "ostree": {"checksum": "c1" * 32, "deploySerial": 0}},
        "booted": {
            "image": {"image": {"image": "ghcr.io/acme/nexus-os:stable", "transport": "registry"},
                      "version": "45.20261105", "timestamp": "2026-11-05T04:10:00Z",
                      "imageDigest": "sha256:" + "b" * 64},
            "cachedUpdate": None, "incompatible": False, "pinned": False,
            "store": "ostreeContainer", "ostree": {"checksum": "b1" * 32, "deploySerial": 0}},
        "rollback": {
            "image": {"image": {"image": "ghcr.io/acme/nexus-os:stable", "transport": "registry"},
                      "version": "45.20261101", "timestamp": "2026-11-01T04:10:00Z",
                      "imageDigest": "sha256:" + "a" * 64},
            "cachedUpdate": None, "incompatible": False, "pinned": False,
            "store": "ostreeContainer", "ostree": {"checksum": "a1" * 32, "deploySerial": 0}},
        "rollbackQueued": False,
        "type": "bootcHost",
    },
}

# Shape of `rpm-ostree status --json` (booted deployment + rollback, no staged update).
RPM_OSTREE_STATUS = {
    "deployments": [
        {"id": "default-b1", "osname": "default", "serial": 0, "checksum": "b1" * 32,
         "version": "45.20261105", "timestamp": 1762315800, "booted": True, "staged": False,
         "pinned": False, "unlocked": "none",
         "container-image-reference": "ostree-image-signed:docker://ghcr.io/acme/nexus-os:stable",
         "container-image-reference-digest": "sha256:" + "b" * 64},
        {"id": "default-a1", "osname": "default", "serial": 0, "checksum": "a1" * 32,
         "version": "45.20261101", "timestamp": 1761969000, "booted": False, "staged": False,
         "pinned": False, "unlocked": "none",
         "container-image-reference": "ostree-image-signed:docker://ghcr.io/acme/nexus-os:stable",
         "container-image-reference-digest": "sha256:" + "a" * 64},
    ],
    "transaction": None,
    "cached-update": None,
    "update-driver": None,
}

BOOTC_CHECK_AVAILABLE = """Update available for: docker://ghcr.io/acme/nexus-os:stable
  Version: 45.20261108
  Digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
Total new layers: 3    Size: 412.6 MB
"""
RPM_OSTREE_CHECK_AVAILABLE = """1 metadata, 0 content objects fetched; 153 KiB transferred in 1 seconds; 0 bytes content written
AvailableUpdate:
        Version: 45.20261108 (2026-11-08T04:12:00Z)
         Digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
"""


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(modules=["system"])


def test_module_loads_without_errors(ctx):
    assert "system" in ctx.registry.modules
    assert ctx.registry.load_errors == {}
    names = {a["action"] for a in ctx.registry.describe() if a["action"].startswith("system.")}
    for required in ("status", "update_check", "update_apply", "rollback", "power", "logs", "diagnostics"):
        assert f"system.{required}" in names


def test_argv_used_by_module_pass_core_validators():
    for argv in (["bootc", "status", "--format=json"], ["bootc", "upgrade", "--check"], ["bootc", "upgrade"],
                 ["bootc", "rollback"], ["rpm-ostree", "status", "--json"], ["rpm-ostree", "upgrade", "--check"],
                 ["rpm-ostree", "upgrade"], ["rpm-ostree", "rollback"], ["systemctl", "poweroff"],
                 ["systemctl", "reboot"], ["systemctl", "suspend"], ["loginctl", "terminate-session", "3"],
                 ["journalctl", "--user", "-u", "nexusd", "-n", "200", "--no-pager", "-o", "short-iso"]):
        validate_argv(argv)
    with pytest.raises(PermissionDenied):
        validate_argv(["bootc", "switch", "ghcr.io/evil/os"])
    with pytest.raises(PermissionDenied):
        validate_argv(["journalctl", "-u", "nexusd"])


# ---- parsers -------------------------------------------------------------------

def test_parse_bootc_status():
    d = parse_bootc_status(json.dumps(BOOTC_STATUS))
    assert d["backend"] == "bootc"
    assert d["booted"]["version"] == "45.20261105"
    assert d["staged"]["version"] == "45.20261108"
    assert d["rollback"]["version"] == "45.20261101"
    assert d["tracking"] == "ghcr.io/acme/nexus-os:stable"
    assert d["booted"]["digest"] == "sha256:" + "b" * 64


def test_parse_rpm_ostree_status_strips_transport_prefix():
    d = parse_rpm_ostree_status(json.dumps(RPM_OSTREE_STATUS))
    assert d["backend"] == "rpm-ostree"
    assert d["booted"]["image"] == "ghcr.io/acme/nexus-os:stable"
    assert d["staged"] is None
    assert d["rollback"]["version"] == "45.20261101"


def test_parse_rpm_ostree_status_with_staged_first():
    data = json.loads(json.dumps(RPM_OSTREE_STATUS))
    staged = dict(data["deployments"][0], booted=False, staged=True, version="45.20261108", checksum="c1" * 32)
    data["deployments"].insert(0, staged)
    d = parse_rpm_ostree_status(json.dumps(data))
    assert d["staged"]["version"] == "45.20261108"
    assert d["booted"]["version"] == "45.20261105"
    assert d["rollback"]["version"] == "45.20261101"


@pytest.mark.parametrize("bad", ["", "not json", "[]", json.dumps({"x": 1})])
def test_parsers_reject_garbage(bad):
    with pytest.raises(ValueError):
        parse_bootc_status(bad)
    with pytest.raises(ValueError):
        parse_rpm_ostree_status(bad)


def test_parse_upgrade_check_variants():
    assert parse_upgrade_check("bootc", 0, BOOTC_CHECK_AVAILABLE) == {
        "available": True, "version": "45.20261108", "digest": "sha256:" + "c" * 64}
    assert parse_upgrade_check("bootc", 0, "No changes in: docker://ghcr.io/acme/nexus-os:stable\n")["available"] is False
    assert parse_upgrade_check("bootc", 0, "No update available.\n")["available"] is False
    r = parse_upgrade_check("rpm-ostree", 0, RPM_OSTREE_CHECK_AVAILABLE)
    assert r["available"] is True and r["version"].startswith("45.20261108")
    assert parse_upgrade_check("rpm-ostree", 77, "No updates available.\n")["available"] is False
    with pytest.raises(RuntimeError):
        parse_upgrade_check("bootc", 1, "", "error: This command requires root privileges")


# ---- status ---------------------------------------------------------------------

def test_status_uses_bootc(ctx, runner):
    runner.script(["bootc", "status"], stdout=json.dumps(BOOTC_STATUS))
    s = ctx.call("system.status")
    assert s["backend"] == "bootc"
    assert s["os_version"] == "45.20261105"
    assert s["reboot_required"] is True  # a staged deployment is waiting
    assert s["nexus"]["version"]
    assert runner.calls == [["bootc", "status", "--format=json"]]


def test_status_falls_back_to_rpm_ostree_when_bootc_refuses(ctx, runner):
    runner.script(["bootc", "status"], returncode=1, stderr="error: This command requires root privileges")
    runner.script(["rpm-ostree", "status"], stdout=json.dumps(RPM_OSTREE_STATUS))
    s = ctx.call("system.status")
    assert s["backend"] == "rpm-ostree"
    assert s["reboot_required"] is False
    assert s["rollback"]["version"] == "45.20261101"
    assert s["fallback_from"][0]["backend"] == "bootc"


def test_status_without_backends_is_unavailable(ctx, runner):
    runner.binaries -= {"bootc", "rpm-ostree"}
    with pytest.raises(Unavailable):
        ctx.call("system.status")
    assert ctx.module("system").status()["ok"] is False


def test_backend_setting_forces_rpm_ostree(ctx, runner):
    ctx.settings.set("system.update_backend", "rpm-ostree")
    runner.script(["rpm-ostree", "status"], stdout=json.dumps(RPM_OSTREE_STATUS))
    assert ctx.call("system.status")["backend"] == "rpm-ostree"
    assert all(c[0] == "rpm-ostree" for c in runner.calls)


def test_nexus_image_info_is_reported(make_ctx, runner, tmp_path):
    info = tmp_path / "image-info.json"
    info.write_text(json.dumps({"nexus-version": "0.1.0", "base-image": "ghcr.io/ublue-os/bazzite:stable"}))
    ctx = make_ctx(modules=["system"])
    ctx.module("system").image_info_path = info
    runner.script(["bootc", "status"], stdout=json.dumps(BOOTC_STATUS))
    assert ctx.call("system.status")["nexus"]["image"]["base-image"] == "ghcr.io/ublue-os/bazzite:stable"


# ---- update_check ------------------------------------------------------------------

def test_update_check_reports_system_and_flatpak(ctx, runner):
    runner.script(["bootc", "upgrade", "--check"], stdout=BOOTC_CHECK_AVAILABLE)
    runner.script(["flatpak", "remote-ls"], stdout="Application ID\norg.mozilla.firefox\nnet.rpcs3.RPCS3\n")
    r = ctx.call("system.update_check", origin=Origin.AI)  # READ: allowed for the AI
    assert r["system"]["available"] is True
    assert r["system"]["version"] == "45.20261108"
    assert r["flatpak"]["apps"] == ["org.mozilla.firefox", "net.rpcs3.RPCS3"]
    assert r["any_available"] is True
    # never upgrades anything
    assert not any(c[:2] in (["bootc", "upgrade"], ["rpm-ostree", "upgrade"]) and "--check" not in c
                   for c in runner.calls)


def test_update_check_rpm_ostree_77_means_up_to_date(ctx, runner):
    runner.script(["bootc", "upgrade"], returncode=1, stderr="error: This command requires root privileges")
    runner.script(["rpm-ostree", "upgrade", "--check"], returncode=77, stdout="No updates available.\n")
    runner.script(["flatpak", "remote-ls"], stdout="")
    r = ctx.call("system.update_check")
    assert r["system"]["backend"] == "rpm-ostree"
    assert r["system"]["available"] is False
    assert r["flatpak"]["available"] is False
    assert r["any_available"] is False


def test_update_check_reports_errors_without_raising(ctx, runner):
    runner.script(["bootc", "upgrade"], returncode=1, stderr="network unreachable")
    runner.script(["rpm-ostree", "upgrade"], returncode=1, stderr="network unreachable")
    runner.binaries.discard("flatpak")
    r = ctx.call("system.update_check")
    assert r["system"]["available"] is None
    assert r["system"]["error"]["error"] == "command_failed"
    assert r["flatpak"]["error"]["error"] == "unavailable"


# ---- update_apply / rollback (SYSTEM) --------------------------------------------------

def _approve(ctx, action, params=None, origin=Origin.USER):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call(action, params or {}, origin=origin)
    cid = ei.value.details["confirmation_id"]
    pending = ctx.permissions.approve(cid, Origin.USER)
    return ctx.call(pending.action, pending.params, origin=pending.origin, confirmation_id=cid)


def test_update_apply_requires_confirmation_and_never_reboots(ctx, runner):
    runner.script(["bootc", "upgrade"], stdout="Fetching ghcr.io/acme/nexus-os:stable\nQueued for next boot: ghcr.io/acme/nexus-os:stable\n")
    with pytest.raises(ConfirmationRequired):
        ctx.call("system.update_apply", origin=Origin.AI)
    assert runner.calls == []
    res = _approve(ctx, "system.update_apply", origin=Origin.AI)
    t = ctx.tasks.wait(res["task_id"], timeout=5)
    assert t.state == "done", t.error
    assert t.result["staged"] is True and t.result["reboot_required"] is True
    assert "Redémarrez" in t.result["message"]
    assert ["bootc", "upgrade"] in runner.calls
    assert not any(c[0] == "systemctl" for c in runner.calls)


def test_update_apply_falls_back_to_rpm_ostree(ctx, runner):
    runner.script(["bootc", "upgrade"], returncode=1, stderr="error: This command requires root privileges")
    runner.script(["rpm-ostree", "upgrade"], stdout="Staging deployment... done\nRun \"systemctl reboot\" to start a reboot\n")
    res = _approve(ctx, "system.update_apply")
    t = ctx.tasks.wait(res["task_id"], timeout=5)
    assert t.state == "done", t.error
    assert t.result["backend"] == "rpm-ostree"
    assert t.result["fallback_from"][0]["backend"] == "bootc"
    assert not any(c[0] == "systemctl" for c in runner.calls)


def test_update_apply_already_up_to_date(ctx, runner):
    runner.script(["bootc", "upgrade"], stdout="No changes in: docker://ghcr.io/acme/nexus-os:stable\n")
    t = ctx.tasks.wait(_approve(ctx, "system.update_apply")["task_id"], timeout=5)
    assert t.state == "done"
    assert t.result["staged"] is False and t.result["reboot_required"] is False


def test_update_apply_failure_marks_task_failed(ctx, runner):
    runner.script(["bootc", "upgrade"], returncode=1, stderr="boom")
    runner.script(["rpm-ostree", "upgrade"], returncode=1, stderr="error: Remote unreachable")
    t = ctx.tasks.wait(_approve(ctx, "system.update_apply")["task_id"], timeout=5)
    assert t.state == "failed"
    assert t.error["error"] == "command_failed"


def test_rollback_requires_confirmation_and_runs(ctx, runner):
    runner.script(["bootc", "status"], stdout=json.dumps(BOOTC_STATUS))
    runner.script(["bootc", "rollback"], stdout="Next boot: rollback deployment\n")
    with pytest.raises(ConfirmationRequired):
        ctx.call("system.rollback")
    assert not any(c[:2] == ["bootc", "rollback"] for c in runner.calls)
    r = _approve(ctx, "system.rollback")
    assert r["reboot_required"] is True
    assert r["target"]["version"] == "45.20261101"
    assert ["bootc", "rollback"] in runner.calls


def test_rollback_without_previous_deployment(ctx, runner):
    data = json.loads(json.dumps(RPM_OSTREE_STATUS))
    data["deployments"] = data["deployments"][:1]
    ctx.settings.set("system.update_backend", "rpm-ostree")
    runner.script(["rpm-ostree", "status"], stdout=json.dumps(data))
    with pytest.raises(NotFound):
        _approve(ctx, "system.rollback")
    assert ["rpm-ostree", "rollback"] not in runner.calls


# ---- power ------------------------------------------------------------------------------

def test_suspend_is_direct(ctx, runner):
    r = ctx.call("system.power", {"action": "suspend"}, origin=Origin.AI)
    assert r == {"action": "suspend", "done": True}
    assert runner.calls == [["systemctl", "suspend"]]


@pytest.mark.parametrize("act,argv", [("shutdown", ["systemctl", "poweroff"]), ("reboot", ["systemctl", "reboot"])])
def test_shutdown_and_reboot_need_confirmation(ctx, runner, act, argv):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("system.power", {"action": act}, origin=Origin.AI)
    assert runner.calls == []
    # The confirmation is bound to the SYSTEM-level action with the exact parameter.
    assert ei.value.details["action"] == "system.power_system"
    cid = ei.value.details["confirmation_id"]
    with pytest.raises(PermissionDenied):
        ctx.permissions.approve(cid, Origin.AI)
    pending = ctx.permissions.approve(cid, Origin.USER)
    assert pending.params == {"action": act}
    r = ctx.call(pending.action, pending.params, origin=pending.origin, confirmation_id=cid)
    assert r["done"] is True
    assert runner.calls == [argv]


def test_confirmation_cannot_be_reused_for_another_power_action(ctx, runner):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("system.power", {"action": "logout"})
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    with pytest.raises(ConfirmationRequired):
        ctx.call("system.power_system", {"action": "shutdown"}, confirmation_id=cid)
    assert runner.calls == []


def test_logout_uses_session_id(ctx, runner):
    ctx.module("system").session_id = "7"
    _approve(ctx, "system.power", {"action": "logout"})
    assert runner.calls == [["loginctl", "terminate-session", "7"]]


def test_power_rejects_unknown_action(ctx):
    with pytest.raises(InvalidParams):
        ctx.call("system.power", {"action": "hibernate"})


def test_power_failure_is_reported(ctx, runner):
    runner.script(["systemctl", "suspend"], returncode=1, stderr="Sleep verb not supported")
    from nexus.core.errors import CommandFailed
    with pytest.raises(CommandFailed):
        ctx.call("system.power", {"action": "suspend"})


# ---- logs & diagnostics ------------------------------------------------------------------------

def test_logs_reads_file_and_user_journal(ctx, runner):
    log = ctx.paths.data / "nexus.log"
    log.write_text("".join(f"line {i}\n" for i in range(500)))
    runner.script(["journalctl"], stdout="2026-11-08T10:00:00+0100 host nexusd[12]: started\n")
    r = ctx.call("system.logs", {"lines": 10})
    assert r["nexus_log"] == [f"line {i}" for i in range(490, 500)]
    assert r["journal"] == ["2026-11-08T10:00:00+0100 host nexusd[12]: started"]
    j = [c for c in runner.calls if c[0] == "journalctl"][0]
    assert "--user" in j and "--no-pager" in j and j[j.index("-n") + 1] == "10"


def test_logs_bounds_and_missing_journal(ctx, runner):
    with pytest.raises(InvalidParams):
        ctx.call("system.logs", {"lines": 0})
    runner.binaries.discard("journalctl")
    r = ctx.call("system.logs")
    assert r["nexus_log"] == [] and r["journal"] is None and r["journal_error"]


def test_diagnostics_aggregates(make_ctx, runner, tmp_path):
    osr = tmp_path / "os-release"
    osr.write_text('NAME="Bazzite"\nVERSION_ID=45\nPRETTY_NAME="Bazzite"\n')
    ctx = make_ctx(modules=["system"])
    sysmod = ctx.module("system")
    sysmod.os_release_path = osr
    sysmod.disk_paths = [tmp_path]
    ctx.registry.load_errors["broken"] = "ImportError: boom"
    runner.script(["bootc", "status"], stdout=json.dumps(BOOTC_STATUS))
    ctx.call("system.status")  # produces an audit entry
    d = ctx.call("system.diagnostics")
    assert d["modules"]["system"]["ok"] is True
    assert d["load_errors"] == {"broken": "ImportError: boom"}
    assert d["versions"]["os"] == "Bazzite" and d["versions"]["os_version"] == "45"
    assert d["versions"]["python"]
    assert d["disk"][0]["total"] > 0
    assert d["last_audit"]["event"] in ("allow", "exec")
    assert d["system"]["backend"] == "bootc"
    json.dumps(d)  # must be serialisable for the API


def test_diagnostics_survives_failing_module_and_no_backend(ctx, runner):
    from nexus.core.module import NexusModule

    class Broken(NexusModule):
        name = "broken"

        def status(self):
            raise RuntimeError("kaput")

    ctx.registry.register(Broken(ctx))
    runner.binaries -= {"bootc", "rpm-ostree"}
    d = ctx.call("system.diagnostics")
    assert d["modules"]["broken"]["ok"] is False
    assert d["modules"]["broken"]["error"] == "RuntimeError: kaput"
    assert d["system"]["error"]["error"] == "unavailable"
