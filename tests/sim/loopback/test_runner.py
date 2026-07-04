"""pytest entry point for the raw SpaceWire loopback cocotb simulation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cocotb_tools.runner import get_runner

SIM_DIR = Path(__file__).resolve().parent
TOPLEVEL = "loopback_top"


@pytest.mark.parametrize("simulator", ["icarus"])
def test_loopback(simulator: str) -> None:
    """Build and run the loopback tests with the requested simulator."""
    waves = os.environ.get("WAVES") == "1"
    build_dir = SIM_DIR / "sim_build" / simulator
    runner = get_runner(simulator)

    runner.build(
        sources=[SIM_DIR / "hdl" / "loopback_top.v"],
        hdl_toplevel=TOPLEVEL,
        build_dir=build_dir,
        always=True,
        timescale=("1ns", "1ps"),
        waves=waves,
    )
    runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module="tb_loopback",
        test_dir=SIM_DIR,
        build_dir=build_dir,
        waves=waves,
    )
