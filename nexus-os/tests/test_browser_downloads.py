import os

import pytest

from nexus.core.errors import ConfirmationRequired, NotFound, PermissionDenied
from nexus.security.permissions import Origin


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(modules=["browser"])


@pytest.fixture
def dl(ctx):
    d = ctx.paths.home / "Downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete(ctx, name):
    params = {"name": name}
    with pytest.raises(ConfirmationRequired) as ei:
        ctx.call("browser.download_delete", params)
    cid = ei.value.details["confirmation_id"]
    ctx.permissions.approve(cid, Origin.USER)
    return ctx.call("browser.download_delete", params, confirmation_id=cid)


def test_downloads_listing(ctx, dl):
    (dl / "video.mp4").write_bytes(b"x" * 10)
    (dl / "doc.pdf").write_bytes(b"y" * 3)
    (dl / "big.iso.part").write_bytes(b"")
    (dl / "sub").mkdir()
    os.utime(dl / "doc.pdf", (1, 1))
    r = ctx.call("browser.downloads")
    assert r["exists"] and r["dir"] == str(dl)
    by = {f["name"]: f for f in r["files"]}
    assert by["video.mp4"]["size"] == 10 and by["video.mp4"]["type"] == "vidéo"
    assert by["doc.pdf"]["type"] == "document"
    assert by["big.iso.part"]["partial"] is True
    assert by["sub"]["is_dir"] is True
    assert r["files"][-1]["name"] == "doc.pdf"  # oldest last
    assert "modified" in by["doc.pdf"]


def test_downloads_missing_dir(ctx):
    r = ctx.call("browser.downloads")
    assert r == {"dir": str(ctx.paths.home / "Downloads"), "files": [], "exists": False}


def test_xdg_user_dirs(ctx):
    cfg = ctx.paths.home / ".config"
    cfg.mkdir(parents=True)
    (cfg / "user-dirs.dirs").write_text('# comment\nXDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n')
    d = ctx.paths.home / "Téléchargements"
    d.mkdir()
    (d / "a.zip").write_bytes(b"z")
    r = ctx.call("browser.downloads")
    assert r["dir"] == str(d) and r["files"][0]["type"] == "archive"


def test_download_delete(ctx, dl):
    (dl / "a.txt").write_text("hi")
    assert delete(ctx, "a.txt")["deleted"] == "a.txt"
    assert not (dl / "a.txt").exists()
    with pytest.raises(NotFound):
        delete(ctx, "a.txt")


@pytest.mark.parametrize("name", ["../secret.txt", "../../etc/passwd", "/etc/passwd", "..", ".",
                                  "sub/../../secret.txt", "a\\..\\b"])
def test_download_delete_traversal_refused(ctx, dl, name):
    secret = dl.parent / "secret.txt"
    secret.write_text("keep")
    with pytest.raises(PermissionDenied):
        delete(ctx, name)
    assert secret.exists()


def test_download_delete_symlink_and_dir_refused(ctx, dl):
    outside = dl.parent / "outside.txt"
    outside.write_text("keep")
    (dl / "link").symlink_to(outside)
    (dl / "folder").mkdir()
    with pytest.raises(PermissionDenied):
        delete(ctx, "link")
    with pytest.raises(PermissionDenied):
        delete(ctx, "folder")
    assert outside.exists() and (dl / "folder").is_dir()


def test_download_delete_requires_confirmation_even_for_ai(ctx, dl):
    (dl / "a.txt").write_text("hi")
    with pytest.raises(ConfirmationRequired):
        ctx.call("browser.download_delete", {"name": "a.txt"}, origin=Origin.AI)
    assert (dl / "a.txt").exists()
