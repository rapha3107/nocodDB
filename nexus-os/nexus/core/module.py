"""Module contract and registry — the internal API every NEXUS module implements.

A module is a subclass of `NexusModule` living in `nexus/modules/<name>/`, whose
package exposes `MODULE = <class>`. It declares actions with `@action(...)`.
Every call (UI or AI) goes through `Registry.call`, which enforces permissions,
validates parameters, audits and converts errors. Modules call each other only
through `self.ctx.call(...)`, never by importing each other.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..security.permissions import Origin, Risk
from .errors import InvalidParams, NexusError, NotFound

log = logging.getLogger("nexus")

_TYPES = {"any": object, "str": str, "int": int, "float": (int, float), "bool": bool, "list": list, "dict": dict}


@dataclass
class Param:
    type: str = "str"           # any | str | int | float | bool | list | dict
    required: bool = True
    description: str = ""
    default: Any = None
    choices: list | None = None
    # Optional validator run BEFORE the permission check, so malformed input is
    # rejected without ever prompting the user. Raise InvalidParams/NexusError.
    validator: Callable[[Any], Any] | None = None


@dataclass
class ActionSpec:
    name: str                    # local name, e.g. "install"
    risk: Risk
    description: str             # FR, also shown to the AI as tool description
    params: dict[str, Param] = field(default_factory=dict)
    summary: Callable[[dict], str] | None = None  # human text for confirmation dialogs
    fn: Callable | None = None


def action(name: str, risk: Risk, description: str, params: dict[str, Param] | None = None,
           summary: Callable[[dict], str] | None = None):
    def deco(fn):
        fn.__nexus_action__ = ActionSpec(name, risk, description, params or {}, summary)
        return fn
    return deco


class NexusModule:
    name: str = ""
    title: str = ""
    description: str = ""

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.log = logging.getLogger(f"nexus.{self.name}")

    def start(self) -> None:
        """Called once after all modules are loaded (background scans, etc.)."""

    def stop(self) -> None:
        pass

    def status(self) -> dict:
        """Health/availability summary shown on the diagnostics page."""
        return {"ok": True}

    def specs(self) -> list[ActionSpec]:
        out = []
        for _, m in inspect.getmembers(self, predicate=inspect.ismethod):
            spec = getattr(m, "__nexus_action__", None)
            if spec:
                out.append(ActionSpec(spec.name, spec.risk, spec.description, spec.params,
                                      spec.summary, m))
        return out


def validate_params(spec: ActionSpec, params: dict) -> dict:
    if not isinstance(params, dict):
        raise InvalidParams("Les paramètres doivent être un objet")
    unknown = set(params) - set(spec.params)
    if unknown:
        raise InvalidParams(f"Paramètres inconnus: {', '.join(sorted(unknown))}")
    clean = {}
    for k, p in spec.params.items():
        if k not in params or params[k] is None:
            if p.required:
                raise InvalidParams(f"Paramètre requis manquant: {k}")
            if p.default is not None:
                clean[k] = p.default
            continue
        v = params[k]
        t = _TYPES[p.type]
        if p.type == "int" and isinstance(v, bool) or not isinstance(v, t):
            raise InvalidParams(f"Type invalide pour {k}: attendu {p.type}")
        if p.type == "str" and len(v) > 4096:
            raise InvalidParams(f"Valeur trop longue pour {k}")
        if p.choices is not None and v not in p.choices:
            raise InvalidParams(f"Valeur non autorisée pour {k}: {v}")
        if p.validator is not None:
            try:
                p.validator(v)
            except NexusError:
                raise
            except Exception as e:
                raise InvalidParams(f"Valeur invalide pour {k}") from e
        clean[k] = v
    return clean


class Registry:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.modules: dict[str, NexusModule] = {}
        self.actions: dict[str, ActionSpec] = {}
        self.load_errors: dict[str, str] = {}

    def register(self, module: NexusModule) -> None:
        if not module.name:
            raise ValueError("module without name")
        self.modules[module.name] = module
        for spec in module.specs():
            self.actions[f"{module.name}.{spec.name}"] = spec

    def discover(self, only: list[str] | None = None) -> None:
        import nexus.modules as pkg
        for info in pkgutil.iter_modules(pkg.__path__):
            if only is not None and info.name not in only:
                continue
            try:
                mod = importlib.import_module(f"nexus.modules.{info.name}")
                cls = getattr(mod, "MODULE", None)
                if cls is None:
                    continue
                self.register(cls(self.ctx))
            except Exception as e:  # one broken module must not take the OS down
                log.exception("module %s failed to load", info.name)
                self.load_errors[info.name] = f"{type(e).__name__}: {e}"

    def start_all(self) -> None:
        for name, m in self.modules.items():
            try:
                m.start()
            except Exception as e:
                log.exception("module %s failed to start", name)
                self.load_errors[name] = f"start: {type(e).__name__}: {e}"

    def stop_all(self) -> None:
        for m in self.modules.values():
            try:
                m.stop()
            except Exception:
                log.exception("module %s failed to stop", m.name)

    def describe(self) -> list[dict]:
        return [
            {"action": k, "risk": s.risk.value, "description": s.description,
             "params": {n: {"type": p.type, "required": p.required, "description": p.description,
                            **({"choices": p.choices} if p.choices else {})}
                        for n, p in s.params.items()}}
            for k, s in sorted(self.actions.items())
        ]

    def call(self, action_name: str, params: dict | None = None, *,
             origin: Origin = Origin.USER, confirmation_id: str | None = None) -> Any:
        spec = self.actions.get(action_name)
        if not spec:
            raise NotFound(f"Action inconnue: {action_name}")
        clean = validate_params(spec, params or {})
        summary = spec.summary(clean) if spec.summary else f"{action_name} ({', '.join(f'{k}={v}' for k, v in clean.items())})"
        self.ctx.permissions.check(action_name, clean, spec.risk, origin, summary, confirmation_id)
        try:
            return spec.fn(**clean)
        except NexusError:
            raise
        except Exception as e:
            log.error("action %s crashed: %s", action_name, traceback.format_exc())
            raise NexusError(f"Erreur interne dans {action_name}: {type(e).__name__}",
                             code="internal_error") from e


@dataclass
class Context:
    paths: Any
    settings: Any
    events: Any
    audit: Any
    permissions: Any
    runner: Any
    net: Any
    tasks: Any = None
    registry: Registry | None = None

    def call(self, action_name: str, params: dict | None = None, *,
             origin: Origin = Origin.USER, confirmation_id: str | None = None) -> Any:
        return self.registry.call(action_name, params, origin=origin, confirmation_id=confirmation_id)

    def module(self, name: str) -> NexusModule | None:
        return self.registry.modules.get(name) if self.registry else None
