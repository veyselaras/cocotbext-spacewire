"""VIP<->VIP SpaceWire packet API and flow-control tests over dual_loopback."""

from __future__ import annotations

import cocotb
from cocotb.handle import SimHandleBase
from cocotb.triggers import Combine, Timer

from cocotbext.spacewire.link import SpaceWireLink
from cocotbext.spacewire.link_fsm import State
from cocotbext.spacewire.packet import SpaceWirePacket


async def _wait_ns(ns: int) -> None:
    await Timer(ns, unit="ns")


async def _quiesce(dut: SimHandleBase) -> None:
    """Force all four driven DUT lines to 0 and let edges settle.

    Same rationale as the Phase-5 handshake tests: the dual_loopback DUT
    is combinational, so a stale line from a prior test can inject a
    spurious first bit when the next monitor arms.
    """

    for name in ("spw_a_do", "spw_a_so", "spw_b_do", "spw_b_so"):
        getattr(dut, name).value = 0
    await Timer(1000, unit="ns")


async def _bring_up(dut: SimHandleBase, **link_kwargs) -> tuple[SpaceWireLink, SpaceWireLink]:
    """Quiesce the DUT and drive both endpoints all the way to Run."""

    await _quiesce(dut)
    link_a = SpaceWireLink(dut, prefix="spw_a", link_start=True, **link_kwargs)
    link_b = SpaceWireLink(dut, prefix="spw_b", link_start=True, **link_kwargs)
    await link_a.start()
    await link_b.start()
    await Combine(
        cocotb.start_soon(link_a.connect(timeout_us=300)),
        cocotb.start_soon(link_b.connect(timeout_us=300)),
    )
    assert link_a.state is State.RUN
    assert link_b.state is State.RUN
    return link_a, link_b


# --------------------------------------------------------------------------- #
# Basic packet round-trips
# --------------------------------------------------------------------------- #


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_basic_packet(dut: SimHandleBase) -> None:
    """Roadmap example: send_packet([0x01, 0x02, 0x03]) round-trips as EOP."""

    link_a, link_b = await _bring_up(dut, bit_rate=200_000_000)
    try:
        sender = cocotb.start_soon(link_a.send_packet([0x01, 0x02, 0x03], "EOP"))
        received = await link_b.recv_packet()
        await sender  # ensure the sender finished draining its queue.
        assert received == SpaceWirePacket(data=(1, 2, 3), terminator="EOP")
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_eep_packet(dut: SimHandleBase) -> None:
    """EEP terminator round-trips just like EOP."""

    link_a, link_b = await _bring_up(dut, bit_rate=200_000_000)
    try:
        sender = cocotb.start_soon(link_a.send_packet([0xDE, 0xAD, 0xBE, 0xEF], "EEP"))
        received = await link_b.recv_packet()
        await sender
        assert received == SpaceWirePacket(data=(0xDE, 0xAD, 0xBE, 0xEF), terminator="EEP")
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_empty_packet(dut: SimHandleBase) -> None:
    """§5.5.4e NOTE: an empty packet is legal (terminator alone)."""

    link_a, link_b = await _bring_up(dut, bit_rate=200_000_000)
    try:
        sender = cocotb.start_soon(link_a.send_packet([], "EOP"))
        received = await link_b.recv_packet()
        await sender
        assert received == SpaceWirePacket(data=(), terminator="EOP")
        assert len(received) == 1
    finally:
        link_a.stop()
        link_b.stop()


# --------------------------------------------------------------------------- #
# Flow-control back-pressure
# --------------------------------------------------------------------------- #


