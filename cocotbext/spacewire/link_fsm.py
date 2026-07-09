"""SpaceWire link initialization state machine (pure Python).

Implements the exchange-level FSM from ECSS-E-ST-50-12C Rev.1 (15 May 2019),
section 8.5.  The FSM is intentionally clock-free: 6.4 microsecond and 12.8
microsecond ticks are fed in as events by the cocotb layer, along with the
character-level and application-level events observed by the driver, monitor,
and user API.

Rev.1 differs from the 2008 edition on two critical transitions: the
Started -> Connecting transition requires both ``sentNULL`` and ``gotNULL``,
and the Connecting -> Run transition requires both ``sentFCT`` and ``gotFCT``.
See :mod:`notes/04_link_fsm.md` for the diff summary and rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

INITIAL_BIT_RATE_BPS = 10_000_000  # Rev.1 R/SWC.195: start at 10 Mbit/s.


class State(Enum):
    """Link initialization states (Rev.1 sec. 8.5.2)."""

    ERROR_RESET = auto()
    ERROR_WAIT = auto()
    READY = auto()
    STARTED = auto()
    CONNECTING = auto()
    RUN = auto()


class TxMode(Enum):
    """What the TX side is allowed to transmit in the current state."""

    RESET = auto()  # D=0, S=0 on the wire.
    SEND_NULLS = auto()
    SEND_NULLS_FCTS = auto()
    SEND_NULLS_FCTS_NCHARS_TIMECODES = auto()


class Event(Enum):
    """Inputs the outer world can feed to the FSM."""

    GOT_NULL = auto()
    GOT_FCT = auto()
    GOT_N_CHAR = auto()
    GOT_TIME_CODE = auto()
    RX_ERR = auto()
    CREDIT_ERR = auto()
    SENT_NULL = auto()
    SENT_FCT = auto()
    LINK_START = auto()
    LINK_START_CLEAR = auto()
    LINK_DISABLE = auto()
    LINK_DISABLE_CLEAR = auto()
    AUTO_START = auto()
    AUTO_START_CLEAR = auto()
    TICK_6_4US = auto()
    TICK_12_8US = auto()


@dataclass(frozen=True)
class Output:
    """Snapshot of what the FSM is telling the outer world right now."""

    state: State
    tx_mode: TxMode
    rx_enabled: bool
    bit_rate_bps: int


_STATE_TX: dict[State, TxMode] = {
    State.ERROR_RESET: TxMode.RESET,
    State.ERROR_WAIT: TxMode.RESET,
    State.READY: TxMode.RESET,
    State.STARTED: TxMode.SEND_NULLS,
    State.CONNECTING: TxMode.SEND_NULLS_FCTS,
    State.RUN: TxMode.SEND_NULLS_FCTS_NCHARS_TIMECODES,
}

_STATE_RX_ENABLED: dict[State, bool] = {
    State.ERROR_RESET: False,
    State.ERROR_WAIT: True,
    State.READY: True,
    State.STARTED: True,
    State.CONNECTING: True,
    State.RUN: True,
}


@dataclass
class LinkFSM:
    """SpaceWire link initialization state machine (ECSS Rev.1 sec. 8.5).

    Pure Python, clock-free.  The cocotb layer owns the 6.4 us and 12.8 us
    timers and feeds ``TICK_6_4US`` / ``TICK_12_8US`` events at their expiry.
    """

    target_bit_rate_bps: int = 200_000_000
    _state: State = State.ERROR_RESET

    # Rev.1 sec. 8.5: gotNULL, once asserted, "qualifies" later received
    # characters as protocol events instead of noise.  It is only cleared on
    # re-entry to ErrorReset (SpaceWire Light spwlink.vhd does the same).
    _got_null_latched: bool = False

    # Application-level flags (edge-set / edge-cleared via events).
    _link_start: bool = False
    _link_disable: bool = False
    _auto_start: bool = False

    # Per-visit transient flags.  Reset on state entry.
    _sent_null_in_started: bool = False
    _sent_fct_in_connecting: bool = False
    _got_fct_in_connecting: bool = False

    # --- public surface -------------------------------------------------

    @property
    def state(self) -> State:
        """Current FSM state."""

        return self._state

    @property
    def got_null_latched(self) -> bool:
        """Whether the peer's first NULL has been observed since last reset."""

        return self._got_null_latched

    def output(self) -> Output:
        """Return the current output snapshot (state, tx, rx, bit rate)."""

        bit_rate = (
            self.target_bit_rate_bps
            if self._state is State.RUN
            else INITIAL_BIT_RATE_BPS
        )
        return Output(
            state=self._state,
            tx_mode=_STATE_TX[self._state],
            rx_enabled=_STATE_RX_ENABLED[self._state],
            bit_rate_bps=bit_rate,
        )

    def feed(self, event: Event) -> Output:
        """Process one input event and return the resulting output snapshot.

        The FSM may chain transitions when latched flags already satisfy the
        next state's guards (e.g. entering Ready with ``linkStart`` already
        set immediately advances to Started).  Chained transitions are
        driven by a synthetic ``None`` event so no flag is disturbed.
        """

        self._update_flags(event)

        # Bounded loop: at most one transition per state, and there are six
        # states, so eight iterations is a safe cap that also catches bugs.
        current_event: Event | None = event
        for _ in range(8):
            handler = _HANDLERS[self._state]
            next_state = handler(self, current_event)
            if next_state is None or next_state is self._state:
                break
            self._enter(next_state)
            current_event = None

        return self.output()

    # --- flag / latch bookkeeping --------------------------------------

    def _update_flags(self, event: Event) -> None:
        """Apply level-style flag updates before the transition decision."""

        if event is Event.GOT_NULL:
            self._got_null_latched = True
        elif event is Event.LINK_START:
            self._link_start = True
        elif event is Event.LINK_START_CLEAR:
            self._link_start = False
        elif event is Event.LINK_DISABLE:
            self._link_disable = True
        elif event is Event.LINK_DISABLE_CLEAR:
            self._link_disable = False
        elif event is Event.AUTO_START:
            self._auto_start = True
        elif event is Event.AUTO_START_CLEAR:
            self._auto_start = False
        elif event is Event.SENT_NULL and self._state is State.STARTED:
            self._sent_null_in_started = True
        elif event is Event.SENT_FCT and self._state is State.CONNECTING:
            self._sent_fct_in_connecting = True
        elif event is Event.GOT_FCT and self._state is State.CONNECTING:
            self._got_fct_in_connecting = True

    def _enter(self, next_state: State) -> None:
        """Transition into ``next_state`` and reset per-visit flags."""

        self._state = next_state

        if next_state is State.ERROR_RESET:
            # Rev.1 sec. 8.5: leaving the link resets the gotNULL qualifier;
            # a fresh handshake must re-observe a NULL from the remote end.
            self._got_null_latched = False
            self._sent_null_in_started = False
            self._sent_fct_in_connecting = False
            self._got_fct_in_connecting = False
        elif next_state is State.STARTED:
            self._sent_null_in_started = False
        elif next_state is State.CONNECTING:
            self._sent_fct_in_connecting = False
            self._got_fct_in_connecting = False

    # --- shared predicates ---------------------------------------------

    def _link_enabled(self) -> bool:
        """Rev.1: LinkEnabled = !linkDisabled AND (linkStart OR autoStart & gotNULL)."""

        if self._link_disable:
            return False
        if self._link_start:
            return True
        return self._auto_start and self._got_null_latched

    def _wait_state_error(self, event: Event | None) -> bool:
        """Error condition shared by ErrorWait/Ready/Started (Rev.1 sec. 8.5).

        rx_err is always an error.  Character-level events count only after
        gotNULL has been latched (before then the RX is not yet character-
        synchronised, so a spurious FCT/N-Char/TimeCode is treated as noise).
        """

        if event is Event.RX_ERR:
            return True
        if not self._got_null_latched:
            return False
        return event in {Event.GOT_FCT, Event.GOT_N_CHAR, Event.GOT_TIME_CODE}


