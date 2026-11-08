import json
import threading
import urllib.error
import urllib.request

import pytest

from nexus.api.server import make_server
from nexus.core.errors import ConfirmationRequired, InvalidParams, PermissionDenied
from nexus.core.module import NexusModule, Param, action
from nexus.security.permissions import Decision, Origin, Risk
from nexus.security.runner import validate_argv
from nexus.security.verify import safe_join, validate_flatpak_id


class Demo(NexusModule):
    name = "demo"
    title = "Demo"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.ran = []

    @action("read", Risk.READ, "lecture")
    def read(self):
        return "data"

    @action("wipe", Risk.DELETE_DATA, "supprime", {"what": Param("str")})
    def wipe(self, what):
        self.ran.append(what)
        return f"wiped {what}"

    @action("sideload", Risk.UNKNOWN_SOURCE, "source inconnue")
    def sideload(self):
        return "installed"

    @action("count", Risk.READ, "typed", {"n": Param("int")})
    def count(self, n):
        return n + 1


@pytest.fixture
def ctx(make_ctx):
    c = make_ctx(modules=[])
    c.registry.register(Demo(c))
    return c


@pytest.mark.parametrize("risk,origin,expected", [
    (Risk.READ, Origin.AI, Decision.ALLOW),
    (Risk.CONTROL, Origin.AI, Decision.ALLOW),
    (Risk.INSTALL_VERIFIED, Origin.AI, Decision.ALLOW),
    (Risk.INSTALL_FREE_GAME, Origin.AI, Decision.ALLOW),
    (Risk.PURCHASE, Origin.USER, Decision.CONFIRM),
    (Risk.PURCHASE, Origin.AI, Decision.CONFIRM),
    (Risk.DELETE_DATA, Origin.AI, Decision.CONFIRM),
    (Risk.SYSTEM, Origin.USER, Decision.CONFIRM),
    (Risk.UNKNOWN_SOURCE, Origin.AI, Decision.DENY),
    (Risk.UNKNOWN_SOURCE, Origin.USER, Decision.CONFIRM),
    (Risk.SETTINGS, Origin.AI, Decision.CONFIRM),
])
def test_policy_table(ctx, risk, origin, expected):
    assert ctx.permissions.decide(risk, origin) is expected


def test_auto_install_can_be_disabled(ctx):
    ctx.settings.set("security.auto_install_verified", False)
    assert ctx.permissions.decide(Risk.INSTALL_VERIFIED, Origin.AI) is Decision.CONFIRM
    assert ctx.permissions.decide(Risk.INSTALL_VERIFIED, Origin.USER) is Decision.ALLOW


def test_confirmation_flow_binds_params(ctx):
    demo = ctx.module("demo")
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("demo.wipe", {"what": "saves"}, origin=Origin.AI)
    cid = ei.value.details["confirmation_id"]
    assert demo.ran == []
    # AI cannot approve
    with pytest.raises(PermissionDenied):
        ctx.permissions.approve(cid, Origin.AI)
    ctx.permissions.approve(cid, Origin.USER)
    # approval cannot be reused for different params
    with pytest.raises(ConfirmationRequired):
        ctx.call("demo.wipe", {"what": "everything"}, origin=Origin.AI, confirmation_id=cid)
    assert ctx.call("demo.wipe", {"what": "saves"}, origin=Origin.AI, confirmation_id=cid) == "wiped saves"
    # single use
    with pytest.raises(ConfirmationRequired):
        ctx.call("demo.wipe", {"what": "saves"}, origin=Origin.AI, confirmation_id=cid)
    assert demo.ran == ["saves"]


def test_unapproved_confirmation_id_does_not_run(ctx):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("demo.wipe", {"what": "x"})
    cid = ei.value.details["confirmation_id"]
    with pytest.raises(ConfirmationRequired):
        ctx.call("demo.wipe", {"what": "x"}, confirmation_id=cid)
    assert ctx.module("demo").ran == []


def test_ai_denied_unknown_source(ctx):
    with pytest.raises(PermissionDenied):
        ctx.call("demo.sideload", origin=Origin.AI)
    assert any(e["event"] == "deny" for e in ctx.audit.memory)


def test_param_validation(ctx):
    with pytest.raises(InvalidParams):
        ctx.call("demo.count", {"n": "1"})
    with pytest.raises(InvalidParams):
        ctx.call("demo.count", {"n": True})
    with pytest.raises(InvalidParams):
        ctx.call("demo.count", {"n": 1, "evil": 2})
    assert ctx.call("demo.count", {"n": 1}) == 2


