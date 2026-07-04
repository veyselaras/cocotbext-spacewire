"""Raw SpaceWire Data-Strobe transmitter driven by simulation time: DS links are
asynchronous, and the receiver recovers timing from ``D XOR S``, so this driver
uses ``Timer`` for bit periods instead of waiting on a DUT clock edge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import cocotb
from cocotb.task import Task
from cocotb.triggers import Event, Timer

from cocotbext.spacewire.bus import SpaceWireBus

__all__ = ["SpaceWireDriver"]


class SpaceWireDriver:
    """Transmit raw SpaceWire bits onto ``do``/``so`` using DS encoding."""

    bus: SpaceWireBus
    _prev_d: int
    _s: int
    _queue: asyncio.Queue[int]
    _task: Task[None] | None

    def __init__(
        self,
        bus: SpaceWireBus,
        bitrate_mbps: float = 10.0,
        initial_d: int = 0,
        initial_s: int = 0,
    ) -> None:
        """Create a raw-bit SpaceWire transmitter."""
        if bitrate_mbps <= 0.0:
            raise ValueError("bitrate_mbps must be greater than zero")

        self.bus = bus
        self._bitrate_mbps = bitrate_mbps
        self._prev_d = self._normalize_bit(initial_d, "initial_d")
        self._s = self._normalize_bit(initial_s, "initial_s")
        self._queue = asyncio.Queue()
        self._task = None
        self._not_empty = Event()
        self._enqueued_bits = 0
        self._driven_bits = 0
        self._drive_waiters: list[tuple[int, Event]] = []

    @property
    def bit_period_ns(self) -> float:
        """Return the current raw-bit period in nanoseconds."""
        return 1000.0 / self._bitrate_mbps

    def start(self) -> None:
        """Start the background TX loop and drive the initial line levels."""
        if self._task is not None:
            raise RuntimeError("SpaceWireDriver is already started")

        self._drive_levels(self._prev_d, self._s)
        self._task = cocotb.start_soon(self._run())

    def stop(self) -> None:
        """Cancel the background TX loop, leaving line levels unchanged."""
        if self._task is None:
            return

        self._task.cancel()
        self._task = None

    async def send_bits(self, bits: Iterable[int]) -> None:
        """Queue raw bits for transmission without waiting for them to finish."""
        for bit in self._normalize_bits(bits):
            self._enqueue_bit(bit)

    async def send_bits_and_wait(self, bits: Iterable[int]) -> None:
        """Queue raw bits and wait until the final bit has been driven."""
        normalized_bits = self._normalize_bits(bits)
        if not normalized_bits:
            return

        for bit in normalized_bits:
            self._enqueue_bit(bit)

        await self._wait_until_driven(self._enqueued_bits)

    async def _run(self) -> None:
        """Transmit queued bits forever."""
        while True:
            while self._queue.empty():
                self._not_empty.clear()
                await self._not_empty.wait()

            bit = self._queue.get_nowait()
            try:
                await self._tx_bit(bit)
            finally:
                self._queue.task_done()

    async def _tx_bit(self, b: int) -> None:
        """Transmit one normalized bit using the SpaceWire DS transition rule."""
        bit = self._normalize_bit(b, "bit")

        if bit == self._prev_d:
            self._s ^= 1

        self._drive_levels(bit, self._s)
        self._prev_d = bit
        self._mark_bit_driven()

        await Timer(self.bit_period_ns, unit="ns")

    def _enqueue_bit(self, bit: int) -> None:
        self._queue.put_nowait(bit)
        self._enqueued_bits += 1
        self._not_empty.set()

    async def _wait_until_driven(self, target_count: int) -> None:
        if self._driven_bits >= target_count:
            return

        event = Event()
        self._drive_waiters.append((target_count, event))
        await event.wait()

    def _mark_bit_driven(self) -> None:
        self._driven_bits += 1
        ready: list[Event] = []
        pending: list[tuple[int, Event]] = []

        for target_count, event in self._drive_waiters:
            if target_count <= self._driven_bits:
                ready.append(event)
            else:
                pending.append((target_count, event))

        self._drive_waiters = pending
        for event in ready:
            event.set()

    def _drive_levels(self, d_level: int, s_level: int) -> None:
        self.bus.do.value = d_level
        self.bus.so.value = s_level

    @staticmethod
    def _normalize_bits(bits: Iterable[int]) -> list[int]:
        return [SpaceWireDriver._normalize_bit(bit, "bits") for bit in bits]

    @staticmethod
    def _normalize_bit(bit: int, name: str) -> int:
        if bit in (0, 1):
            return int(bit)
        msg = f"{name} must contain only binary values (0 or 1); got {bit!r}"
        raise ValueError(msg)
