"""Deterministic permission manager.

Every action exposed by a module declares a `Risk`. The decision table below is
plain code — no LLM is ever consulted. NEXUS AI calls actions with
origin=Origin.AI and can NEVER approve a confirmation: approvals only arrive
through the UI confirmation endpoint (origin=Origin.USER), protected by the
session token.
"""

from __future__ import annotations

import enum
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ConfirmationRequired, PermissionDenied


class Risk(str, enum.Enum):
    READ = "read"                          # query state, no side effect
    CONTROL = "control"                    # play/pause, launch a game, volume
    SETTINGS = "settings"                  # change NEXUS preferences
    INSTALL_VERIFIED = "install_verified"  # verified FOSS from an allowlisted remote
    INSTALL_FREE_GAME = "install_free_game"  # free, legal game from authorized source
    INSTALL_OWNED = "install_owned"        # owned game via its official client
    PURCHASE = "purchase"                  # spending money: always confirm
    UNKNOWN_SOURCE = "unknown_source"      # unverified origin: blocked for AI
    DELETE_DATA = "delete_data"            # uninstall / delete files: confirm
    SYSTEM = "system"                      # OS update, rollback, power, drivers


class Origin(str, enum.Enum):
    USER = "user"   # direct, explicit action in the UI by the human
    AI = "ai"       # NEXUS AI acting on a natural-language request


class Decision(str, enum.Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class PendingConfirmation:
    id: str
    action: str
    params: dict
    risk: Risk
    origin: Origin
    summary: str
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "params": self.params,
            "risk": self.risk.value,
            "origin": self.origin.value,
            "summary": self.summary,
            "created": self.created,
        }


class PermissionManager:
    CONFIRM_TTL = 300  # seconds

    def __init__(self, settings, audit, events=None):
        self.settings = settings
        self.audit = audit
        self.events = events
        self._pending: dict[str, PendingConfirmation] = {}
        self._approved: set[str] = set()
        self._lock = threading.Lock()

    # ---- pure policy -------------------------------------------------------
    def decide(self, risk: Risk, origin: Origin) -> Decision:
        if risk in (Risk.READ, Risk.CONTROL):
            return Decision.ALLOW
        if risk is Risk.SETTINGS:
            return Decision.ALLOW if origin is Origin.USER else Decision.CONFIRM
        if risk is Risk.INSTALL_VERIFIED:
            if origin is Origin.USER or self.settings.get("security.auto_install_verified"):
                return Decision.ALLOW
            return Decision.CONFIRM
        if risk is Risk.INSTALL_FREE_GAME:
            if origin is Origin.USER or self.settings.get("security.auto_install_free_games"):
                return Decision.ALLOW
            return Decision.CONFIRM
        if risk is Risk.INSTALL_OWNED:
            return Decision.ALLOW
        if risk is Risk.UNKNOWN_SOURCE:
            # The AI may never install from an unknown source, even with consent.
            return Decision.DENY if origin is Origin.AI else Decision.CONFIRM
        # PURCHASE, DELETE_DATA, SYSTEM: always an explicit human confirmation.
        return Decision.CONFIRM

    # ---- enforcement -------------------------------------------------------
    def check(self, action: str, params: dict, risk: Risk, origin: Origin,
              summary: str = "", confirmation_id: str | None = None) -> None:
        """Raise unless the action may run now. Consumes a matching approval."""
        decision = self.decide(risk, origin)
        if decision is Decision.DENY:
            self.audit.log("deny", action=action, params=params, origin=origin.value, risk=risk.value)
            raise PermissionDenied(
                "Action bloquée par la politique de sécurité.",
                details={"action": action, "risk": risk.value},
            )
        if decision is Decision.ALLOW:
            self.audit.log("allow", action=action, params=params, origin=origin.value, risk=risk.value)
            return
        # CONFIRM
        if confirmation_id and self._consume(confirmation_id, action, params):
            self.audit.log("allow_confirmed", action=action, params=params,
                           origin=origin.value, risk=risk.value, confirmation=confirmation_id)
            return
        pending = self._create(action, params, risk, origin, summary or action)
        raise ConfirmationRequired(
            "Confirmation requise.",
            details={"confirmation_id": pending.id, "summary": pending.summary,
                     "risk": risk.value, "action": action},
        )

    def _create(self, action, params, risk, origin, summary) -> PendingConfirmation:
        p = PendingConfirmation(secrets.token_urlsafe(16), action, dict(params), risk, origin, summary)
        with self._lock:
            self._gc()
            self._pending[p.id] = p
        self.audit.log("confirm_requested", action=action, params=params, origin=origin.value,
                       risk=risk.value, confirmation=p.id)
        if self.events:
            self.events.publish("confirmation.requested", p.to_dict())
        return p

    def approve(self, confirmation_id: str, origin: Origin) -> PendingConfirmation:
        """Called ONLY by the UI endpoint. AI origin is rejected structurally."""
        if origin is not Origin.USER:
            raise PermissionDenied("Seul l'utilisateur peut confirmer une action.")
        with self._lock:
            self._gc()
            p = self._pending.get(confirmation_id)
            if not p:
                raise PermissionDenied("Confirmation inconnue ou expirée.")
            self._approved.add(confirmation_id)
        self.audit.log("confirm_approved", action=p.action, confirmation=confirmation_id)
        return p

    def reject(self, confirmation_id: str) -> None:
        with self._lock:
            p = self._pending.pop(confirmation_id, None)
            self._approved.discard(confirmation_id)
        if p:
            self.audit.log("confirm_rejected", action=p.action, confirmation=confirmation_id)

    def pending(self) -> list[dict]:
        with self._lock:
            self._gc()
            return [p.to_dict() for k, p in self._pending.items() if k not in self._approved]

    def _consume(self, cid: str, action: str, params: dict) -> bool:
        with self._lock:
            p = self._pending.get(cid)
            if not p or cid not in self._approved:
                return False
            # The approval is bound to the exact action and parameters.
            if p.action != action or p.params != dict(params):
                return False
            del self._pending[cid]
            self._approved.discard(cid)
            return True

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, p in self._pending.items() if now - p.created > self.CONFIRM_TTL]:
            self._pending.pop(k, None)
            self._approved.discard(k)


def summarize(params: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())
