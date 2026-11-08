import json
import signal

import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, NotFound, PermissionDenied, Unavailable
from nexus.modules.browser import BrowserModule
from nexus.modules.browser import urls
from nexus.security.permissions import Origin

FIREFOX_LIST = "org.mozilla.firefox\tFirefox\t140.0\tflathub\n"


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(modules=["browser"])


@pytest.fixture
def firefox(runner):
    runner.script(["flatpak", "list"], stdout=FIREFOX_LIST)
    return runner


def confirmed(ctx, action, params):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call(action, params)
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    return ctx.call(action, params, confirmation_id=cid)


# ------------------------------------------------------------------ URL rules
@pytest.mark.parametrize("text,expected", [
    ("youtube.com", "https://youtube.com"),
    ("  www.YouTube.com/watch?v=abc ", "https://www.youtube.com/watch?v=abc"),
    ("https://fr.wikipedia.org/wiki/Linux", "https://fr.wikipedia.org/wiki/Linux"),
    ("http://example.org", "http://example.org"),
    ("example.com:8443/x", "https://example.com:8443/x"),
    ("8.8.8.8", "https://8.8.8.8"),
])
def test_normalize_url(text, expected):
    assert urls.resolve_input(text) == (expected, "url")


@pytest.mark.parametrize("text", ["comment jouer à elden ring", "linux", "c++: guide", "café"])
def test_non_url_becomes_search(text):
    url, kind = urls.resolve_input(text)
    assert kind == "search"
    assert url.startswith("https://duckduckgo.com/?q=")
    assert " " not in url


def test_search_is_encoded_and_engine_configurable():
    url, _ = urls.resolve_input("a&b=c d", engine="https://www.qwant.com/?q=")
    assert url == "https://www.qwant.com/?q=a%26b%3Dc+d"
    with pytest.raises(InvalidParams):
        urls.resolve_input("hello world", engine="http://evil/?q=")


@pytest.mark.parametrize("text", [
    "file:///etc/passwd", "javascript:alert(1)", "JavaScript:alert(1)", "javascript: alert(1)",
    "data:text/html,<script>x</script>", "chrome://settings", "about:config", "about:blank",
    "view-source:https://example.com", "ftp://example.com", "blob:https://x.com/1",
])
def test_dangerous_schemes_refused(text):
    with pytest.raises(PermissionDenied):
        urls.resolve_input(text)


@pytest.mark.parametrize("text", [
    "http://127.0.0.1", "127.0.0.1:7777", "http://127.0.0.1:7777/api", "http://localhost",
    "localhost:7777", "http://foo.localhost", "http://10.0.0.1", "10.1.2.3", "http://192.168.1.1",
    "http://172.16.0.1", "http://169.254.169.254", "http://[::1]", "[::1]:7777", "http://[fe80::1]",
    "http://[::ffff:127.0.0.1]", "http://0x7f000001", "http://2130706433", "http://0177.0.0.1",
    "http://127.1", "http://0.0.0.0", "http://100.64.0.1", "http://nas.local", "http://router",
    "https://127.0.0.1.", "http://[fd00::1]",
])
def test_local_and_private_hosts_refused(text):
    with pytest.raises(PermissionDenied):
        urls.resolve_input(text)


def test_credentials_in_url_refused():
    with pytest.raises(PermissionDenied):
        urls.resolve_input("https://youtube.com@127.0.0.1/")
    with pytest.raises(PermissionDenied):
        urls.resolve_input("https://user:pw@example.com/")


def test_whitelist_allows_local_host():
    assert urls.resolve_input("http://192.168.1.10:8096", allowed_local=["192.168.1.10"])[0] \
        == "http://192.168.1.10:8096"
    # a whitelisted IP does not open the rest of the LAN
    with pytest.raises(PermissionDenied):
        urls.resolve_input("http://192.168.1.11", allowed_local=["192.168.1.10"])


# ------------------------------------------------------------------ open / close
def test_open_spawns_exact_argv(ctx, firefox):
    r = ctx.call("browser.open", {"url": "youtube.com"})
    assert firefox.spawned == [["flatpak", "run", "org.mozilla.firefox", "--kiosk", "https://youtube.com"]]
    assert r["pid"] == 4242 and r["kind"] == "url"
    assert ctx.call("browser.history")[0]["url"] == "https://youtube.com"


