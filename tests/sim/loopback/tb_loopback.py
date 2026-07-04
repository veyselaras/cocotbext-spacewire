"""VIP-to-VIP SpaceWire raw-bit loopback tests."""

from __future__ import annotations

import random

import cocotb
from cocotb.handle import SimHandleBase
from cocotb.triggers import Timer

from cocotbext.spacewire import SpaceWireBus, SpaceWireDriver, SpaceWireMonitor


def _make_loopback_vip(
    dut: SimHandleBase,
) -> tuple[SpaceWireDriver, SpaceWireMonitor]:
    bus = SpaceWireBus(dut, prefix="spw")
    return SpaceWireDriver(bus), SpaceWireMonitor(bus)


@cocotb.test(timeout_time=100, timeout_unit="us")
async def test_loopback_bits(dut: SimHandleBase) -> None:
    """Round-trip a short deterministic raw-bit stream."""
    bits = [1, 0, 1, 1, 0, 0, 1, 0]
    driver, monitor = _make_loopback_vip(dut)

    driver.start()
    await Timer(1, unit="ns")   # ilk Z→0 propagasyonu geçsin
    monitor.start()
    try:
        await driver.send_bits(bits)
        assert await monitor.recv_bits(len(bits)) == bits
    finally:
        driver.stop()
        monitor.stop()


@cocotb.test(timeout_time=100, timeout_unit="us")
async def test_loopback_random(dut: SimHandleBase) -> None:
    """Round-trip a fixed-seed random raw-bit stream."""
    rng = random.Random(0x5A17)
    bits = [rng.randrange(2) for _ in range(256)]
    driver, monitor = _make_loopback_vip(dut)

    driver.start()
    monitor.start()
    try:
        await driver.send_bits(bits)
        assert await monitor.recv_bits(len(bits)) == bits
    finally:
        driver.stop()
        monitor.stop()


@cocotb.test(timeout_time=100, timeout_unit="us")
async def test_disconnect_timeout(dut: SimHandleBase) -> None:
    """Detect a disconnected source when no D/S transition arrives."""
    bus = SpaceWireBus(dut, prefix="spw")
    monitor = SpaceWireMonitor(bus)

    monitor.start()
    try:
        await Timer(1000, unit="ns")
        assert monitor.disconnected is True
    finally:
        monitor.stop()
