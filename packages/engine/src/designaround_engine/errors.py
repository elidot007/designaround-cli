from __future__ import annotations


class EngineError(Exception):
    """Base class for engine errors that are safe to show to users and agents."""

    code = "engine_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ProgramRejected(EngineError):
    """A CAD program failed static validation (sandbox policy)."""

    code = "program_rejected"


class BuildFailed(EngineError):
    """A CAD program was accepted but failed while building geometry."""

    code = "build_failed"


class NotFound(EngineError):
    code = "not_found"


class InvalidInput(EngineError):
    code = "invalid_input"
