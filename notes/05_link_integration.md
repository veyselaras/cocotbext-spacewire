# Link Integration (`SpaceWireLink`)

Phase 5b wires the pure-Python :mod:`link_fsm` up to real signals via the
existing :class:`SpaceWireDriver` (TX bit pump) and :class:`SpaceWireMonitor`
(RX bit sampler).  No protocol logic lives in the driver or monitor; they
stay dumb about characters.

## Data flow

```
   RX side                                     TX side
   -------                                     -------
                                               tx_mode from FSM
   di / si edges                                     |
        |                                            v
   SpaceWireMonitor          _CharacterParser   Encoder.encode()
        | on_bit(bit)         (Decoder wrapper)      |
        v                            |               v
   parser.feed_bit()  -->  Decoder --+->  FsmEvent   Driver TX queue
                                     |    (GOT_*    (send_bits_and_wait)
                                     |     RX_ERR)       |
                                     v                   v
                                   LinkFSM.feed() <-- SENT_* events
                                        |
                                        v
                             Output(state, tx_mode, rx_enabled, bit_rate_bps)
                                        |
                                        +--> re-arm 6.4us / 12.8us timers
                                        +--> update driver bit rate
                                        +--> wake TX loop
```

## The three background coroutines

`SpaceWireLink.start()` spawns:

1. **`_tx_loop`** — reads `fsm.output().tx_mode`, encodes the appropriate
   next primitive (NULL, FCT, or NULL in Run for Phase 5b), pushes it to
   the driver via `send_bits_and_wait`, and feeds `SENT_NULL` / `SENT_FCT`
   back into the FSM.  In `TxMode.RESET` it parks on `_tx_wakeup`, which
   is set on every state change.
2. **`_disconnect_watchdog`** — polls every 100 ns.  Feeds `RX_ERR` only
   when the peer stops driving edges AND the FSM has already latched
   `got_null` (i.e. the peer has been heard from at least once).  Before
   that first NULL, silence is expected during handshake bring-up; firing
   RX_ERR eagerly would trap both endpoints in an ErrorReset loop while
   they wait for each other.
3. **`_run_timer`** (short-lived, one per state visit) — the 6.4 us
   ErrorReset dwell and the 12.8 us ErrorWait / Started / Connecting
   timeouts.  Each state entry cancels the previous timer via a generation
   counter so a stale expiry can never feed a `TICK_*` event into the
   wrong state.

## `_CharacterParser` — a thin `Decoder` wrapper

The parser is a nine-line class that forwards every line bit to the
existing :class:`characters.Decoder` and translates each completed
high-level character into an FSM event:

| Decoded character                | FSM event      |
| -------------------------------- | -------------- |
| `NULL` (ESC + FCT primitives)    | `GOT_NULL`     |
| bare `FCT`                       | `GOT_FCT`      |
| `DATA`, `EOP`, `EEP`             | `GOT_N_CHAR`   |
| `TIMECODE`, `BROADCAST`          | `GOT_TIME_CODE`|
| parity / escape error            | `RX_ERR`       |

Because the underlying decoder already handles the ESC-lookahead for NULL
and time-codes, the parser cannot leak two events for one NULL — a common
class of bug the prompt calls out explicitly.

## FSM chained transitions

The FSM's `feed()` was upgraded to run its handler in a bounded loop with
a synthetic `None` event, so that entering Ready with `linkStart` already
latched immediately advances to Started.  Without this, the FSM would sit
in Ready forever because the last real event fed (`TICK_12_8US`) has been
consumed by the ErrorWait handler.  All existing FSM unit tests continue
to pass — the two that expected the old "stop in intermediate state"
behaviour were tightened to allow either the Ready or the auto-advanced
Started state.

## Bit-rate switching

`_on_state_change` writes `driver._bitrate_mbps = out.bit_rate_bps / 1e6`
on every state entry.  The driver reads `bit_period_ns` fresh at the top
of every `_tx_bit`, so the switch takes effect on the next bit boundary.
The pre-Run states hold 10 Mbit/s (ECSS R/SWC.195); Run steps to the
`target_bit_rate_bps` the user passed to the constructor.

## Cross-test signal contamination (why `_quiesce`)

The dual-loopback DUT is combinational: `spw_a_di = spw_b_do` and vice
versa.  When one test ends mid-character, the DUT lines hold their last
value.  The next test's first driver write generates an edge on the
peer's RX line; if the peer's monitor arms next, it captures that
transition as a spurious first bit and desynchronises the parity chain.

The testbench's `_quiesce(dut)` helper forces all four driven signals to
0 and waits 1 us for edges to settle before constructing the Links.  It
runs at the top of every test; without it, three of the five scenarios
fail intermittently in a full run while passing individually.

The Link itself also inserts a 1 ns settle between `driver.start()` and
`monitor.start()` inside `start()`.

## Known limitations (deferred to later phases)

- **N-Char / TimeCode transmission** — Phase 6.  Run state currently keeps
  the line alive by continuing to emit NULLs.
- **Flow-control credit** — Phase 6.  `SEND_NULLS_FCTS_NCHARS_TIMECODES`
  behaves like `SEND_NULLS` for now.
- **Error-injection surfaces** (`inject_parity_error`, ...) — Phase 7.
  Parity errors are already correctly detected by the parser, so once the
  driver grows an injection API the FSM will react without further work.
- **SpaceWire Light DUT handshake** — Phase 10.
- **Time-code broadcast events** — Phase 8.  Currently mapped to
  `GOT_TIME_CODE` but there is no user-visible time-code delivery.

## Handshake reference timings (from the passing sim tests)

| Test                              | End time | Note                                 |
| --------------------------------- | -------- | ------------------------------------ |
| `test_basic_handshake`            | ~23 us   | Both sides in Run + 1 us hold        |
| `test_autostart_handshake`        | ~22 us   | A pushes, B follows on first NULL    |
| `test_started_timeout`            | ~65 us   | 2 full ErrorReset cycles             |
| `test_connecting_timeout`         | ~54 us   | 2 full ErrorReset cycles             |
| `test_disconnect_from_run`        | ~24 us   | B detects disconnect < 1 us after A  |