# ----- per-state handlers ------------------------------------------------
# Each handler receives the event AFTER _update_flags has run.  It returns
# the next state, or ``None`` to stay put.


def _in_error_reset(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.1: ErrorReset waits 6.4 us, then advances if the link
    # is not disabled.
    if event is Event.TICK_6_4US and not fsm._link_disable:
        return State.ERROR_WAIT
    return None


def _in_error_wait(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.2.
    if fsm._wait_state_error(event):
        return State.ERROR_RESET
    if event is Event.TICK_12_8US:
        return State.READY
    return None


def _in_ready(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.3.
    if fsm._wait_state_error(event):
        return State.ERROR_RESET
    if fsm._link_enabled():
        return State.STARTED
    return None


def _in_started(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.4: Started -> Connecting requires sentNULL AND gotNULL.
    if fsm._wait_state_error(event):
        return State.ERROR_RESET
    if event is Event.TICK_12_8US:
        return State.ERROR_RESET
    if fsm._sent_null_in_started and fsm._got_null_latched:
        return State.CONNECTING
    return None


def _in_connecting(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.5: Connecting -> Run requires sentFCT AND gotFCT.
    # An N-Char or TimeCode before the FCT handshake completes is an error.
    if event is Event.RX_ERR:
        return State.ERROR_RESET
    if event is Event.CREDIT_ERR:
        return State.ERROR_RESET
    if event in {Event.GOT_N_CHAR, Event.GOT_TIME_CODE}:
        return State.ERROR_RESET
    if event is Event.TICK_12_8US:
        return State.ERROR_RESET
    if fsm._sent_fct_in_connecting and fsm._got_fct_in_connecting:
        return State.RUN
    return None


def _in_run(fsm: LinkFSM, event: Event | None) -> State | None:
    # Rev.1 sec. 8.5.3.6.
    if event in {Event.RX_ERR, Event.CREDIT_ERR}:
        return State.ERROR_RESET
    if event is Event.LINK_DISABLE:
        return State.ERROR_RESET
    return None


_HANDLERS = {
    State.ERROR_RESET: _in_error_reset,
    State.ERROR_WAIT: _in_error_wait,
    State.READY: _in_ready,
    State.STARTED: _in_started,
    State.CONNECTING: _in_connecting,
    State.RUN: _in_run,
}


__all__ = [
    "INITIAL_BIT_RATE_BPS",
    "Event",
    "LinkFSM",
    "Output",
    "State",
    "TxMode",
]