@pytest.mark.parametrize("argv", [
    "flatpak install x",            # string, not list
    ["bash", "-c", "rm -rf /"],
    ["/usr/bin/flatpak", "list"],   # absolute path bypass
    ["sudo", "rpm-ostree", "upgrade"],
    ["flatpak", "remote-add", "evil", "https://evil"],
    ["flatpak", "install", "--from=https://evil/app.flatpakref"],
    ["flatpak", "override", "--filesystem=host", "org.x.Y"],
    ["rpm-ostree", "install", "evilpkg"],
    ["systemctl", "disable", "firewalld"],
    ["systemctl", "start", "sshd"],
    ["xdg-open", "file:///etc/shadow"],
    ["flatpak", "list\x00"],
    ["flatpak", "run", "--command=sh", "org.libretro.RetroArch"],
    ["flatpak", "run", "--filesystem=host", "org.libretro.RetroArch"],
    ["journalctl", "--vacuum-time=1s", "--user"],
])
def test_runner_rejects_dangerous(argv):
    with pytest.raises(PermissionDenied):
        validate_argv(argv)


@pytest.mark.parametrize("argv", [
    ["flatpak", "install", "-y", "--noninteractive", "flathub", "net.rpcs3.RPCS3"],
    ["rpm-ostree", "upgrade"],
    ["systemctl", "--user", "restart", "nexusd.service"],
    ["xdg-open", "steam://rungameid/570"],
    ["flatpak", "run", "org.libretro.RetroArch", "-L", "core.so", "/home/u/roms/a.sfc"],
])
def test_runner_allows_safe(argv):
    validate_argv(argv)


def test_safe_join(tmp_path):
    (tmp_path / "a").mkdir()
    assert safe_join(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    with pytest.raises(PermissionDenied):
        safe_join(tmp_path, "../etc/passwd")
    with pytest.raises(PermissionDenied):
        safe_join(tmp_path, "/etc/passwd")


def test_flatpak_id_validation():
    validate_flatpak_id("net.rpcs3.RPCS3")
    for bad in ["rpcs3", "net.rpcs3.RPCS3; rm -rf /", "--from=x.y.z", "a..b"]:
        with pytest.raises(InvalidParams):
            validate_flatpak_id(bad)


# ---------------- HTTP API ----------------

@pytest.fixture
def server(ctx, tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<meta name="nexus-token" content="__NEXUS_TOKEN__">')
    srv = make_server(ctx, port=0, token="T0KEN", ui_dir=ui)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.stopping.set()
    srv.shutdown()


def _http(srv, method, path, body=None, headers=None):
    port = srv.server_address[1]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_index_injects_token(server):
    st, body = _http(server, "GET", "/")
    assert st == 200 and b"T0KEN" in body


def test_post_requires_token(server):
    st, _ = _http(server, "POST", "/api/call", {"action": "demo.read"})
    assert st == 403
    st, body = _http(server, "POST", "/api/call", {"action": "demo.read"}, {"X-Nexus-Token": "T0KEN"})
    assert st == 200 and json.loads(body)["result"] == "data"


def test_cross_origin_rejected(server):
    st, _ = _http(server, "POST", "/api/call", {"action": "demo.read"},
                  {"X-Nexus-Token": "T0KEN", "Origin": "https://evil.example"})
    assert st == 403


def test_dns_rebinding_host_rejected(server):
    st, _ = _http(server, "GET", "/api/status", headers={"Host": "evil.example", "X-Nexus-Token": "T0KEN"})
    assert st == 421


def test_read_api_requires_token(server):
    assert _http(server, "GET", "/api/actions")[0] == 403
    assert _http(server, "GET", "/api/actions", headers={"X-Nexus-Token": "T0KEN"})[0] == 200


def test_static_traversal_blocked(server):
    assert _http(server, "GET", "/../../etc/passwd")[0] == 404
    assert _http(server, "GET", "/%2e%2e/%2e%2e/etc/passwd")[0] == 404


def test_http_confirmation_roundtrip(server, ctx):
    h = {"X-Nexus-Token": "T0KEN"}
    st, body = _http(server, "POST", "/api/call", {"action": "demo.wipe", "params": {"what": "cache"}}, h)
    assert st == 409
    cid = json.loads(body)["details"]["confirmation_id"]
    st, body = _http(server, "POST", f"/api/confirmations/{cid}/approve", {}, h)
    assert st == 200 and json.loads(body)["result"] == "wiped cache"


def test_tasks_report_progress(ctx):
    def job(t):
        t.update(0.5, "moitié")
        return 42
    tid = ctx.tasks.submit("Test", job)["task_id"]
    t = ctx.tasks.wait(tid)
    assert t.state == "done" and t.result == 42
    assert any(e["topic"] == "task.update" and e["data"]["progress"] == 0.5 for e in ctx.events.recent("task"))


def test_param_validator_runs_before_permission(make_ctx):
    from nexus.security.verify import validate_flatpak_id

    class V(NexusModule):
        name = "v"

        @action("rm", Risk.DELETE_DATA, "x", {"app_id": Param("str", validator=validate_flatpak_id)})
        def rm(self, app_id):
            return app_id

    c = make_ctx(modules=[])
    c.registry.register(V(c))
    with pytest.raises(InvalidParams):
        c.call("v.rm", {"app_id": "bad; id"})
    assert c.permissions.pending() == []
    with pytest.raises(ConfirmationRequired):
        c.call("v.rm", {"app_id": "org.x.Good"})
