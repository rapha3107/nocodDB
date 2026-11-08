"""Shared fixtures. Every test runs in an isolated NEXUS_ROOT with fake runner/net."""

import pytest

from nexus.core.app import build_context
from nexus.core.config import Paths
from nexus.core.net import FakeNet
from nexus.security.runner import FakeRunner


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_ROOT", str(tmp_path / "nexus"))
    p = Paths.detect()
    p.ensure()
    p.home.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def net():
    return FakeNet()


@pytest.fixture
def make_ctx(paths, runner, net):
    """make_ctx(["music"]) -> Context with only those modules loaded (None = all)."""
    created = []

    def _make(modules=None, start=True):
        ctx = build_context(paths=paths, runner=runner, net=net, modules=modules, start=start)
        created.append(ctx)
        return ctx

    yield _make
    for c in created:
        c.registry.stop_all()
        c.tasks.shutdown()
