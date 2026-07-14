"""VIP<->VIP SpaceWireLink handshake tests over the dual-loopback DUT."""

from __future__ import annotations

import cocotb
from cocotb.handle import SimHandleBase
from cocotb.triggers import Combine, Timer

from cocotbext.spacewire.link import SpaceWireLink
from cocotbext.spacewire.link_fsm import State


async def _wait_ns(ns: int) -> None:
    await Timer(ns, unit="ns")


async def _quiesce(dut: SimHandleBase) -> None:
    """Return the four driven signals to 0 and wait for edges to settle.

    A previous test may have left the DUT lines mid-character.  Because the
    loopback is combinational, the first fresh driver write generates an
    edge on the peer's RX line; if the peer's monitor arms next, it captures
    that transition as a spurious bit and desynchronises parity framing.
    Quiescing before the Links are created keeps each test independent.
    """

    for name in ("spw_a_do", "spw_a_so", "spw_b_do", "spw_b_so"):
        getattr(dut, name).value = 0
    await Timer(1000, unit="ns")


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_basic_handshake(dut: SimHandleBase) -> None:
    """Two link_start endpoints must reach Run at both ends."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", bit_rate=200_000_000, link_start=True)
    link_b = SpaceWireLink(dut, prefix="spw_b", bit_rate=200_000_000, link_start=True)

    try:
        await link_a.start()
        await link_b.start()
        await Combine(
            cocotb.start_soon(link_a.connect(timeout_us=200)),
            cocotb.start_soon(link_b.connect(timeout_us=200)),
        )
        assert link_a.state is State.RUN
        assert link_b.state is State.RUN
        # Give a little Run time so the bit-rate switch is exercised.
        await _wait_ns(1000)
        assert link_a.bit_rate_bps == 200_000_000
        assert link_b.bit_rate_bps == 200_000_000
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_autostart_handshake(dut: SimHandleBase) -> None:
    """A pushes with link_start, B follows via auto_start after seeing NULL."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", bit_rate=100_000_000, link_start=True)
    link_b = SpaceWireLink(dut, prefix="spw_b", bit_rate=100_000_000, auto_start=True)

    try:
        await link_a.start()
        await link_b.start()
        await Combine(
            cocotb.start_soon(link_a.connect(timeout_us=300)),
            cocotb.start_soon(link_b.connect(timeout_us=300)),
        )
        assert link_a.state is State.RUN
        assert link_b.state is State.RUN
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_started_timeout(dut: SimHandleBase) -> None:
    """A alone: never sees a peer NULL, must cycle back through ErrorReset."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", bit_rate=100_000_000, link_start=True)
    # B is not created / not started -- its lines stay quiet at 0, so A
    # sees a silent peer and must cycle back through ErrorReset.

    try:
        await link_a.start()

        # Observe multiple ErrorReset re-entries within the first 200 us.
        await link_a.wait_for_error_reset(count=2, timeout_us=200)
        assert link_a.error_reset_count >= 2
        assert link_a.state is not State.RUN
    finally:
        link_a.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_connecting_timeout(dut: SimHandleBase) -> None:
    """B only ever sends NULLs -> A stalls in Connecting until 12.8us timeout."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", bit_rate=100_000_000, link_start=True)
    link_b = SpaceWireLink(
        dut,
        prefix="spw_b",
        bit_rate=100_000_000,
        link_start=True,
        only_nulls=True,
    )

    try:
        await link_a.start()
        await link_b.start()

        # A must drop out of Connecting into ErrorReset at least twice.
        await link_a.wait_for_error_reset(count=2, timeout_us=300)
        assert link_a.error_reset_count >= 2
        assert link_a.state is not State.RUN
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_disconnect_from_run(dut: SimHandleBase) -> None:
    """Reach Run, then call disconnect on A: B must see a disconnect + reset."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", bit_rate=100_000_000, link_start=True)
    link_b = SpaceWireLink(dut, prefix="spw_b", bit_rate=100_000_000, link_start=True)

    try:
        await link_a.start()
        await link_b.start()
        await Combine(
            cocotb.start_soon(link_a.connect(timeout_us=300)),
            cocotb.start_soon(link_b.connect(timeout_us=300)),
        )
        assert link_a.state is State.RUN
        assert link_b.state is State.RUN

        b_reset_start = link_b.error_reset_count
        await link_a.disconnect()

        # B's disconnect watchdog polls every 100 ns; give it a few polls to
        # notice the silent peer (850 ns disconnect + margin).
        for _ in range(200):
            if link_b.error_reset_count > b_reset_start:
                break
            await _wait_ns(100)

        assert link_b.error_reset_count > b_reset_start
        assert link_b.state is not State.RUN
    finally:
        link_a.stop()
        link_b.stop()
