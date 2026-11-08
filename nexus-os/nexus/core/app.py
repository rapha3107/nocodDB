"""Builds the NEXUS application context (used by nexusd, nexusctl and tests)."""

from __future__ import annotations

import logging
import logging.handlers

from ..security.audit import AuditLog
from ..security.permissions import PermissionManager
from ..security.runner import Runner
from .config import Paths, Settings
from .events import EventBus
from .module import Context, Registry
from .net import Net
from .tasks import TaskManager


def setup_logging(paths: Paths, level=logging.INFO) -> None:
    paths.ensure()
    root = logging.getLogger("nexus")
    if root.handlers:
        return
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(paths.data / "nexus.log", maxBytes=5_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def build_context(*, paths: Paths | None = None, runner=None, net=None, modules: list[str] | None = None,
                  start: bool = True) -> Context:
    paths = paths or Paths.detect()
    paths.ensure()
    audit = AuditLog(paths.data / "audit.jsonl")
    settings = Settings(paths.config / "settings.json")
    events = EventBus()
    ctx = Context(
        paths=paths, settings=settings, events=events, audit=audit,
        permissions=PermissionManager(settings, audit, events),
        runner=runner or Runner(audit), net=net or Net(audit),
        tasks=TaskManager(events),
    )
    ctx.registry = Registry(ctx)
    ctx.registry.discover(only=modules)
    if start:
        ctx.registry.start_all()
    return ctx