def test_open_search(ctx, firefox):
    r = ctx.call("browser.open", {"url": "nexus os bazzite"})
    assert r["kind"] == "search"
    assert firefox.spawned[0][-1] == "https://duckduckgo.com/?q=nexus+os+bazzite"


def test_open_custom_search_engine(ctx, firefox):
    ctx.settings.set("browser.search_engine", "https://www.startpage.com/do/search?q=")
    ctx.call("browser.open", {"url": "proton"})
    assert firefox.spawned[0][-1] == "https://www.startpage.com/do/search?q=proton"


def test_open_refuses_local_without_spawning(ctx, firefox):
    for u in ("http://127.0.0.1:7777", "javascript:alert(1)", "file:///etc/passwd", "http://0x7f000001"):
        with pytest.raises(PermissionDenied):
            ctx.call("browser.open", {"url": u})
    assert firefox.spawned == []


def test_open_whitelist_setting(ctx, firefox):
    ctx.settings.set("browser.allowed_local_hosts", ["192.168.1.10"])
    ctx.call("browser.open", {"url": "http://192.168.1.10:8096"})
    assert firefox.spawned[0][-1] == "http://192.168.1.10:8096"


def test_open_chromium_fallback(ctx, runner):
    runner.script(["flatpak", "list"], stdout="org.chromium.Chromium\tChromium\t1\tflathub\n")
    ctx.call("browser.open", {"url": "twitch.tv"})
    assert runner.spawned == [["flatpak", "run", "org.chromium.Chromium", "--kiosk", "https://twitch.tv"]]


def test_open_without_browser_unavailable(ctx, runner):
    with pytest.raises(Unavailable):
        ctx.call("browser.open", {"url": "youtube.com"})
    st = ctx.call("browser.status")
    assert st["ok"] is False and st["browser"] is None and st["open"] is False


def test_status_detects_firefox(ctx, firefox):
    st = ctx.call("browser.status")
    assert st["ok"] and st["browser"] == "org.mozilla.firefox" and st["open"] is False


@pytest.fixture
def fake_proc(make_ctx, firefox, tmp_path):
    ctx = make_ctx(modules=[])
    proc = tmp_path / "proc"
    killed = []
    mod = BrowserModule(ctx, proc_root=proc, kill=lambda pid, sig: killed.append((pid, sig)))
    ctx.registry.register(mod)
    return ctx, proc, killed


def _write_cmdline(proc, pid, args):
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in args) + b"\x00")


def test_close_without_pid_kills_nothing(fake_proc):
    ctx, proc, killed = fake_proc
    r = ctx.call("browser.close")
    assert r["closed"] is False and killed == []


def test_close_kills_only_verified_browser_pid(fake_proc):
    ctx, proc, killed = fake_proc
    ctx.call("browser.open", {"url": "youtube.com"})
    _write_cmdline(proc, 4242, ["flatpak", "run", "org.mozilla.firefox", "--kiosk", "https://youtube.com"])
    assert ctx.call("browser.status")["open"] is True
    r = ctx.call("browser.close")
    assert r["closed"] is True and killed == [(4242, signal.SIGTERM)]
    # pid forgotten afterwards
    assert ctx.call("browser.close")["closed"] is False and len(killed) == 1


def test_close_accepts_bwrap_exec(fake_proc):
    ctx, proc, killed = fake_proc
    ctx.call("browser.open", {"url": "youtube.com"})
    _write_cmdline(proc, 4242, ["bwrap", "--args", "40", "--", "/app/bin/firefox", "--kiosk", "x"])
    assert ctx.call("browser.close")["closed"] is True


def test_close_refuses_recycled_pid(fake_proc):
    ctx, proc, killed = fake_proc
    ctx.call("browser.open", {"url": "youtube.com"})
    _write_cmdline(proc, 4242, ["/usr/bin/bash", "-l"])  # pid reused by something else
    r = ctx.call("browser.close")
    assert r["closed"] is False and killed == []


