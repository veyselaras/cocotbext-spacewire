"""SpaceWire data-strobe bus handle wrapper: ``di`` and ``si`` are DUT-driven
inputs read by the VIP monitor, while ``do`` and ``so`` are VIP-driven outputs
written by the VIP driver.
"""

from __future__ import annotations

from typing import TypeAlias, cast

from cocotb.handle import LogicArrayObject, LogicObject, SimHandleBase

SpaceWireSignal: TypeAlias = LogicObject | LogicArrayObject

__all__ = ["SpaceWireBus", "SpaceWireSignal"]


class SpaceWireBus:
    """Resolve and expose the four data-strobe SpaceWire signals."""

    di: SpaceWireSignal
    si: SpaceWireSignal
    do: SpaceWireSignal
    so: SpaceWireSignal

    def __init__(self, entity: SimHandleBase, prefix: str = "spw") -> None:
        """Resolve SpaceWire signal handles below *entity* using *prefix*."""
        self._entity = entity
        self._prefix = prefix

        self.di = self._lookup_signal("di")
        self.si = self._lookup_signal("si")
        self.do = self._lookup_signal("do")
        self.so = self._lookup_signal("so")

    def __repr__(self) -> str:
        """Return a compact representation for logs and debugging."""
        entity_name = getattr(self._entity, "_path", repr(self._entity))
        return (
            f"{type(self).__name__}(entity={entity_name!r}, "
            f"prefix={self._prefix!r})"
        )

    def _lookup_signal(self, signal_name: str) -> SpaceWireSignal:
        hdl_name = self._hdl_name(signal_name)
        try:
            signal = getattr(self._entity, hdl_name)
        except AttributeError as exc:
            msg = f"SpaceWire signal {hdl_name!r} was not found on {self._entity!r}"
            raise AttributeError(msg) from exc

        return cast(SpaceWireSignal, signal)

    def _hdl_name(self, signal_name: str) -> str:
        if self._prefix == "":
            return signal_name
        return f"{self._prefix}_{signal_name}"
