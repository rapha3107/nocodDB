import pytest

from nexus.core.errors import ConfirmationRequired, InvalidParams, NexusError, NotFound
from nexus.security.permissions import Origin


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(modules=["profiles"])


def test_default_profile_exists(ctx):
    r = ctx.call("profiles.list")
    assert len(r["profiles"]) == 1
    assert r["active"] == "default"
    assert r["profiles"][0]["active"] is True
    assert ctx.call("profiles.current")["id"] == "default"


def test_create_switch_delete(ctx):
    p = ctx.call("profiles.create", {"name": "  Léa  ", "avatar": "🦊", "color": "#2EC4B6"})
    assert p["name"] == "Léa" and p["avatar"] == "🦊" and p["color"] == "#2ec4b6"
    assert len(ctx.call("profiles.list")["profiles"]) == 2
    ctx.call("profiles.switch", {"id": p["id"]})
    assert ctx.settings.get("profile.active") == p["id"]
    assert ctx.call("profiles.current")["name"] == "Léa"
    # delete needs confirmation even from the user
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("profiles.delete", {"id": p["id"]})
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    r = ctx.call("profiles.delete", {"id": p["id"]}, confirmation_id=cid)
    assert r["active"] == "default"
    assert ctx.settings.get("profile.active") == "default"


def test_persistence(ctx, make_ctx):
    ctx.call("profiles.create", {"name": "Max"})
    ctx2 = make_ctx(modules=["profiles"])
    assert [p["name"] for p in ctx2.call("profiles.list")["profiles"]] == ["Joueur", "Max"]


def test_cannot_delete_last_profile(ctx):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("profiles.delete", {"id": "default"})
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    with pytest.raises(NexusError) as e2:
        ctx.call("profiles.delete", {"id": "default"}, confirmation_id=cid)
    assert e2.value.code == "last_profile"


@pytest.mark.parametrize("params", [
    {"name": ""}, {"name": "x" * 25}, {"name": "A", "color": "red"}, {"name": "A", "avatar": "x" * 9},
])
def test_create_validation(ctx, params):
    with pytest.raises(InvalidParams):
        ctx.call("profiles.create", params)


def test_duplicate_name(ctx):
    with pytest.raises(InvalidParams):
        ctx.call("profiles.create", {"name": "joueur"})


def test_switch_unknown(ctx):
    with pytest.raises(NotFound):
        ctx.call("profiles.switch", {"id": "nope"})


def test_update(ctx):
    p = ctx.call("profiles.update", {"id": "default", "name": "Sam", "avatar": "👾"})
    assert p["name"] == "Sam" and ctx.call("profiles.current")["avatar"] == "👾"


def test_corrupt_file_recovers(ctx):
    ctx.paths.data.joinpath("profiles.json").write_text("{not json")
    assert ctx.call("profiles.list")["profiles"][0]["id"] == "default"


def test_settings_get_set(ctx):
    s = ctx.call("profiles.settings_get")
    assert s["settings"]["language"] == "fr"
    assert "security.auto_install_verified" in s["settable"]
    ctx.call("profiles.settings_set", {"key": "music.volume", "value": 40})
    ctx.call("profiles.settings_set", {"key": "ai.monthly_budget", "value": 5})
    ctx.call("profiles.settings_set", {"key": "ai.share_library_with_remote", "value": False})
    assert ctx.settings.get("music.volume") == 40
    assert ctx.settings.get("ai.monthly_budget") == 5.0
    assert ctx.settings.get("ai.share_library_with_remote") is False


@pytest.mark.parametrize("key,value", [
    ("music.volume", 101), ("music.volume", True), ("language", "xx"),
    ("security.auto_install_verified", "yes"), ("ai.monthly_budget", -1),
])
def test_settings_set_validation(ctx, key, value):
    with pytest.raises(InvalidParams):
        ctx.call("profiles.settings_set", {"key": key, "value": value})


@pytest.mark.parametrize("key", ["profile.active", "ai.provider", "ai.remote_enabled", "foo"])
def test_settings_set_not_whitelisted(ctx, key):
    with pytest.raises(InvalidParams):
        ctx.call("profiles.settings_set", {"key": key, "value": "x"})


def test_ai_cannot_change_security_without_user(ctx):
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("profiles.settings_set", {"key": "security.auto_install_verified", "value": False},
                 origin=Origin.AI)
    assert ctx.settings.get("security.auto_install_verified") is True
    # the AI cannot approve its own request
    from nexus.core.errors import PermissionDenied
    with pytest.raises(PermissionDenied):
        ctx.permissions.approve(ei.value.details["confirmation_id"], Origin.AI)
    # AI cannot create profiles silently either
    with pytest.raises(ConfirmationRequired):
        ctx.call("profiles.create", {"name": "IA"}, origin=Origin.AI)
