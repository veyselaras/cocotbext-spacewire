"""Raw SpaceWire Data-Strobe monitor: it waits for the first edge on D or S and
samples D as the received bit, so the transmitter must already have driven
``di``/``si`` to defined initial values before samples are meaningful.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import cocotb
from cocotb.task import Task
from cocotb.triggers import Edge, Event, First, SimTimeoutError, with_timeout

from cocotbext.spacewire.bus import SpaceWireBus
from cocotbext.spacewire.exceptions import DisconnectError

__all__ = ["SpaceWireMonitor"]

_LOG = logging.getLogger(__name__)


class SpaceWireMonitor:
    """Receive raw SpaceWire bits from ``di``/``si`` transitions."""

    bus: SpaceWireBus
    _queue: asyncio.Queue[int]
    _task: Task[None] | None
    _disconnected: bool
    _callbacks: list[Callable[[int], None]]

    def __init__(
        self,
        bus: SpaceWireBus,
        disconnect_timeout_ns: float = 850.0,
        raise_on_disconnect: bool = False,
    ) -> None:
        """Create a passive raw-bit SpaceWire monitor."""
        if disconnect_timeout_ns <= 0.0:
            raise ValueError("disconnect_timeout_ns must be greater than zero")

        self.bus = bus
        self.disconnect_timeout_ns = disconnect_timeout_ns
        self.raise_on_disconnect = raise_on_disconnect
        self._queue = asyncio.Queue()
        self._task = None
        self._disconnected = False
        self._callbacks = []
        self._bit_available = Event()

    @property
    def disconnected(self) -> bool:
        """Return true once the monitor has observed a disconnect timeout."""
        return self._disconnected

    def start(self) -> None:
        """Start the background RX loop."""
        if self._task is not None:
            raise RuntimeError("SpaceWireMonitor is already started")

        self._task = cocotb.start_soon(self._run())

    def stop(self) -> None:
        """Cancel the background RX loop."""
        if self._task is None:
            return

        self._task.cancel()
        self._task = None

    async def recv_bit(self) -> int:
        """Return the next received bit, blocking until one is available."""
        while self._queue.empty():
            self._bit_available.clear()
            await self._bit_available.wait()

        return self._queue.get_nowait()

    async def recv_bits(self, n: int) -> list[int]:
        """Return the next *n* received bits."""
        if n < 0:
            raise ValueError("n must be non-negative")

        return [await self.recv_bit() for _ in range(n)]

    def on_bit(self, cb: Callable[[int], None]) -> None:
        """Register a synchronous callback for every received bit."""
        self._callbacks.append(cb)

    async def _run(self) -> None:
            """Receive bits forever, marking disconnects on transition timeouts."""
            while True:
                edge = First(Edge(self.bus.di), Edge(self.bus.si))

                try:
                    await with_timeout(edge, self.disconnect_timeout_ns, "ns")
                except SimTimeoutError as exc:
                    self._disconnected = True
                    if self.raise_on_disconnect:
                        msg = (
                            "SpaceWire disconnect: no D/S transition within "
                            f"{self.disconnect_timeout_ns} ns"
                        )
                        raise DisconnectError(msg) from exc
                    continue

                self._disconnected = False
                try:
                    bit = int(self.bus.di.value)
                except ValueError:
                    # X/Z transient (start-up, disconnect artifacts) — real bit değil
                    continue
                self._queue.put_nowait(bit)
                self._bit_available.set()
                self._invoke_callbacks(bit)

    def _invoke_callbacks(self, bit: int) -> None:
        for callback in self._callbacks:
            try:
                callback(bit)
            except Exception:
                _LOG.exception("SpaceWire monitor bit callback failed")
