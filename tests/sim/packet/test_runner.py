"""pytest entry point for the VIP<->VIP packet / flow-control cocotb sim."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cocotb_tools.runner import get_runner

SIM_DIR = Path(__file__).resolve().parent
# Reuse the dual-loopback DUT from the Phase-5 handshake tests.
DUT_SOURCE = SIM_DIR.parent / "link_handshake" / "hdl" / "dual_loopback.v"
TOPLEVEL = "dual_loopback"


@pytest.mark.parametrize("simulator", ["icarus"])
def test_packet(simulator: str) -> None:
    """Build the dual-loopback DUT and run the packet-API tests."""

    waves = os.environ.get("WAVES") == "1"
    build_dir = SIM_DIR / "sim_build" / simulator
    runner = get_runner(simulator)

    runner.build(
        sources=[DUT_SOURCE],
        hdl_toplevel=TOPLEVEL,
        build_dir=build_dir,
        always=True,
        timescale=("1ns", "1ps"),
        waves=waves,
    )
    runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module="tb_packet",
        test_dir=SIM_DIR,
        build_dir=build_dir,
        waves=waves,
    )
