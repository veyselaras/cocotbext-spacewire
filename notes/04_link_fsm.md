# Link Initialization FSM

Implementation of the SpaceWire exchange-level FSM from **ECSS-E-ST-50-12C
Rev.1** (15 May 2019), section 8.5.  Pure Python, clock-free.  The cocotb
layer owns the 6.4 us / 12.8 us timers and feeds them as `TICK_6_4US` and
`TICK_12_8US` events.

## State diagram

```
                       +--------------+
                +----->|  ErrorReset  |<-------------------------+
                |      +------+-------+                          |
                |             | TICK_6_4US (and !linkDisabled)   |
                |             v                                  |
                |      +--------------+                          |
                |      |  ErrorWait   | --RX_ERR / char-err----->+
                |      +------+-------+                          |
                |             | TICK_12_8US                      |
                |             v                                  |
                |      +--------------+                          |
                |      |    Ready     | --RX_ERR / char-err----->+
                |      +------+-------+                          |
                |             | LinkEnabled                      |
                |             v                                  |
                |      +--------------+                          |
                |      |   Started    | --RX_ERR / 12.8us------->+
                |      +------+-------+                          |
                |             | sentNULL AND gotNULL             |
                |             v                                  |
                |      +--------------+                          |
                |      |  Connecting  | --RX_ERR / N-Char /      |
                |      +------+-------+   TimeCode / 12.8us----->+
                |             | sentFCT AND gotFCT               |
                |             v                                  |
                |      +--------------+                          |
                +------|     Run      | --RX_ERR / creditErr /   |
                       +--------------+   linkDisable------------+
```

## Per-state TX / RX / bit-rate

| State        | tx_mode                              | rx_enabled | bit_rate     |
| ------------ | ------------------------------------ | ---------- | ------------ |
| ErrorReset   | RESET (D=0, S=0)                     | off        | 10 Mbit/s    |
| ErrorWait    | RESET                                | on         | 10 Mbit/s    |
| Ready        | RESET                                | on         | 10 Mbit/s    |
| Started      | SEND_NULLS                           | on         | 10 Mbit/s    |
| Connecting   | SEND_NULLS_FCTS                      | on         | 10 Mbit/s    |
| Run          | SEND_NULLS_FCTS_NCHARS_TIMECODES     | on         | target       |

The 10 Mbit/s constraint on everything before Run is Rev.1 R/SWC.195 — every
link must handshake at 10 Mbit/s before switching to the operating rate.

## LinkEnabled (Rev.1)

```
LinkEnabled = !linkDisabled AND (linkStart OR (autoStart AND gotNULL))
```

`link_disable` is a hard block: it holds ErrorReset closed and forces Run
back to ErrorReset.  `auto_start` only fires once the peer has sent a NULL
(Parkes §3, Figure 39) — this is the "one side pushes, the other side
follows" mode.

## The `gotNULL` gate

The FSM's shared error predicate for ErrorWait / Ready / Started is

```
error = rx_err OR (got_null_latched AND (got_fct OR got_n_char OR got_time_code))
```

Before `gotNULL` has been latched the RX side is not yet character-
synchronised, so an isolated FCT / N-Char / TimeCode is treated as noise
rather than a protocol violation.  Once `gotNULL` fires it stays latched
until the next re-entry to ErrorReset — where we also clear the transient
`sent_null_in_started`, `sent_fct_in_connecting`, and
`got_fct_in_connecting` flags so a fresh handshake starts clean.

## Rev.1 vs 2008 diff (why this matters)

Rev.1 tightened two transitions that the 2008 edition left one-sided:

1. **Started -> Connecting**: 2008 only required `gotNULL`.  Rev.1 requires
   `sentNULL AND gotNULL`.  Without this, a receiver could advance on the
   peer's NULL before its own TX had even come up, and disconnect-storms
   were possible during flaky links.
2. **Connecting -> Run**: 2008 only required `gotFCT`.  Rev.1 requires
   `sentFCT AND gotFCT`.  Same rationale.
3. Rev.1 explicitly forbids `linkStart` or `autoStart` from lifting the
   link while `linkDisabled` is set — the 2008 wording was ambiguous.
4. Rev.1 makes the 6.4 us / 12.8 us dwells fixed timeouts rather than
   "at least" bounds.
5. Signal-level rule: in ErrorReset the transmitter must drive D=0, S=0.
   The FSM signals this via `tx_mode = RESET`; the cocotb layer must
   translate that into the actual pin state.

## Notes for the cocotb wiring in Phase 5b

- The FSM's `feed()` is synchronous and idempotent-per-event.  Timers and
  character events must be serialised onto a single coroutine before being
  fed in.
- `Output.bit_rate_bps` changes at the moment we enter Run; the driver's
  Timer period should be reprogrammed on that edge.
- Handling of `SENT_NULL` / `SENT_FCT`: the driver knows when it emits
  each; feed the event once per transmission.  The FSM only latches the
  flag while the appropriate state is active — feeding SENT_NULL in Ready
  or Run is safely ignored.
