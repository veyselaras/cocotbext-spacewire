"""SpaceWire packet type (Parkes §3, ECSS-E-ST-50-12C §5.4).

A SpaceWire packet is a sequence of data N-Chars terminated by either an EOP
(end-of-packet) or an EEP (error-end-of-packet).  ECSS does not constrain the
content of ``data``; SpaceWire only transports bytes.  Per §5.5.4e NOTE, an
empty packet is legal — the terminator alone is a full packet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

__all__ = ["SpaceWirePacket", "Terminator"]


Terminator = Literal["EOP", "EEP"]
_VALID_TERMINATORS: frozenset[str] = frozenset({"EOP", "EEP"})


def _validate_data(data: Iterable[int]) -> tuple[int, ...]:
    normalized: list[int] = []
    for i, byte in enumerate(data):
        if not isinstance(byte, int) or isinstance(byte, bool):
            raise ValueError(f"data[{i}] must be int, got {type(byte).__name__}")
        if not 0 <= byte <= 0xFF:
            raise ValueError(f"data[{i}] must be in 0..255, got {byte}")
        normalized.append(byte)
    return tuple(normalized)


@dataclass(frozen=True)
class SpaceWirePacket:
    """Immutable SpaceWire packet: N-Char payload plus terminator."""

    data: tuple[int, ...]
    terminator: Terminator

    def __post_init__(self) -> None:
        # Accept lists / iterables at construction, freeze to a tuple.
        object.__setattr__(self, "data", _validate_data(self.data))
        if self.terminator not in _VALID_TERMINATORS:
            raise ValueError(
                f"terminator must be one of {sorted(_VALID_TERMINATORS)}, "
                f"got {self.terminator!r}"
            )

    def __len__(self) -> int:
        """Total N-Char count including the terminator.

        §5.5.4e NOTE treats EOP and EEP as credit-counted N-Chars, so
        the total credit needed to send this packet is ``len(packet)``.
        """

        return len(self.data) + 1

    @classmethod
    def eop(cls, data: Iterable[int]) -> "SpaceWirePacket":
        """Build a normal end-of-packet packet."""

        return cls(data=tuple(data), terminator="EOP")

    @classmethod
    def eep(cls, data: Iterable[int]) -> "SpaceWirePacket":
        """Build an error-end-of-packet packet (partial/aborted upstream)."""

        return cls(data=tuple(data), terminator="EEP")
