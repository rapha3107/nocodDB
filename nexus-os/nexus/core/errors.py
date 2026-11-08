"""Error types shared by every NEXUS module.

Errors carry a stable machine `code` (used by the UI and NEXUS AI to explain
failures) and a human message (French first, the UI translates codes too).
"""


class NexusError(Exception):
    code = "error"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFound(NexusError):
    code = "not_found"
    http_status = 404


class InvalidParams(NexusError):
    code = "invalid_params"
    http_status = 400


class PermissionDenied(NexusError):
    code = "permission_denied"
    http_status = 403


class ConfirmationRequired(NexusError):
    """Raised when an action needs an explicit user confirmation.

    `details["confirmation_id"]` identifies the pending request; only the UI
    confirmation endpoint (never NEXUS AI) can approve it.
    """

    code = "confirmation_required"
    http_status = 409


class CommandFailed(NexusError):
    code = "command_failed"
    http_status = 502


class Unavailable(NexusError):
    """A dependency (binary, service, hardware) is missing on this machine."""

    code = "unavailable"
    http_status = 503
