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
    """Raised on ECSS-E-ST-50-12C §5.5.5 flow-control credit violations.

    ``reason`` is one of ``"nchar_with_zero_rx_credit"`` (§5.5.5a.1) or
    ``"fct_overflow"`` (§5.5.5a.2).  ``character`` is ``None`` for pure
    counter-state violations that do not correspond to a decoded character.
    """

    def __init__(self, reason: str, character: Any = None, message: str | None = None) -> None:
        if reason not in _CREDIT_ERROR_REASONS:
            raise ValueError(
                f"unknown CreditError reason {reason!r}; "
                f"expected one of {sorted(_CREDIT_ERROR_REASONS)}"
            )
        super().__init__(character, message or reason)
        self.reason = reason


_CREDIT_ERROR_REASONS = frozenset({"nchar_with_zero_rx_credit", "fct_overflow"})


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