@cocotb.test(timeout_time=1000, timeout_unit="us")
async def test_backpressure_slow_receiver(dut: SimHandleBase) -> None:
    """End-B stalls receiving; end-A queues past initial credit, then drains.

    We deliberately shrink the RX FIFO capacity to 8 so a single missed
    drain fills the queue: with ``rx_fifo_capacity=8`` there is no
    initial-burst headroom (§5.5.4p), so end-A gets exactly one FCT of
    credit from the handshake and stalls the moment its 8 slots are used.

    Verify: (a) A blocks — the outbound N-Chars pile up in its TX queue
    without being emitted beyond the current tx_credit; (b) no deadlock;
    (c) once B resumes and grants FCTs, A drains and the packet arrives
    intact.
    """

    link_a, link_b = await _bring_up(
        dut, bit_rate=100_000_000, rx_fifo_capacity=8
    )
    try:
        payload = list(range(60))  # 60 data + EOP -> multiple stop-go bursts
        sender_task = cocotb.start_soon(link_a.send_packet(payload, "EOP"))

        # Let A drain the initial 8-char credit window and pile the rest
        # into its TX queue.  B is NOT calling recv_packet yet.
        await _wait_ns(20_000)

        assert not sender_task.done(), (
            f"sender finished too early: tx_queue={link_a.tx_queue_depth}, "
            f"tx_credit={link_a.tx_credit}"
        )
        assert link_a.tx_queue_depth > 0
        assert link_a.tx_credit == 0  # §5.5.4d: stalled at zero credit

        # Now B drains its RX queue: this frees RX headroom, B issues
        # more FCTs, A resumes.
        received = await link_b.recv_packet()

        await sender_task
        assert received.data == tuple(payload)
        assert received.terminator == "EOP"
        assert link_a.tx_queue_depth == 0
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=500, timeout_unit="us")
async def test_error_reset_zeros_credit(dut: SimHandleBase) -> None:
    """§5.5.4g / §5.5.4m: entry to ErrorReset zeros both credit counters.

    Bring the link to Run (which naturally accumulates rx_credit and
    tx_credit from the handshake + initial FCT burst), force a link
    reset by disabling A, and check both credit counters clear.
    """

    from cocotbext.spacewire.link_fsm import Event as FsmEvent

    link_a, link_b = await _bring_up(dut, bit_rate=100_000_000)
    try:
        # After the initial FCT burst both credit counters should be at
        # or near their steady-state values (well above zero).
        assert link_a.tx_credit > 0
        assert link_a.rx_credit > 0

        # Force A into ErrorReset via link-disable (the FSM path we know
        # unambiguously triggers _on_state_change with new==ErrorReset).
        link_a._feed_fsm(FsmEvent.LINK_DISABLE)
        assert link_a.state is State.ERROR_RESET
        assert link_a.tx_credit == 0  # §5.5.4g
        assert link_a.rx_credit == 0  # §5.5.4m
    finally:
        link_a.stop()
        link_b.stop()


@cocotb.test(timeout_time=1000, timeout_unit="us")
async def test_credit_exhaustion_recovery(dut: SimHandleBase) -> None:
    """Send exactly one credit window's worth of N-Chars, then verify the
    next one blocks until the receiver grants a fresh FCT.

    Using ``rx_fifo_capacity=8`` the credit window is exactly 8 N-Chars,
    so a 9-N-Char payload guarantees the last N-Char blocks in the TX
    queue until the receiver drains and B emits the next FCT (§5.5.4d,
    §5.5.4e.2, §5.5.4l.1).
    """

    link_a, link_b = await _bring_up(
        dut, bit_rate=100_000_000, rx_fifo_capacity=8
    )
    try:
        # 8 data + EOP = 9 N-Chars: fills the initial credit window plus
        # one, so the last N-Char waits for a fresh FCT.
        payload = list(range(8))
        sender_task = cocotb.start_soon(link_a.send_packet(payload, "EOP"))

        # Give A time to drain the initial 8 credits and block on the 9th.
        await _wait_ns(15_000)
        assert not sender_task.done(), (
            "sender should be blocked past the 8-credit window; "
            f"tx_credit={link_a.tx_credit}, tx_queue={link_a.tx_queue_depth}"
        )
        assert link_a.tx_credit == 0

        received = await link_b.recv_packet()
        await sender_task
        assert received.data == tuple(payload)
        assert received.terminator == "EOP"
    finally:
        link_a.stop()
        link_b.stop()
