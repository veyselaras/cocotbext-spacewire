"""pytest entry point for the VIP<->VIP link handshake cocotb simulation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cocotb_tools.runner import get_runner

SIM_DIR = Path(__file__).resolve().parent
TOPLEVEL = "dual_loopback"


@pytest.mark.parametrize("simulator", ["icarus"])
def test_link_handshake(simulator: str) -> None:
    """Build the dual-loopback DUT and run the handshake tests."""

    waves = os.environ.get("WAVES") == "1"
    build_dir = SIM_DIR / "sim_build" / simulator
    runner = get_runner(simulator)

    runner.build(
        sources=[SIM_DIR / "hdl" / "dual_loopback.v"],
        hdl_toplevel=TOPLEVEL,
        build_dir=build_dir,
        always=True,
        timescale=("1ns", "1ps"),
        waves=waves,
    )
    runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module="tb_link_handshake",
        test_dir=SIM_DIR,
        build_dir=build_dir,
        waves=waves,
    )
