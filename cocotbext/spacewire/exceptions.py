"""Exceptions shared by SpaceWire verification layers.

The short names are canonical for new code.  The longer ``SpaceWire*`` names are
kept as aliases so existing imports continue to resolve.
"""

from __future__ import annotations

from typing import Any


class ProtocolError(Exception):
    """Base exception for SpaceWire protocol checking."""

    def __init__(self, character: Any, message: str) -> None:
        super().__init__(message)
        self.character = character


class ParityError(ProtocolError):
    """Raised by strict monitors when a parity violation is decoded."""


class EscapeError(ProtocolError):
    """Raised by strict monitors when an illegal escape sequence is decoded."""


class CreditError(ProtocolError):
    """Reserved for future flow-control credit violations."""


class DisconnectError(Exception):
    """Raised when the SpaceWire link is disconnected."""


SpaceWireProtocolError = ProtocolError
SpaceWireParityError = ParityError
SpaceWireEscapeError = EscapeError


__all__ = [
    "CreditError",
    "DisconnectError",
    "EscapeError",
    "ParityError",
    "ProtocolError",
    "SpaceWireEscapeError",
    "SpaceWireParityError",
    "SpaceWireProtocolError",
]