def test_close_dead_pid(fake_proc):
    ctx, proc, killed = fake_proc
    ctx.call("browser.open", {"url": "youtube.com"})  # no /proc entry: process gone
    assert ctx.call("browser.close")["closed"] is False and killed == []


# ------------------------------------------------------------------ bookmarks
def test_default_bookmarks_and_persistence(ctx, make_ctx):
    bms = ctx.call("browser.bookmarks")
    assert len(bms) >= 3 and all(b["url"].startswith("https://") for b in bms)
    bm = ctx.call("browser.bookmark_add", {"title": " Proton  DB ", "url": "protondb.com/app/1"})
    assert bm["url"] == "https://protondb.com/app/1" and bm["title"] == "Proton DB"
    path = ctx.paths.data / "browser" / "bookmarks.json"
    assert any(b["id"] == bm["id"] for b in json.loads(path.read_text()))
    ctx2 = make_ctx(modules=["browser"])
    assert any(b["id"] == bm["id"] for b in ctx2.call("browser.bookmarks"))
    ctx.call("browser.bookmark_remove", {"id": bm["id"]})
    assert not any(b["id"] == bm["id"] for b in ctx.call("browser.bookmarks"))
    with pytest.raises(NotFound):
        ctx.call("browser.bookmark_remove", {"id": bm["id"]})


def test_bookmark_add_validation(ctx):
    for bad in ("javascript:alert(1)", "http://127.0.0.1", "pas une url"):
        with pytest.raises((PermissionDenied, InvalidParams)):
            ctx.call("browser.bookmark_add", {"title": "x", "url": bad})
    with pytest.raises(InvalidParams):
        ctx.call("browser.bookmark_add", {"title": "  ", "url": "example.com"})
    a = ctx.call("browser.bookmark_add", {"title": "E", "url": "example.com"})
    b = ctx.call("browser.bookmark_add", {"title": "E2", "url": "https://example.com"})
    assert b["id"] == a["id"] and b["existing"]


def test_bookmark_actions_need_confirmation_from_ai(ctx):
    with pytest.raises(ConfirmationRequired):
        ctx.call("browser.bookmark_add", {"title": "x", "url": "example.com"}, origin=Origin.AI)


# ------------------------------------------------------------------ history / suggest
def test_history_limit_dedupe_and_clear(ctx, firefox):
    for i in range(105):
        ctx.call("browser.open", {"url": f"example{i}.com"})
    ctx.call("browser.open", {"url": "example50.com"})
    hist = ctx.call("browser.history")
    assert len(hist) == 100 and hist[0]["url"] == "https://example50.com"
    assert len({h["url"] for h in hist}) == 100
    r = confirmed(ctx, "browser.history_clear", {})
    assert r["cleared"] == 100 and ctx.call("browser.history") == []


def test_suggest(ctx, firefox):
    ctx.call("browser.open", {"url": "youtube.com/feed/subscriptions"})
    ctx.call("browser.open", {"url": "steam deck"})
    s = ctx.call("browser.suggest", {"text": "you"})
    urls_ = [x["url"] for x in s]
    assert urls_[0] == "https://www.youtube.com"  # bookmark first
    assert "https://youtube.com/feed/subscriptions" in urls_
    s = ctx.call("browser.suggest", {"text": "steam"})
    assert {x["source"] for x in s} == {"bookmark", "history"}
    assert len(ctx.call("browser.suggest", {"text": "", "limit": 2})) == 2
    assert ctx.call("browser.suggest", {"text": "zzzzzz"}) == []


# ------------------------------------------------------------------ services
def test_services_catalog():
    from nexus.modules.browser.services import SERVICES
    ids = {s["id"] for s in SERVICES}
    assert {"youtube", "twitch", "netflix", "disneyplus", "primevideo", "crunchyroll", "spotify",
            "youtubemusic"} <= ids
    for s in SERVICES:
        assert urls.check_url(s["url"]) == s["url"]
        assert s["compatibility"] in {"stable", "bon", "expérimental", "limité", "non supporté"}
        if s["drm"]:
            assert "DRM" in s["note"]


def test_services_action(ctx):
    r = ctx.call("browser.services")
    assert r["services"] and "Steam Input" in r["controller_note"]
