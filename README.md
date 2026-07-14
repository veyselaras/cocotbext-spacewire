# cocotbext-spacewire

A cocotb extension providing a SpaceWire Verification IP (VIP) for simulating
and verifying SpaceWire interfaces in HDL designs.

## How this is being built

This project is built entirely via *vibe coding*: the author reads the
relevant ECSS / Parkes sections and prompts an LLM to implement each phase
against those references. What you're reading is the resulting codebase, not
a hand-written one.

## Status

Work in progress, built phase-by-phase per an internal roadmap.

- **Done** — Phases 1–6: repo skeleton, DS codec, character layer, cocotb
  bit-level TX/RX, link FSM, flow control + packet API.
- **Pending** — Phases 7–11: error injection, time-code, coverage,
  reference DUT + CI, docs / packaging.

## What works today

- DS encode / decode (signal layer)
- SpaceWire characters: NULL, FCT, EOP, EEP, ESC, data — with parity
- Link FSM per ECSS-E-ST-50-12C §8.5 (ErrorReset → ErrorWait → Ready →
  Started → Connecting → Run, 6.4 µs / 12.8 µs timers, Rev.1
  `sentNULL`/`sentFCT` gates)
- Flow control (credit counter, 56-credit cap) per §5.5.4 / §5.5.5
- Packet API: `send_packet` / `recv_packet` / `send_char`
- VIP↔VIP loopback verified in Icarus Verilog

## Standards reference

ECSS-E-ST-50-12C (2008), with Rev.1 (15 May 2019) updates where noted.

## Install

```
pip install -e .
```

Dev install only — not on PyPI yet.

## Quick usage

Mirrors the pattern in `tests/sim/packet/`:

```python
import cocotb
from cocotbext.spacewire.link import SpaceWireLink
from cocotbext.spacewire.link_fsm import State

@cocotb.test()
async def test_send_recv(dut):
    a = SpaceWireLink(dut, prefix="spw_a", bit_rate=200_000_000, link_start=True)
    b = SpaceWireLink(dut, prefix="spw_b", bit_rate=200_000_000, link_start=True)
    await a.start(); await b.start()
    await a.connect(); await b.connect()
    assert a.state is State.RUN and b.state is State.RUN

    sender = cocotb.start_soon(a.send_packet([0x01, 0x02, 0x03], "EOP"))
    packet = await b.recv_packet()
    await sender

    assert packet.data == (1, 2, 3)
    assert packet.terminator == "EOP"
```

The DUT is a plain combinational cross-connect of the two endpoints'
`do`/`so` outputs and `di`/`si` inputs — see
`tests/sim/link_handshake/hdl/dual_loopback.v`.

## Requirements

- Python 3.10+
- cocotb 2.x
- Icarus Verilog or GHDL

## License

MIT (see `LICENSE`).
